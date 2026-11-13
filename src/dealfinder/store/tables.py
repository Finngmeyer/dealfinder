"""Database tables. SQLite today; nothing here is SQLite-specific, so Postgres works too.

Classes end in ``Row`` to keep them apart from the pydantic models of the same things.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC in Python, stored without a zone so SQLite and Postgres agree."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must carry a time zone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: UtcDateTime,
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class RunRow(Base):
    """One sweep, probe or import."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))  # daily, weekly, probe, import
    status: Mapped[str] = mapped_column(String(24))  # running, completed, stopped_budget, failed
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    report: Mapped[str | None] = mapped_column(Text)


class PropertyRow(Base):
    """A physical property. Listings from every source point at it."""

    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    address_key: Mapped[str | None] = mapped_column(String(240), index=True)
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(80))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(2), index=True)
    zip_code: Mapped[str | None] = mapped_column(String(5), index=True)
    county_fips: Mapped[str | None] = mapped_column(String(5), index=True)
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    geo_cell: Mapped[str | None] = mapped_column(String(24), index=True)  # grid square, ~100 m

    property_kind: Mapped[str] = mapped_column(String(24), default="other")
    unit_count: Mapped[int | None]
    unit_confidence: Mapped[str | None] = mapped_column(String(8))
    bedrooms: Mapped[float | None]
    bathrooms: Mapped[float | None]
    building_sqft: Mapped[float | None]
    lot_sqft: Mapped[float | None]
    year_built: Mapped[int | None]

    # FEMA flood map lookup; all null until stage 2 has checked the property.
    flood_zone: Mapped[str | None] = mapped_column(String(24))
    in_flood_zone: Mapped[bool | None]
    flood_checked_at: Mapped[datetime | None]

    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    starred_at: Mapped[datetime | None]  # price cuts are alerted only from this moment on
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    listings: Mapped[list[ListingRow]] = relationship(back_populates="property")


class ListingRow(Base):
    """One source's listing of a property. ``raw_json`` keeps the record as received."""

    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "source_listing_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    source_listing_id: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(16), index=True)
    price: Mapped[float | None]

    property_type_raw: Mapped[str | None] = mapped_column(String(80))
    source_kind: Mapped[str] = mapped_column(
        String(24), default="other"
    )  # what the source says it is
    unit_count_stated: Mapped[int | None]  # a unit count the source gives as data
    description: Mapped[str | None] = mapped_column(Text)
    hoa_fee_month: Mapped[float | None]
    tax_annual: Mapped[float | None]
    listed_date: Mapped[date | None]
    removed_date: Mapped[date | None]
    days_on_market: Mapped[int | None]
    listing_type: Mapped[str | None] = mapped_column(String(40))
    mls_name: Mapped[str | None] = mapped_column(String(120))
    mls_number: Mapped[str | None] = mapped_column(String(80))
    agent_name: Mapped[str | None] = mapped_column(String(160))
    agent_phone: Mapped[str | None] = mapped_column(String(40))
    office_name: Mapped[str | None] = mapped_column(String(160))
    url: Mapped[str | None] = mapped_column(String(500))
    raw_json: Mapped[dict[str, Any]] = mapped_column(default=dict)

    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    missed_sweeps: Mapped[int] = mapped_column(default=0)  # full sweeps in a row without it

    property: Mapped[PropertyRow] = relationship(back_populates="listings")
    price_events: Mapped[list[PriceEventRow]] = relationship(
        back_populates="listing", order_by="PriceEventRow.id", cascade="all, delete-orphan"
    )


class PriceEventRow(Base):
    """Something that happened to a listing: first seen, a price change, off or back on."""

    __tablename__ = "price_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(default=utcnow)
    event: Mapped[str] = mapped_column(String(24))  # listed, price_change, off_market, relisted
    price: Mapped[float | None]
    previous_price: Mapped[float | None]

    listing: Mapped[ListingRow] = relationship(back_populates="price_events")


class ClassificationRow(Base):
    """What kind of property this is and how many units, with the evidence."""

    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), unique=True)
    property_kind: Mapped[str] = mapped_column(String(24))
    unit_count: Mapped[int | None]
    confidence: Mapped[str] = mapped_column(String(8))
    signals: Mapped[list[Any]] = mapped_column(default=list)
    flags: Mapped[list[Any]] = mapped_column(default=list)
    method: Mapped[str] = mapped_column(String(24))  # "rules", or "rules+llm" once Claude was asked
    version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(32))  # what was classified; unchanged = skip
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UnitRow(Base):
    """One unit of a property, with its rents and where each came from."""

    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("property_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    position: Mapped[int]
    beds: Mapped[float | None]
    baths: Mapped[float | None]
    sqft: Mapped[float | None]
    in_place_rent: Mapped[float | None]  # stated in the listing: an actual figure
    in_place_rent_quote: Mapped[str | None] = mapped_column(Text)  # the listing's words for it
    vacant: Mapped[bool] = mapped_column(Boolean, default=False)
    market_rent: Mapped[float | None]
    market_rent_source: Mapped[str | None] = mapped_column(String(160))
    rent_low: Mapped[float | None]
    rent_high: Mapped[float | None]
    comps: Mapped[list[Any]] = mapped_column(default=list)
    mix_confidence: Mapped[str | None] = mapped_column(String(8))


class MarketDataRow(Base):
    """Market data for a ZIP or county as last loaded, for the dashboard and for reruns."""

    __tablename__ = "market_data"
    __table_args__ = (UniqueConstraint("geography", "geography_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    geography: Mapped[str] = mapped_column(String(8))  # zip or county
    geography_id: Mapped[str] = mapped_column(String(10))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class ScreenRow(Base):
    """One listing's trip through the funnel: whether it could be underwritten, whether
    stage 1 kept it, and the inputs that were used, each with its source."""

    __tablename__ = "screens"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), unique=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    stage: Mapped[int]  # 0 could not be underwritten, 1 free data only, 2 enriched
    passed_stage1: Mapped[bool | None]
    note: Mapped[str | None] = mapped_column(Text)  # why kept, dropped or not underwritten
    type_match: Mapped[str] = mapped_column(String(12))  # yes | unverified
    confidence: Mapped[str | None] = mapped_column(String(8))  # of the unit count
    price: Mapped[float | None]  # the price that was underwritten
    request: Mapped[dict[str, Any]] = mapped_column(default=dict)
    inputs: Mapped[list[Any]] = mapped_column(default=list)
    flags: Mapped[list[Any]] = mapped_column(default=list)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)  # statement, rates, sources
    enrichment: Mapped[dict[str, Any]] = mapped_column(default=dict)  # what stage 2 did
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)

    listing: Mapped[ListingRow] = relationship()


class UnderwritingRow(Base):
    """One listing underwritten under one loan scenario, with its score."""

    __tablename__ = "underwriting"
    __table_args__ = (
        UniqueConstraint("listing_id", "scenario"),
        Index("ix_underwriting_rank", "scenario", "cash_flow_door_month"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    scenario: Mapped[str] = mapped_column(String(40))
    stage: Mapped[int]
    engine_version: Mapped[str] = mapped_column(String(16))
    results: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cash_flow_door_month: Mapped[float | None]
    coc: Mapped[float | None]
    dscr: Mapped[float | None]
    cap_rate: Mapped[float | None]
    mao: Mapped[float | None]
    score: Mapped[float | None]
    grade: Mapped[str | None] = mapped_column(String(1))
    score_parts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    in_buy_box: Mapped[bool | None]
    buy_box_misses: Mapped[list[Any]] = mapped_column(default=list)  # why it is outside
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)


class OverrideRow(Base):
    """The user's own value for one input of one property."""

    __tablename__ = "overrides"
    __table_args__ = (UniqueConstraint("property_id", "field"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    field: Mapped[str] = mapped_column(String(60))
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApiUsageRow(Base):
    """One request to a metered API. The budget guard counts these."""

    __tablename__ = "api_usage"
    __table_args__ = (Index("ix_api_usage_provider_time", "provider", "requested_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str] = mapped_column(String(120))
    requested_at: Mapped[datetime] = mapped_column(default=utcnow)
    billable: Mapped[bool] = mapped_column(Boolean, default=True)
    units: Mapped[int] = mapped_column(default=1)  # requests, or results where billed per result
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))


class AlertRow(Base):
    """An alert that was raised. ``dedupe_key`` stops the same alert going out twice."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None]
    channel: Mapped[str | None] = mapped_column(String(40))


class CheckpointRow(Base):
    """How far a run got through one search, so it can resume at the next page."""

    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "source", "scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(160))  # for example "OH|Multi-Family"
    next_offset: Mapped[int] = mapped_column(default=0)
    total: Mapped[int | None]
    fetched: Mapped[int] = mapped_column(default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class InventoryRow(Base):
    """How many listings a search returned last time, for estimating the next run's cost."""

    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("source", "scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(160))
    total: Mapped[int]
    counted_at: Mapped[datetime] = mapped_column(default=utcnow)
