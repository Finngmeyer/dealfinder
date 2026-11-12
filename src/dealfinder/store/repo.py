"""Reading and writing listings, runs, checkpoints and inventory counts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dealfinder.geo import lookup_zip
from dealfinder.models import Listing
from dealfinder.store import dedupe
from dealfinder.store.tables import (
    CheckpointRow,
    InventoryRow,
    ListingRow,
    PriceEventRow,
    PropertyRow,
    RunRow,
    utcnow,
)

# How specific a property kind is. A more specific kind replaces a vaguer one, never the reverse.
_KIND_RANK = {"other": 0, "multifamily_2_4": 1}
_DEFAULT_KIND_RANK = 2


@dataclass(frozen=True)
class DedupeRule:
    radius_m: float
    price_tolerance: float


@dataclass(frozen=True)
class Upserted:
    listing: ListingRow
    created: bool  # first time this source's listing is seen
    new_property: bool  # no existing property matched
    price_changed: bool
    relisted: bool


def _address_key(listing: Listing) -> str | None:
    return dedupe.normalize_address(
        listing.address_line1,
        listing.city,
        listing.state,
        listing.zip_code,
        line2=listing.address_line2,
        keep_unit=listing.property_kind == "condo",
    )


def find_property(session: Session, listing: Listing, rule: DedupeRule) -> PropertyRow | None:
    """The property this listing belongs to, if one is already stored.

    First by normalized address. Then by location: a stored property within ``radius_m`` whose
    active listing is priced within ``price_tolerance`` of this one.
    """
    key = _address_key(listing)
    if key is not None:
        match = session.scalars(
            select(PropertyRow).where(PropertyRow.address_key == key).order_by(PropertyRow.id)
        ).first()
        if match is not None:
            return match
    if listing.latitude is None or listing.longitude is None or not listing.price:
        return None
    nearby = session.scalars(
        select(PropertyRow).where(
            PropertyRow.geo_cell.in_(dedupe.neighbor_cells(listing.latitude, listing.longitude))
        )
    ).all()
    best: tuple[float, PropertyRow] | None = None
    for candidate in nearby:
        if candidate.latitude is None or candidate.longitude is None:
            continue
        distance = dedupe.distance_m(
            listing.latitude, listing.longitude, candidate.latitude, candidate.longitude
        )
        if distance > rule.radius_m:
            continue
        prices = [row.price for row in candidate.listings if row.status == "active"]
        if not any(dedupe.prices_close(listing.price, p, rule.price_tolerance) for p in prices):
            continue
        if best is None or distance < best[0]:
            best = (distance, candidate)
    return best[1] if best else None


def _fill_property(row: PropertyRow, listing: Listing) -> None:
    """Copy what the listing knows onto the property without discarding what is already there."""
    for name in (
        "address_line1",
        "address_line2",
        "city",
        "state",
        "zip_code",
        "county_fips",
        "latitude",
        "longitude",
        "bedrooms",
        "bathrooms",
        "building_sqft",
        "lot_sqft",
        "year_built",
    ):
        value = getattr(listing, name)
        if value is not None and getattr(row, name) is None:
            setattr(row, name, value)
    if row.county_fips is None and row.zip_code:
        place = lookup_zip(row.zip_code)
        if place is not None:
            row.county_fips = place.county_fips
            row.state = row.state or place.state
    if row.address_key is None:
        row.address_key = _address_key(listing)
    if row.geo_cell is None and row.latitude is not None and row.longitude is not None:
        row.geo_cell = dedupe.geo_cell(row.latitude, row.longitude)
    # Kind and unit count belong to the classifier once it has run (it sets a confidence).
    # Until then these are stand-ins taken from the first listing that knows.
    if row.unit_confidence is not None:
        return
    if _KIND_RANK.get(listing.property_kind, _DEFAULT_KIND_RANK) > _KIND_RANK.get(
        row.property_kind, _DEFAULT_KIND_RANK
    ):
        row.property_kind = listing.property_kind
    if listing.unit_count is not None and row.unit_count is None:
        row.unit_count = listing.unit_count


_LISTING_FIELDS = (
    "status",
    "price",
    "property_type_raw",
    "description",
    "hoa_fee_month",
    "tax_annual",
    "listed_date",
    "removed_date",
    "days_on_market",
    "listing_type",
    "mls_name",
    "mls_number",
    "agent_name",
    "agent_phone",
    "office_name",
    "url",
)


def upsert_listing(
    session: Session,
    listing: Listing,
    *,
    rule: DedupeRule,
    run_id: int | None = None,
    now: datetime | None = None,
) -> Upserted:
    """Store a listing. Running it twice with the same data changes nothing but ``last_seen``.

    A changed price adds a ``price_change`` event; a listing that had gone off the market and
    is active again adds ``relisted``.
    """
    moment = now or utcnow()
    row = session.scalars(
        select(ListingRow).where(
            ListingRow.source == listing.source,
            ListingRow.source_listing_id == listing.source_listing_id,
        )
    ).first()

    if row is None:
        property_row = find_property(session, listing, rule)
        new_property = property_row is None
        if property_row is None:
            property_row = PropertyRow(property_kind="other", created_at=moment, updated_at=moment)
            session.add(property_row)
        _fill_property(property_row, listing)
        row = ListingRow(
            property=property_row,
            source=listing.source,
            source_listing_id=listing.source_listing_id,
            unit_count_stated=listing.unit_count,
            source_kind=listing.property_kind,
            raw_json=listing.raw,
            first_seen_at=moment,
            last_seen_at=moment,
            last_seen_run_id=run_id,
            **{name: getattr(listing, name) for name in _LISTING_FIELDS},
        )
        session.add(row)
        session.add(
            PriceEventRow(listing=row, observed_at=moment, event="listed", price=listing.price)
        )
        session.flush()
        return Upserted(
            row, created=True, new_property=new_property, price_changed=False, relisted=False
        )

    relisted = row.status != "active" and listing.status == "active"
    price_changed = (
        listing.price is not None and row.price is not None and listing.price != row.price
    )
    if relisted:
        session.add(
            PriceEventRow(
                listing=row,
                observed_at=moment,
                event="relisted",
                price=listing.price,
                previous_price=row.price,
            )
        )
    elif price_changed:
        session.add(
            PriceEventRow(
                listing=row,
                observed_at=moment,
                event="price_change",
                price=listing.price,
                previous_price=row.price,
            )
        )
    for name in _LISTING_FIELDS:
        value = getattr(listing, name)
        if value is not None or name == "status":
            setattr(row, name, value)
    if listing.unit_count is not None:
        row.unit_count_stated = listing.unit_count
    row.source_kind = listing.property_kind
    # Keep what later steps attached to the record (keys starting with "_"), such as a
    # property-record lookup, when the source sends the listing again.
    kept = {key: value for key, value in (row.raw_json or {}).items() if key.startswith("_")}
    row.raw_json = {**listing.raw, **kept}
    row.last_seen_at = moment
    row.last_seen_run_id = run_id
    row.missed_sweeps = 0
    _fill_property(row.property, listing)
    row.property.updated_at = moment
    session.flush()
    return Upserted(
        row, created=False, new_property=False, price_changed=price_changed, relisted=relisted
    )


def mark_off_market(
    session: Session,
    *,
    source: str,
    state: str,
    raw_types: Sequence[str],
    run_id: int,
    after_missed: int = 1,
    now: datetime | None = None,
) -> int:
    """Handle this source's active listings in a state that a complete sweep did not return.

    Each one's count of missed sweeps goes up, and it is flagged off-market once the count
    reaches ``after_missed``. Only listings of the swept property types are touched. Returns
    how many were flagged.
    """
    moment = now or utcnow()
    missing = session.scalars(
        select(ListingRow)
        .join(PropertyRow)
        .where(
            ListingRow.source == source,
            ListingRow.status == "active",
            PropertyRow.state == state,
            ListingRow.property_type_raw.in_(list(raw_types)),
            (ListingRow.last_seen_run_id.is_(None)) | (ListingRow.last_seen_run_id != run_id),
        )
    ).all()
    flagged = 0
    for row in missing:
        row.missed_sweeps += 1
        if row.missed_sweeps < after_missed:
            continue
        row.status = "off_market"
        flagged += 1
        session.add(
            PriceEventRow(listing=row, observed_at=moment, event="off_market", price=row.price)
        )
    session.flush()
    return flagged


# ---- runs, checkpoints, inventory ------------------------------------------------------------


def start_run(session: Session, mode: str, params: dict[str, Any]) -> RunRow:
    run = RunRow(mode=mode, status="running", params=params, stats={})
    session.add(run)
    session.flush()
    return run


def resumable_run(session: Session, mode: str, params: dict[str, Any]) -> RunRow | None:
    """The most recent unfinished run of this mode with the same parameters."""
    candidates = session.scalars(
        select(RunRow)
        .where(RunRow.mode == mode, RunRow.status.in_(("running", "stopped_budget", "failed")))
        .order_by(RunRow.id.desc())
    ).all()
    return next((run for run in candidates if run.params == params), None)


def finish_run(
    session: Session, run: RunRow, *, status: str, stats: dict[str, Any], report: str
) -> None:
    run.status = status
    run.stats = stats
    run.report = report
    run.finished_at = utcnow()
    session.flush()


def checkpoint(session: Session, run_id: int, source: str, scope: str) -> CheckpointRow:
    """The checkpoint for one search within a run, created at offset 0 the first time."""
    row = session.scalars(
        select(CheckpointRow).where(
            CheckpointRow.run_id == run_id,
            CheckpointRow.source == source,
            CheckpointRow.scope == scope,
        )
    ).first()
    if row is None:
        row = CheckpointRow(run_id=run_id, source=source, scope=scope, next_offset=0, fetched=0)
        session.add(row)
        session.flush()
    return row


def record_inventory(session: Session, source: str, scope: str, total: int) -> None:
    row = session.scalars(
        select(InventoryRow).where(InventoryRow.source == source, InventoryRow.scope == scope)
    ).first()
    if row is None:
        session.add(InventoryRow(source=source, scope=scope, total=total))
    else:
        row.total = total
        row.counted_at = utcnow()
    session.flush()


def inventory_total(session: Session, source: str, scope: str) -> int | None:
    row = session.scalars(
        select(InventoryRow).where(InventoryRow.source == source, InventoryRow.scope == scope)
    ).first()
    return None if row is None else row.total
