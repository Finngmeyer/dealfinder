"""Classify stored properties, and look up unit counts the listings do not carry.

``classify_all`` runs the rule-based classifier (and, when asked, Claude) over stored
properties and saves the result: the kind, unit count and confidence on the property, one row
per unit, and the evidence in ``classifications``. A property whose inputs have not changed
since it was last classified is skipped, so the step is cheap to repeat after every import and
sweep, and Claude is never asked twice about the same text.

``resolve_units`` spends RentCast requests: for a listing RentCast only calls "2 to 4 units",
it fetches the property record, which often states the unit count and carries the last tax
bill. One request per listing, likeliest matches first, inside the request budget.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import Engine, distinct, func, select
from sqlalchemy.orm import Session, selectinload

from dealfinder.adapters.rentcast import PropertyRecord, RentCastSource
from dealfinder.classify.classifier import (
    CLASSIFIER_VERSION,
    KIND_BY_COUNT,
    RANK,
    SINGLE_DWELLING,
    Classification,
    ClassifierInput,
    classify,
    kind_matches,
)
from dealfinder.classify.llm import LLM_METHOD, Extractor, apply_extraction
from dealfinder.http import SourceUnavailable
from dealfinder.settings import Config, LlmSettings
from dealfinder.store.db import session_scope
from dealfinder.store.tables import (
    ClassificationRow,
    ListingRow,
    PropertyRow,
    UnitRow,
    utcnow,
)
from dealfinder.store.usage import BudgetExceeded

# How specific a source's own label is; the most specific label among a property's listings wins.
_SPECIFICITY = {"other": 0, "multifamily_2_4": 1}
_BATCH = 500
RECORD_KEY = "_property_record"  # where a listing's raw_json keeps its property-record lookup


def classifier_input(row: PropertyRow) -> ClassifierInput:
    """What the classifier needs, gathered from a property and all of its listings.

    Structured facts come from the sources, never from an earlier classification, so the
    classifier can be rerun after a rule change without feeding on its own output.
    """
    listings = row.listings
    stated = [item.unit_count_stated for item in listings if item.unit_count_stated]
    kinds = sorted(
        (item.source_kind for item in listings), key=lambda kind: _SPECIFICITY.get(kind, 2)
    )
    descriptions = sorted((item.description or "" for item in listings), key=len)
    return ClassifierInput(
        source_kind=kinds[-1] if kinds else "other",  # type: ignore[arg-type]
        stated_units=max(stated) if stated else None,
        description=descriptions[-1] if descriptions and descriptions[-1] else None,
        bedrooms=row.bedrooms,
        bathrooms=row.bathrooms,
        building_sqft=row.building_sqft,
    )


def input_hash(entry: ClassifierInput, keywords: Mapping[str, Sequence[str]]) -> str:
    """A fingerprint of everything a classification depends on: the listing's facts and text,
    the keyword lists, and the version of the rules."""
    payload = json.dumps(
        [
            CLASSIFIER_VERSION,
            entry.model_dump(mode="json"),
            {flag: list(phrases) for flag, phrases in keywords.items()},
        ],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def save_classification(
    session: Session, row: PropertyRow, result: Classification, digest: str
) -> None:
    """Store ``result`` as the property's classification and units.

    Units are updated in place. A unit whose layout did not change keeps the market rent
    already looked up for it; one whose layout changed loses it, since that rent was for a
    different unit.
    """
    stored = session.scalars(
        select(ClassificationRow).where(ClassificationRow.property_id == row.id)
    ).first()
    if stored is None:
        stored = ClassificationRow(property_id=row.id)
        session.add(stored)
    stored.property_kind = result.property_kind
    stored.unit_count = result.unit_count
    stored.confidence = result.confidence
    stored.signals = [signal.model_dump() for signal in result.signals]
    stored.flags = list(result.flags)
    stored.method = result.method
    stored.version = result.version
    stored.input_hash = digest
    stored.created_at = utcnow()

    existing = {
        unit.position: unit
        for unit in session.scalars(select(UnitRow).where(UnitRow.property_id == row.id))
    }
    for position, unit in enumerate(result.units):
        target = existing.pop(position, None)
        if target is None:
            target = UnitRow(property_id=row.id, position=position, comps=[])
            session.add(target)
        elif (target.beds, target.baths, target.sqft) != (unit.beds, unit.baths, unit.sqft):
            target.market_rent = target.market_rent_source = None
            target.rent_low = target.rent_high = None
            target.comps = []
        target.beds, target.baths, target.sqft = unit.beds, unit.baths, unit.sqft
        target.in_place_rent = unit.in_place_rent
        target.in_place_rent_quote = unit.rent_quote
        target.vacant = unit.vacant
        target.mix_confidence = result.mix_confidence
    for leftover in existing.values():
        session.delete(leftover)

    row.property_kind = result.property_kind
    row.unit_count = result.unit_count
    row.unit_confidence = result.confidence
    session.flush()


def wants_llm(result: Classification, entry: ClassifierInput, llm: LlmSettings) -> bool:
    """Whether Claude should read this description: there is one, the source gives no unit
    count of its own, the rules are less sure than the setting asks, and it is a building."""
    return (
        bool(entry.description)
        and entry.stated_units is None
        and RANK[result.confidence] < RANK[llm.only_below_confidence]
        and result.property_kind != "land"
        and entry.source_kind not in SINGLE_DWELLING
    )


class ClassifyReport(BaseModel):
    properties: int = 0  # stored in total
    read: int = 0  # classified in this run; the rest were unchanged
    by_kind: dict[str, int] = Field(default_factory=dict)
    in_buy_box: int = 0  # a wanted kind at the required confidence
    unverified: int = 0  # might be a wanted kind; the unit count is not confirmed
    with_actual_rents: int = 0
    llm_calls: int = 0
    llm_note: str = ""

    def text(self) -> str:
        kinds = ", ".join(
            f"{count:,} {kind.replace('_', ' ')}" for kind, count in self.by_kind.items()
        )
        lines = [
            f"{self.properties:,} properties stored; {self.read:,} read in this run, "
            f"{self.properties - self.read:,} unchanged.",
            f"By kind: {kinds or 'none'}.",
            f"Of the wanted property types: {self.in_buy_box:,} confirmed, "
            f"{self.unverified:,} with an unconfirmed unit count.",
            f"With rents stated in the listing: {self.with_actual_rents:,}.",
        ]
        if self.llm_calls or self.llm_note:
            lines.append(f"Claude read {self.llm_calls:,} descriptions. {self.llm_note}".strip())
        return "\n".join(lines)


def classify_all(
    engine: Engine,
    config: Config,
    *,
    extractor: Extractor | None = None,
    force: bool = False,
) -> ClassifyReport:
    """Classify every stored property whose inputs changed since it was last classified.

    ``extractor`` is the optional Claude step; with it, properties the rules are unsure of
    are read again even when unchanged, unless Claude has already read them. ``force``
    reclassifies everything.
    """
    settings = config.settings
    llm = settings.classify.llm
    report = ClassifyReport()
    active = extractor
    with session_scope(engine) as session:
        ids = list(session.scalars(select(PropertyRow.id).order_by(PropertyRow.id)))
        known = {
            property_id: (digest, method)
            for property_id, digest, method in session.execute(
                select(
                    ClassificationRow.property_id,
                    ClassificationRow.input_hash,
                    ClassificationRow.method,
                )
            )
        }

    for start in range(0, len(ids), _BATCH):
        with session_scope(engine) as session:
            rows = session.scalars(
                select(PropertyRow)
                .where(PropertyRow.id.in_(ids[start : start + _BATCH]))
                .options(selectinload(PropertyRow.listings))
                .order_by(PropertyRow.id)
            ).all()
            for row in rows:
                entry = classifier_input(row)
                digest = input_hash(entry, config.keywords)
                before = known.get(row.id)
                unchanged = before is not None and before[0] == digest and not force
                if unchanged and (active is None or (before and before[1] == LLM_METHOD)):
                    continue
                result = classify(entry, config.keywords)
                asked = False
                if (
                    active is not None
                    and wants_llm(result, entry, llm)
                    and report.llm_calls < llm.max_calls_per_run
                ):
                    report.llm_calls += 1
                    try:
                        extraction = active(entry.description or "")
                    except SourceUnavailable as problem:
                        report.llm_note = f"Stopped asking: {problem.reason}."
                        active = None
                    else:
                        asked = True
                        if extraction is not None:
                            result = apply_extraction(result, extraction, entry)
                        result.method = LLM_METHOD  # asked, even if it had nothing to add
                if unchanged and not asked:
                    continue  # the rules already ran on exactly this; nothing new to store
                save_classification(session, row, result, digest)
                report.read += 1

    box = settings.buy_box
    kinds: Counter[str] = Counter()
    with session_scope(engine) as session:
        for kind, confidence in session.execute(
            select(PropertyRow.property_kind, PropertyRow.unit_confidence)
        ):
            report.properties += 1
            kinds[kind] += 1
            match = kind_matches(kind, confidence, box.property_types, box.min_unit_confidence)
            report.in_buy_box += match == "yes"
            report.unverified += match == "unverified"
        report.with_actual_rents = (
            session.scalar(
                select(func.count(distinct(UnitRow.property_id))).where(
                    UnitRow.in_place_rent.is_not(None)
                )
            )
            or 0
        )
    report.by_kind = dict(kinds.most_common())
    if extractor is not None and report.llm_calls >= llm.max_calls_per_run and not report.llm_note:
        report.llm_note = (
            f"Reached classify.llm.max_calls_per_run ({llm.max_calls_per_run}); "
            "run again to continue."
        )
    return report


def label_rows(engine: Engine) -> list[dict[str, str]]:
    """One labeling-sheet row per stored property that has a description.

    ``units`` is filled in where a source states the unit count as data, which makes those
    rows a ready-made answer key: the sheet leaves the stated count out of the classifier's
    inputs, so scoring it measures how well the text alone is read.
    """

    def number(value: float | None) -> str:
        return "" if value is None else f"{value:g}"

    rows: list[dict[str, str]] = []
    with session_scope(engine) as session:
        query = select(PropertyRow).options(selectinload(PropertyRow.listings))
        for place in session.scalars(query.order_by(PropertyRow.id)):
            entry = classifier_input(place)
            if not entry.description:
                continue
            fixed = entry.source_kind in SINGLE_DWELLING or entry.source_kind == "land"
            rows.append(
                {
                    "description": entry.description,
                    "units": "land" if entry.source_kind == "land" else number(entry.stated_units),
                    "source_kind": entry.source_kind if fixed else "",
                    "bedrooms": number(entry.bedrooms),
                    "bathrooms": number(entry.bathrooms),
                    "address": ", ".join(
                        part for part in (place.address_line1, place.city, place.state) if part
                    ),
                }
            )
    return rows


class ResolveReport(BaseModel):
    candidates: int = 0  # listings whose unit count is not confirmed
    looked_up: int = 0
    requests: int = 0  # billed; lookups answered from the cache are free
    with_unit_count: int = 0
    with_tax_bill: int = 0
    no_record: int = 0
    stopped: str = ""

    def text(self) -> str:
        lines = [
            f"{self.candidates:,} listings had no confirmed unit count; looked up "
            f"{self.looked_up:,} ({self.requests} billed).",
            f"Unit count found for {self.with_unit_count:,}; tax bill for {self.with_tax_bill:,}; "
            f"no record for {self.no_record:,}.",
        ]
        if self.stopped:
            lines.append(self.stopped)
        return "\n".join(lines)


def unresolved_listings(session: Session, config: Config) -> list[ListingRow]:
    """Active RentCast listings of 2-4 unit buildings whose unit count nothing has confirmed,
    skipping any already looked up and any over the buy box price.

    Requests are scarce, so the order matters: first the listings whose bed and bath totals
    hint at a kind the buy box wants, then those with no hint, then the rest; cheapest first
    within each group. Empty when the buy box wants no 2-4 unit kind.
    """
    box = config.settings.buy_box
    wanted = set(box.property_types) & set(KIND_BY_COUNT.values())
    if not wanted:
        return []
    cap = box.max_price

    def priority(row: ListingRow) -> int:
        place = row.property
        if place.unit_count is None:
            return 1
        return 0 if KIND_BY_COUNT.get(place.unit_count) in wanted else 2

    rows = session.scalars(
        select(ListingRow)
        .join(PropertyRow)
        .where(
            ListingRow.source == "rentcast",
            ListingRow.status == "active",
            ListingRow.source_kind == "multifamily_2_4",
            (PropertyRow.unit_confidence.is_(None)) | (PropertyRow.unit_confidence == "low"),
        )
        .order_by(ListingRow.price.asc().nulls_last(), ListingRow.id)
    ).all()
    pending = [
        row
        for row in rows
        if RECORD_KEY not in (row.raw_json or {}) and (cap is None or (row.price or 0) <= cap)
    ]
    return sorted(pending, key=priority)  # stable: keeps cheapest first within a group


def resolve_units(
    engine: Engine, config: Config, source: RentCastSource, *, limit: int | None = None
) -> ResolveReport:
    """Fetch property records for unresolved listings, then reclassify those properties."""
    report = ResolveReport()
    with session_scope(engine) as session:
        candidates = unresolved_listings(session, config)
        report.candidates = len(candidates)
        ids = [row.id for row in candidates[:limit]]

    for listing_id in ids:
        with session_scope(engine) as session:
            row = session.get_one(ListingRow, listing_id)
            billed_before = source.requests_billed
            try:
                record = source.property_record(row.source_listing_id)
            except BudgetExceeded as stop:
                report.stopped = f"Stopped: {stop}"
                break
            except SourceUnavailable as problem:
                if problem.status != 404:  # 404: RentCast has no record for this address
                    report.stopped = f"Stopped: {problem.reason}."
                    break
                record = None
            report.looked_up += 1
            report.requests += source.requests_billed - billed_before
            report.no_record += record is None
            report.with_unit_count += bool(record and record.unit_count is not None)
            report.with_tax_bill += bool(record and record.tax_annual is not None)
            apply_record(session, row, record, config)
    return report


def reclassify(session: Session, place: PropertyRow, config: Config) -> Classification:
    """Run the rules on a property again and store the result."""
    entry = classifier_input(place)
    result = classify(entry, config.keywords)
    save_classification(session, place, result, input_hash(entry, config.keywords))
    return result


def apply_record(
    session: Session, row: ListingRow, record: PropertyRecord | None, config: Config
) -> None:
    """Store what a property-record lookup found (None: RentCast has no record) on the
    listing, note that it was looked up so it is never asked for again, and reclassify."""
    note: dict[str, object] = {"on": date.today().isoformat(), "found": record is not None}
    place = row.property
    if record is not None:
        if record.unit_count is not None:
            row.unit_count_stated = record.unit_count
        if record.tax_annual is not None:
            row.tax_annual = record.tax_annual
        for name in ("bedrooms", "bathrooms", "building_sqft", "year_built"):
            if getattr(place, name) is None:  # fill gaps; never replace the listing's own
                setattr(place, name, getattr(record, name))
        note.update(record.model_dump(exclude={"property_id"}))
    row.raw_json = {**(row.raw_json or {}), RECORD_KEY: note}
    session.flush()
    reclassify(session, place, config)


def read_with_claude(
    session: Session, place: PropertyRow, config: Config, extractor: Extractor
) -> bool:
    """Have Claude read one property's description, if the rules are unsure and it has not
    been read already. True when Claude was asked. Raises SourceUnavailable when it cannot be."""
    stored = session.scalars(
        select(ClassificationRow).where(ClassificationRow.property_id == place.id)
    ).first()
    entry = classifier_input(place)
    digest = input_hash(entry, config.keywords)
    if stored is not None and stored.input_hash == digest and stored.method == LLM_METHOD:
        return False
    result = classify(entry, config.keywords)
    if not wants_llm(result, entry, config.settings.classify.llm):
        return False
    extraction = extractor(entry.description or "")
    if extraction is not None:
        result = apply_extraction(result, extraction, entry)
    result.method = LLM_METHOD
    save_classification(session, place, result, digest)
    return True
