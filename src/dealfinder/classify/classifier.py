"""Decide what a listing is and how many units it has.

Evidence, strongest first:
  * a unit count the source states as data (high)
  * per-unit detail in the text, such as "each unit 2bd/1ba" alongside a count (high)
  * the building's total said outright, a word like fourplex or four-family, "4 units",
    unit numbers that run 1 to N, or a count of meters (medium; high when it agrees with the
    structured count)
  * a bed/bath pattern such as 8 bd / 4 ba on a listing the source calls 2-4 units (low)

A fourplex is four units at medium or high confidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from dealfinder.classify import patterns as p
from dealfinder.classify.keywords import find_flags
from dealfinder.classify.rents import find_rents, unit_clauses
from dealfinder.classify.unit_mix import MixConfidence, UnitDetail, even_mix
from dealfinder.models import PropertyKind

Confidence = Literal["high", "medium", "low"]
CLASSIFIER_VERSION = "1"
RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
SINGLE_DWELLING = frozenset({"sfr", "condo", "townhouse", "manufactured"})
MAX_UNITS = 500  # anything larger is a typo or a price
KIND_BY_COUNT: dict[int, PropertyKind] = {2: "duplex", 3: "triplex", 4: "fourplex"}
_NOT_A_TOTAL = frozenset(
    {"other", "top", "upper", "lower", "remaining", "first", "last", "front", "rear", "back",
     "additional", "more", "another", "next", "bottom", "middle", "these", "those", "both"}
)  # fmt: skip


class Signal(BaseModel):
    """One piece of evidence about the unit count."""

    name: str
    strength: Confidence
    unit_count: int | None
    evidence: str  # the listing's own words, or a description of the structured value


class ClassifierInput(BaseModel):
    source_kind: PropertyKind = "other"
    stated_units: int | None = None  # a unit count the source gives as data
    description: str | None = None
    bedrooms: float | None = None  # building totals
    bathrooms: float | None = None
    building_sqft: float | None = None


class Classification(BaseModel):
    property_kind: PropertyKind
    unit_count: int | None
    confidence: Confidence
    signals: list[Signal]
    flags: list[str]
    units: list[UnitDetail]
    mix_confidence: MixConfidence | None  # None when there is no unit count to split by
    method: str = "rules"
    version: str = CLASSIFIER_VERSION

    @property
    def is_fourplex(self) -> bool:
        return self.unit_count == 4 and self.meets("medium")

    def meets(self, minimum: Confidence) -> bool:
        return RANK[self.confidence] >= RANK[minimum]


@dataclass
class _Evidence:
    """What the text says, sorted by kind. Each entry is (unit count, quote)."""

    hypothetical: list[str] = field(default_factory=list)
    totals: list[tuple[int, str]] = field(default_factory=list)
    multiples: list[tuple[int, str]] = field(default_factory=list)
    counted: list[tuple[int, float, float | None, str]] = field(default_factory=list)
    plex: list[tuple[int, str, int, int]] = field(default_factory=list)  # count, quote, span
    plex_total: tuple[int, str] | None = None
    units_plain: list[tuple[int, str]] = field(default_factory=list)
    numbering: tuple[int, str] | None = None
    meters: list[tuple[int, str]] = field(default_factory=list)
    each_unit: tuple[float, float | None, str] | None = None
    land: bool = False


def _beds(token: str) -> float:
    return 0.0 if token == "studio" else float(token)


def _read_text(original: str) -> _Evidence:
    """Collect evidence from a description. Spans are blanked as they are consumed so that,
    for example, "two fourplexes" is not also read as one fourplex."""
    found = _Evidence()
    text = p.normalize(original)

    def consume(pattern, handler) -> None:  # type: ignore[no-untyped-def]
        nonlocal text
        for match in list(pattern.finditer(text)):
            if handler(match) is not False:
                text = p.mask(text, match.start(), match.end())

    def quote(match) -> str:  # type: ignore[no-untyped-def]
        return original[match.start() : match.end()].strip()

    # Things that could be built, not things that are there.
    consume(p.HYPOTHETICAL_AFTER, lambda m: found.hypothetical.append(quote(m)))
    consume(p.HYPOTHETICAL, lambda m: found.hypothetical.append(quote(m)))

    def total(match) -> bool | None:  # type: ignore[no-untyped-def]
        count = p.to_int(match["n"])
        if not 2 <= count <= MAX_UNITS:
            return False
        found.totals.append((count, quote(match)))
        return None

    for pattern in p.TOTAL_PATTERNS:
        consume(pattern, total)
    consume(p.SUBSET, lambda m: None)  # "4 units available" says nothing about the total

    def multiple(match) -> None:  # type: ignore[no-untyped-def]
        times = 2 if "of" in match["n"] else p.to_int(match["n"])
        word = match["plex"]
        digits = "".join(character for character in word if character.isdigit())
        per_building = next(
            (
                count
                for name, count in sorted(p.PLEX_WORDS.items(), key=lambda item: -len(item[0]))
                if word.startswith(name)
            ),
            int(digits) if digits else 0,
        )
        if per_building:
            found.multiples.append((times * per_building, quote(match)))

    consume(p.PLEX_MULTIPLE, multiple)

    def counted(match) -> None:  # type: ignore[no-untyped-def]
        baths = float(match["baths"]) if match["baths"] else None
        found.counted.append((p.to_int(match["n"]), _beds(match["beds"]), baths, quote(match)))

    consume(p.COUNTED_UNITS, counted)

    def counted_after(match) -> None:  # type: ignore[no-untyped-def]
        beds = match["beds"] or match["sbeds"]
        baths = match["baths"] or match["sbaths"]
        found.counted.append((p.to_int(match["n"]), _beds(beds), float(baths), quote(match)))

    consume(p.COUNTED_UNITS_AFTER, counted_after)

    before_plex = text

    def plex(match, count: int) -> None:  # type: ignore[no-untyped-def]
        found.plex.append((count, quote(match), match.start(), match.end()))

    consume(p.PLEX_SINGLE, lambda m: plex(m, p.PLEX_WORDS[m["plex"]]))

    def numbered_plex(match) -> bool | None:  # type: ignore[no-untyped-def]
        count = int(match["n"])
        if not 2 <= count <= 12:
            return False
        plex(match, count)
        return None

    consume(p.PLEX_NUMERIC, numbered_plex)

    def family(match) -> bool | None:  # type: ignore[no-untyped-def]
        count = p.to_int(match["n"])
        if not 2 <= count <= 12:
            return False  # "single-family" is not in the pattern; "1 family" is not a plex
        plex(match, count)
        return None

    consume(p.FAMILY, family)
    found.plex_total = _combine_plex(found.plex, before_plex)

    def plain(match) -> bool | None:  # type: ignore[no-untyped-def]
        count = p.to_int(match["n"])
        before = text[: match.start()].split()
        if not 2 <= count <= MAX_UNITS or (before and before[-1] in _NOT_A_TOTAL):
            return False
        found.units_plain.append((count, quote(match)))
        return None

    consume(p.UNITS_PLAIN, plain)

    def meters(match) -> bool | None:  # type: ignore[no-untyped-def]
        count = p.to_int(match["n"])
        if not 2 <= count <= 12:
            return False
        found.meters.append((count, quote(match)))
        return None

    consume(p.METERS, meters)

    # "each unit 2bd/1ba": a layout that holds for every unit.
    for match in p.EACH_UNIT.finditer(text):
        window = text[match.end() : match.end() + 45]
        mix = p.BED_BATH.search(window) or p.SLASH_MIX.search(window)
        if mix:
            end = match.end() + mix.end()
            found.each_unit = (
                _beds(mix["beds"]),
                float(mix["baths"]),
                original[match.start() : end].strip(),
            )
            break

    # Unit numbers that run 1..N say there are N units; a lone "Unit 2" says only "at least 2".
    numbers: set[int] = set()
    spans: list[tuple[int, int]] = []
    for match in p.UNIT_RANGE.finditer(text):
        first, last = int(match["first"]), int(match["last"])
        if 1 <= first < last <= 60:
            numbers.update(range(first, last + 1))
            spans.append(match.span())
    for match in p.UNIT_LABEL.finditer(text):
        if match["label"].isdigit():
            numbers.add(int(match["label"]))
            spans.append(match.span())
    if numbers and max(numbers) >= 2 and numbers == set(range(1, max(numbers) + 1)):
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        found.numbering = (max(numbers), original[start:end].strip())

    found.land = bool(p.LAND_START.search(text) or p.LAND_ANYWHERE.search(text))
    return found


_JOINED = ("and", "plus", "&", "+")


def _combine_plex(mentions: list[tuple[int, str, int, int]], text: str) -> tuple[int, str] | None:
    """The count a listing's plex words add up to.

    The first mention is the headline. Later mentions are added only when joined to the one
    before by "and" or "plus" within a few words ("duplex and triplex on one lot"), so a
    passing comparison ("fourplex that lives like a duplex") does not inflate the count.
    """
    if not mentions:
        return None
    ordered = sorted(mentions, key=lambda mention: mention[2])
    total, quotes, last_end = ordered[0][0], [ordered[0][1]], ordered[0][3]
    for count, quote, start, end in ordered[1:]:
        between = text[last_end:start]
        if len(between) > 25 or not any(word in between.split() for word in _JOINED):
            break
        total += count
        quotes.append(quote)
        last_end = end
    return total, " + ".join(quotes)


def _best(entries: Sequence[tuple[int, str]]) -> tuple[int, str] | None:
    """The largest count among entries of one kind: a building's total is never smaller than a
    part of it that the text also mentions."""
    return max(entries, key=lambda entry: entry[0]) if entries else None


def _text_count(found: _Evidence, single_dwelling: bool) -> tuple[int, str, str, bool] | None:
    """(unit count, evidence name, quote, whether the text also gives per-unit detail).

    Nothing is read from the text of a listing the source calls a house, condo or townhouse:
    "24-unit complex" or "end unit in a fourplex" there describes the building around it.
    """
    plex = found.plex_total
    if single_dwelling:
        return None

    counted_total = sum(count for count, _, _, _ in found.counted)
    detail = found.each_unit is not None
    total = _best(found.totals)
    if total:
        return total[0], "stated_total", total[1], detail or counted_total == total[0]
    multiple = _best(found.multiples)
    if multiple:
        return multiple[0], "several_buildings", multiple[1], detail
    if plex:
        return plex[0], "plex_word", plex[1], detail or counted_total == plex[0]
    plain = _best(found.units_plain)
    # "5 units: four 1-bedroom apartments and a studio" lists only some of them.
    if counted_total >= 2 and (plain is None or counted_total >= plain[0]):
        return counted_total, "units_listed", "; ".join(q for _, _, _, q in found.counted), True
    if plain:
        return plain[0], "units_mentioned", plain[1], detail
    if found.numbering:
        return found.numbering[0], "unit_numbers", found.numbering[1], False
    meters = _best(found.meters)
    if meters:
        return meters[0], "meters", meters[1], False
    return None


def kind_for(count: int | None, source_kind: PropertyKind) -> PropertyKind:
    if count is None:
        return source_kind
    if count >= 5:
        return "multifamily_5plus"
    if count == 1:
        return source_kind if source_kind in SINGLE_DWELLING else "sfr"
    return KIND_BY_COUNT[count]


def _units(
    count: int, found: _Evidence, entry: ClassifierInput, original: str
) -> tuple[list[UnitDetail], MixConfidence]:
    """Per-unit detail when the text gives it, else the building's totals split evenly."""
    text = p.normalize(original)
    counted_total = sum(n for n, _, _, _ in found.counted)
    numbered = {
        clause.unit: clause for clause in unit_clauses(text, original) if clause.beds is not None
    }
    if found.counted and counted_total == count:
        units = [
            UnitDetail(beds=beds, baths=baths)
            for n, beds, baths, _ in found.counted
            for _ in range(n)
        ]
        confidence: MixConfidence = "high"
    elif set(numbered) == set(range(count)):  # "Unit 1: 2bd/1ba. Unit 2: ..." for every unit
        units = [
            UnitDetail(beds=numbered[position].beds, baths=numbered[position].baths)
            for position in range(count)
        ]
        confidence = "high"
    elif found.each_unit is not None:
        beds, baths, _ = found.each_unit
        units = [UnitDetail(beds=beds, baths=baths) for _ in range(count)]
        confidence = "high"
    else:
        units = even_mix(count, entry.bedrooms, entry.bathrooms, entry.building_sqft)
        confidence = "low"

    rents = find_rents(text, original, count)
    for position, unit in enumerate(units):
        unit.vacant = position in rents.vacant
        if position in rents.rents:
            unit.in_place_rent = rents.rents[position]
            unit.rent_quote = rents.quotes[position]
    return units, confidence


def classify(entry: ClassifierInput, keywords: Mapping[str, Sequence[str]]) -> Classification:
    """Classify one listing from its structured fields and description."""
    original = entry.description or ""
    found = _read_text(original)
    flags = find_flags(original, keywords)
    signals = [
        Signal(name="not_built", strength="low", unit_count=None, evidence=quote)
        for quote in found.hypothetical
    ]
    single = entry.source_kind in SINGLE_DWELLING
    from_text = _text_count(found, single)
    stated = entry.stated_units if entry.stated_units and entry.stated_units >= 1 else None

    # Land: the source says so, or the text describes a lot and nothing standing on it.
    has_building = bool(entry.bedrooms or entry.bathrooms or entry.building_sqft)
    if entry.source_kind == "land" or (
        found.land and stated is None and from_text is None and not has_building
    ):
        signals.append(
            Signal(
                name="land",
                strength="high" if entry.source_kind == "land" else "medium",
                unit_count=None,
                evidence="source lists it as land"
                if entry.source_kind == "land"
                else original[:80],
            )
        )
        return Classification(
            property_kind="land",
            unit_count=None,
            confidence="high" if entry.source_kind == "land" else "medium",
            signals=signals,
            flags=flags,
            units=[],
            mix_confidence=None,
        )

    count: int | None = None
    confidence: Confidence = "low"
    if from_text is not None:
        text_count, name, quote, detailed = from_text
        signals.append(
            Signal(
                name=name,
                strength="high" if detailed or text_count == stated else "medium",
                unit_count=text_count,
                evidence=quote,
            )
        )
        if detailed and found.each_unit is not None:
            signals.append(
                Signal(
                    name="per_unit_detail",
                    strength="high",
                    unit_count=text_count,
                    evidence=found.each_unit[2],
                )
            )
    if stated is not None:
        signals.insert(
            0,
            Signal(
                name="stated_by_source",
                strength="high",
                unit_count=stated,
                evidence=f"the source lists {stated} unit{'s' if stated != 1 else ''}",
            ),
        )
        count, confidence = stated, "high"
        if from_text is not None and from_text[0] != stated:
            confidence = "medium"
            flags = [*flags, "unit_count_conflict"]
    elif from_text is not None:
        count = from_text[0]
        confidence = "high" if from_text[3] else "medium"
        if "commercial" in flags:
            confidence = "low"  # shops or offices: the units may not be homes
    elif single:
        count, confidence = 1, "high"
        signals.append(
            Signal(
                name="single_dwelling",
                strength="high",
                unit_count=1,
                evidence=f"the source lists it as {entry.source_kind}",
            )
        )
    elif entry.source_kind == "multifamily_2_4":
        guess = _bed_bath_guess(entry)
        if guess is not None:
            count, confidence = guess, "low"
            signals.append(
                Signal(
                    name="bed_bath_pattern",
                    strength="low",
                    unit_count=guess,
                    evidence=f"{entry.bedrooms:g} bd / {entry.bathrooms:g} ba",
                )
            )

    if "adu" in flags and count is not None and count >= 2:
        signals.append(
            Signal(name="adu", strength="low", unit_count=None, evidence="one unit is an ADU")
        )
    units: list[UnitDetail] = []
    mix: MixConfidence | None = None
    if count is not None:
        units, mix = _units(count, found, entry, original)
        detailed = mix == "high" and from_text is not None and from_text[0] == count
        if detailed and confidence == "medium" and stated is None and "commercial" not in flags:
            confidence = "high"  # the text describes every unit, which is more than a label
            if not any(signal.name == "per_unit_detail" for signal in signals):
                signals.append(
                    Signal(
                        name="per_unit_detail",
                        strength="high",
                        unit_count=count,
                        evidence="a layout is given for every unit",
                    )
                )
    return Classification(
        property_kind=kind_for(count, entry.source_kind),
        unit_count=count,
        confidence=confidence,
        signals=signals,
        flags=flags,
        units=units,
        mix_confidence=mix,
    )


def _bed_bath_guess(entry: ClassifierInput) -> int | None:
    """A weak guess for a 2-4 unit building: one bathroom per unit, when the totals fit."""
    beds, baths = entry.bedrooms, entry.bathrooms
    if beds is None or baths is None or baths != int(baths):
        return None
    units = int(baths)
    return units if 2 <= units <= 4 and beds >= units else None


Match = Literal["yes", "no", "unverified"]


def kind_matches(
    kind: str, confidence: str | None, types: Sequence[str], minimum: Confidence
) -> Match:
    """Whether a property of this kind belongs to the wanted property types.

    "unverified" means it might: the source says 2-4 units and nothing confirms which, or the
    count is known only below the required confidence. Those go in their own list.
    ``confidence`` None (never classified) counts as low.
    """
    wanted = set(types)
    sure = RANK.get(confidence or "low", 0) >= RANK[minimum]
    if kind in ("land", "other", "manufactured"):
        return "no"
    small_multifamily = wanted & set(KIND_BY_COUNT.values())
    if kind == "multifamily_2_4":
        return "unverified" if small_multifamily else "no"
    if kind in KIND_BY_COUNT.values():
        if kind not in wanted:
            return "no" if sure or not small_multifamily else "unverified"
        return "yes" if sure else "unverified"
    return "yes" if kind in wanted else "no"


def matches_types(result: Classification, types: Sequence[str], minimum: Confidence) -> Match:
    """``kind_matches`` for a classification."""
    return kind_matches(result.property_kind, result.confidence, types, minimum)
