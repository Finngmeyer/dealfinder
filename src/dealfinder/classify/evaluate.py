"""Score the classifier against descriptions someone has labeled by hand.

The file is a CSV with a ``description`` column and a ``units`` column holding the true unit
count, ``land`` for a lot, or ``unknown`` when the text does not say. Optional columns give the
classifier what a listing source would: ``source_kind``, ``stated_units``, ``bedrooms``,
``bathrooms``.

The headline number is fourplex precision: of the listings the classifier calls a fourplex
(four units at medium or high confidence), the share that really are.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import get_args

from pydantic import BaseModel

from dealfinder.classify.classifier import Classification, ClassifierInput, classify
from dealfinder.models import PropertyKind

DEFAULT_KIND: PropertyKind = "multifamily_2_4"  # what a source says of a small multifamily listing
_KINDS = set(get_args(PropertyKind))


class LabeledRow(BaseModel):
    row: int  # spreadsheet row number
    entry: ClassifierInput
    label: int | str  # unit count, "land" or "unknown"


class Miss(BaseModel):
    row: int
    description: str
    expected: int | str
    got: str


class EvalReport(BaseModel):
    rows: int
    correct: int
    fourplex_called: int  # how many the classifier called a fourplex
    fourplex_right: int  # how many of those are
    fourplex_actual: int  # how many really are fourplexes
    misses: list[Miss]

    @property
    def precision(self) -> float | None:
        return self.fourplex_right / self.fourplex_called if self.fourplex_called else None

    @property
    def recall(self) -> float | None:
        return self.fourplex_right / self.fourplex_actual if self.fourplex_actual else None

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.rows if self.rows else None

    def text(self) -> str:
        def share(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.1%}"

        lines = [
            f"{self.rows} labeled descriptions",
            f"Fourplex precision {share(self.precision)} "
            f"({self.fourplex_right} right of {self.fourplex_called} called a fourplex)",
            f"Fourplex recall {share(self.recall)} "
            f"({self.fourplex_right} found of {self.fourplex_actual} real fourplexes)",
            f"Unit count right on {share(self.accuracy)} of rows ({self.correct} of {self.rows})",
        ]
        for miss in self.misses:
            lines.append(
                f"  row {miss.row}: expected {miss.expected}, got {miss.got}: "
                f"{miss.description[:90]}"
            )
        return "\n".join(lines)


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text and text.strip() else None
    except ValueError:
        return None


def load_labeled(path: Path) -> list[LabeledRow]:
    rows: list[LabeledRow] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"description", "units"} <= set(reader.fieldnames):
            raise ValueError(f"{path} needs a 'description' column and a 'units' column")
        for number, record in enumerate(reader, start=2):
            description = (record.get("description") or "").strip()
            label_text = (record.get("units") or "").strip().lower()
            if not description or not label_text:
                continue
            label: int | str = int(label_text) if label_text.isdigit() else label_text
            if isinstance(label, str) and label not in ("land", "unknown"):
                raise ValueError(f"{path} row {number}: units must be a number, land or unknown")
            kind = (record.get("source_kind") or "").strip() or DEFAULT_KIND
            if kind not in _KINDS:
                raise ValueError(f"{path} row {number}: unknown source_kind '{kind}'")
            stated = _number(record.get("stated_units"))
            rows.append(
                LabeledRow(
                    row=number,
                    label=label,
                    entry=ClassifierInput(
                        source_kind=kind,  # type: ignore[arg-type]
                        stated_units=None if stated is None else int(stated),
                        description=description,
                        bedrooms=_number(record.get("bedrooms")),
                        bathrooms=_number(record.get("bathrooms")),
                    ),
                )
            )
    return rows


SHEET_COLUMNS = ("description", "units", "source_kind", "bedrooms", "bathrooms", "address")


def append_label_rows(path: Path, rows: Sequence[Mapping[str, str]]) -> tuple[int, int]:
    """Add rows to a labeling sheet, skipping descriptions it already holds.

    Existing rows are never touched, so labels typed in by hand are safe. Returns how many
    rows were added and how many were already there.
    """
    existing: list[dict[str, str]] = []
    columns = list(SHEET_COLUMNS)
    if path.is_file():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            columns = [*columns, *(c for c in reader.fieldnames or [] if c not in columns)]
            existing = [dict(record) for record in reader]
    seen = {(record.get("description") or "").strip() for record in existing}
    added = []
    for row in rows:
        text = (row.get("description") or "").strip()
        if text and text not in seen:
            seen.add(text)
            added.append(dict(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([*existing, *added])
    return len(added), len(rows) - len(added)


def _describe(result: Classification) -> str:
    if result.property_kind == "land":
        return "land"
    if result.unit_count is None:
        return "unknown"
    return f"{result.unit_count} ({result.confidence})"


def _is_right(result: Classification, label: int | str) -> bool:
    if label == "land":
        return result.property_kind == "land"
    if label == "unknown":
        return result.property_kind != "land" and not (
            result.unit_count is not None and result.meets("medium")
        )
    return result.unit_count == label and result.meets("medium")


def evaluate(rows: Sequence[LabeledRow], keywords: Mapping[str, Sequence[str]]) -> EvalReport:
    report = EvalReport(
        rows=len(rows), correct=0, fourplex_called=0, fourplex_right=0, fourplex_actual=0, misses=[]
    )
    for row in rows:
        result = classify(row.entry, keywords)
        actual_fourplex = row.label == 4
        report.fourplex_actual += actual_fourplex
        if result.is_fourplex:
            report.fourplex_called += 1
            report.fourplex_right += actual_fourplex
        if _is_right(result, row.label):
            report.correct += 1
        else:
            report.misses.append(
                Miss(
                    row=row.row,
                    description=row.entry.description or "",
                    expected=row.label,
                    got=_describe(result),
                )
            )
    return report
