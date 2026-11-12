"""In-place rents and per-unit detail stated in a listing description.

Only clear statements are used: a rent tied to a unit number, to "each unit", or a list with
one figure per unit. A dollar amount next to a unit number counts as rent only when the words
around it say so ("leased at", "/mo") or there are no other words at all ("Unit 1: $900").
Amounts outside a sane monthly range are ignored, which keeps prices, annual totals and repair
bills from being read as rents.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import BaseModel, Field

from dealfinder.classify.patterns import BED_BATH, MONEY, SLASH_MIX, money_to_float

MIN_RENT, MAX_RENT = 200.0, 15_000.0
_CLAUSE_LENGTH = 70

_LEASED = r"(?:are\s+|is\s+|each\s+|currently\s+|both\s+|all\s+)*(?:leased|rented|rents?|renting)"
_AT = r"(?:\s+(?:out|each))?\s*(?:at|for|@|:)?\s*"
UNIT_RANGE_RENT = re.compile(
    rf"\bunits?\s*#?\s*(?P<first>\d{{1,2}})\s*(?:-|to|through|thru|&|and)\s*#?(?P<last>\d{{1,2}})"
    rf"\s+{_LEASED}{_AT}{MONEY}"
)
EACH_RENT = re.compile(
    rf"\b(?:each|every|all|both)\s+(?:of\s+the\s+)?(?:\w+\s+)?(?:units?\s+)?{_LEASED}{_AT}{MONEY}"
    rf"|\brents?\s+(?:are|of|is)\s+{MONEY}\s*(?:/|per|a)?\s*(?:mo(?:nth)?\s+)?(?:each|per\s+unit)"
)
RENT_LIST = re.compile(
    rf"\b(?:current\s+|monthly\s+|actual\s+)?rents?(?:\s+are|\s+of|\s+roll)?\s*:?\s*"
    rf"(?P<list>{MONEY}(?:\s*(?:,|/|&|and|,\s*and)\s*{MONEY})+)"
)
VACANT_RANGE = re.compile(
    r"\bunits?\s*#?\s*(?P<first>\d{1,2})\s*(?:-|to|through|thru|&|and)\s*#?(?P<last>\d{1,2})\s+"
    r"(?:are\s+|both\s+|currently\s+)*vacant\b"
)
_LABEL = re.compile(r"\bunit\s*#?\s*(?P<n>\d{1,2})\b")
_CLAUSE_END = re.compile(r"[.;\n](?!\d)")
_AMOUNT = re.compile(MONEY)
_RENT_CUE = re.compile(r"\b(?:rent(?:s|ed|ing)?|leased?|month(?:ly)?|per\s+mo)\b|/\s*mo")
_FILLER = re.compile(r"\b(?:at|for|is|currently|now)\b")
_WORD = re.compile(r"[a-z]{3,}")


class UnitClause(BaseModel):
    """What the text says right after one "Unit N"."""

    unit: int  # 0-based position
    beds: float | None = None
    baths: float | None = None
    rent: float | None = None
    vacant: bool = False
    quote: str = ""


class RentFindings(BaseModel):
    """Rents by unit position (0-based) with the words each came from."""

    rents: dict[int, float] = Field(default_factory=dict)
    quotes: dict[int, str] = Field(default_factory=dict)
    vacant: set[int] = Field(default_factory=set)


def _plausible(amount: float) -> bool:
    return MIN_RENT <= amount <= MAX_RENT


def unit_clauses(text: str, original: str) -> Iterator[UnitClause]:
    """One entry per "Unit N" in the text, read from the words that follow it up to the end of
    the sentence or the next unit."""
    labels = list(_LABEL.finditer(text))
    for index, label in enumerate(labels):
        start = label.end()
        stop = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        clause = text[start : min(stop, start + _CLAUSE_LENGTH)]
        end = _CLAUSE_END.search(clause)
        if end:
            clause = clause[: end.start()]
        found = UnitClause(
            unit=int(label["n"]) - 1,
            vacant=bool(re.search(r"\bvacant\b", clause)),
            quote=original[label.start() : start + len(clause)].strip(),
        )
        mix = BED_BATH.search(clause) or SLASH_MIX.search(clause)
        rest = clause
        if mix:
            found.beds = 0.0 if mix["beds"] == "studio" else float(mix["beds"])
            found.baths = float(mix["baths"])
            rest = rest[: mix.start()] + rest[mix.end() :]
        money = _AMOUNT.search(rest)
        if money and not found.vacant:
            amount = money_to_float(money.group(1))
            around = _FILLER.sub("", rest[: money.start()] + rest[money.end() :])
            if _plausible(amount) and (_RENT_CUE.search(clause) or not _WORD.search(around)):
                found.rent = amount
        yield found


def find_rents(text: str, original: str, unit_count: int | None) -> RentFindings:
    """Read rents from ``text`` (normalized) and quote ``original`` at the same positions."""
    found = RentFindings()

    def record(unit: int, amount: float, quote: str) -> None:
        if unit < 0 or not _plausible(amount) or (unit_count is not None and unit >= unit_count):
            return
        found.rents.setdefault(unit, amount)
        found.quotes.setdefault(unit, quote)

    for match in VACANT_RANGE.finditer(text):
        first, last = int(match["first"]), int(match["last"])
        if first <= last <= 60:
            found.vacant.update(range(first - 1, last))

    for match in UNIT_RANGE_RENT.finditer(text):
        first, last = int(match["first"]), int(match["last"])
        if not first <= last <= 60:
            continue
        amount = money_to_float(match.group(match.lastindex or 0))
        for unit in range(first - 1, last):
            record(unit, amount, original[match.start() : match.end()].strip())

    for clause in unit_clauses(text, original):
        if clause.vacant:
            found.vacant.add(clause.unit)
        elif clause.rent is not None:
            record(clause.unit, clause.rent, clause.quote)

    if unit_count:
        for match in EACH_RENT.finditer(text):
            amounts = [group for group in match.groups() if group]
            amount = money_to_float(amounts[-1])
            for unit in range(unit_count):
                if unit not in found.vacant:
                    record(unit, amount, original[match.start() : match.end()].strip())
        for match in RENT_LIST.finditer(text):
            amounts = [money_to_float(m.group(1)) for m in _AMOUNT.finditer(match["list"])]
            if len(amounts) == unit_count and all(_plausible(a) for a in amounts):
                for unit, amount in enumerate(amounts):
                    record(unit, amount, original[match.start() : match.end()].strip())

    for unit in found.vacant:
        found.rents.pop(unit, None)
        found.quotes.pop(unit, None)
    return found
