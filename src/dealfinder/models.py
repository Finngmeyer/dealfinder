"""The canonical listing every source is translated into, and the interface sources implement."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from dealfinder.settings import PropertyType

# What a source says a listing is. "multifamily_2_4" is a building of two to four units whose
# exact count is not known yet; the classifier narrows it to duplex, triplex or fourplex.
PropertyKind = Literal[
    "sfr",
    "condo",
    "townhouse",
    "duplex",
    "triplex",
    "fourplex",
    "multifamily_2_4",
    "multifamily_5plus",
    "manufactured",
    "land",
    "other",
]
ListingStatus = Literal["active", "pending", "off_market", "sold", "unknown"]

# Buy-box property types that can only be told apart once the unit count is known.
SMALL_MULTIFAMILY: frozenset[str] = frozenset({"duplex", "triplex", "fourplex"})


class PriceChange(BaseModel):
    """One dated entry from a source's own listing history."""

    day: date
    event: str
    price: float | None = None


class Listing(BaseModel):
    """A for-sale listing in one shape, whatever source it came from."""

    model_config = ConfigDict(extra="forbid")

    source: str
    source_listing_id: str
    status: ListingStatus = "active"
    price: float | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    county_fips: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    property_kind: PropertyKind = "other"
    property_type_raw: str | None = None  # the source's own wording
    unit_count: int | None = None  # only when the source states it as data
    bedrooms: float | None = None
    bathrooms: float | None = None
    building_sqft: float | None = None
    lot_sqft: float | None = None
    year_built: int | None = None

    description: str | None = None
    hoa_fee_month: float | None = None
    tax_annual: float | None = None  # last property tax bill
    listed_date: date | None = None
    removed_date: date | None = None
    days_on_market: int | None = None
    listing_type: str | None = None  # standard, foreclosure, short sale, new construction
    mls_name: str | None = None
    mls_number: str | None = None
    agent_name: str | None = None
    agent_phone: str | None = None
    office_name: str | None = None
    url: str | None = None
    history: list[PriceChange] = Field(default_factory=list)

    raw: dict[str, Any] = Field(default_factory=dict)  # the source's record, kept as received


class Geo(BaseModel):
    """Where to search. A state is always given; a source may ignore finer filters."""

    state: str  # two-letter abbreviation
    zip_codes: list[str] = Field(default_factory=list)


class CallEstimate(BaseModel):
    """What a search is expected to cost before it runs."""

    calls: int  # billable requests, or results for sources billed per result
    known: bool  # False when no inventory count is on hand and ``calls`` is only a floor
    note: str = ""


class RawPage(BaseModel):
    """One page of raw records from a source, with enough to resume after it."""

    offset: int
    items: list[dict[str, Any]]
    total: int | None = None  # total matches, when the source reports it
    from_cache: bool = False


class ListingSource(Protocol):
    """One place listings come from. Scrapers set ``is_scraper`` and stay off by default."""

    name: str
    env_vars: tuple[str, ...]
    is_scraper: bool

    def estimate_calls(self, geo: Geo, types: Sequence[PropertyType]) -> CallEstimate: ...

    def search(
        self, geo: Geo, types: Sequence[PropertyType], since: date | None
    ) -> Iterator[dict[str, Any]]: ...

    def to_listing(self, raw: dict[str, Any]) -> Listing: ...
