"""RentCast sale listings, a licensed API.

Every successful request is billed and RentCast has no cap of its own, so each request passes
the budget guard just before it is sent and is recorded straight after. A page that is still in
the on-disk cache costs nothing and is not counted.

Listings carry no description, unit count or tax bill. A "Multi-Family" listing is a building
of two to four units; which of those it is has to come from elsewhere.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.models import CallEstimate, Geo, Listing, PriceChange, PropertyKind, RawPage
from dealfinder.settings import PropertyType, Sweep
from dealfinder.store.usage import BudgetGuard

SOURCE = "rentcast"
BASE_URL = "https://api.rentcast.io/v1"
LISTINGS_URL = f"{BASE_URL}/listings/sale"
PROPERTIES_URL = f"{BASE_URL}/properties"
RENT_URL = f"{BASE_URL}/avm/rent/long-term"
TOTAL_HEADER = "X-Total-Count"

# Buy-box property type -> RentCast's propertyType.
RAW_TYPES: dict[str, str] = {
    "sfr": "Single Family",
    "condo": "Condo",
    "townhouse": "Townhouse",
    "duplex": "Multi-Family",
    "triplex": "Multi-Family",
    "fourplex": "Multi-Family",
    "multifamily_5plus": "Apartment",
}
# RentCast's propertyType -> what the listing is, as far as RentCast can say.
KINDS: dict[str, PropertyKind] = {
    "Single Family": "sfr",
    "Condo": "condo",
    "Townhouse": "townhouse",
    "Manufactured": "manufactured",
    "Multi-Family": "multifamily_2_4",
    "Apartment": "multifamily_5plus",
    "Land": "land",
}
# What to call a property kind when asking for a rent estimate of one of its units.
RENT_TYPES: dict[str, str] = {
    "sfr": "Single Family",
    "condo": "Condo",
    "townhouse": "Townhouse",
    "manufactured": "Manufactured",
    "duplex": "Multi-Family",
    "triplex": "Multi-Family",
    "fourplex": "Multi-Family",
    "multifamily_2_4": "Multi-Family",
    "multifamily_5plus": "Apartment",
}
_STATUSES = {"active": "active", "inactive": "off_market"}


def raw_types(types: Sequence[PropertyType]) -> list[str]:
    """RentCast property types covering the buy-box types, without repeats."""
    return sorted({RAW_TYPES[kind] for kind in types})


def _day(value: Any) -> date | None:
    """Date from RentCast's ISO timestamps such as '2024-06-24T00:00:00.000Z'."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def parse_page(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise SourceUnavailable(SOURCE, "the response was not JSON") from error
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise SourceUnavailable(SOURCE, "the response was not a list of listings")
    return data


def to_listing(raw: dict[str, Any]) -> Listing:
    """Translate one RentCast sale listing."""
    listing_id = _text(raw.get("id"))
    if listing_id is None:
        raise ValueError("a RentCast listing without an id")
    raw_type = _text(raw.get("propertyType"))
    state_fips, county_code = _text(raw.get("stateFips")), _text(raw.get("countyFips"))
    agent = raw.get("listingAgent") if isinstance(raw.get("listingAgent"), dict) else {}
    office = raw.get("listingOffice") if isinstance(raw.get("listingOffice"), dict) else {}
    hoa = raw.get("hoa") if isinstance(raw.get("hoa"), dict) else {}
    history = raw.get("history") if isinstance(raw.get("history"), dict) else {}
    year_built = _number(raw.get("yearBuilt"))
    days_on_market = _number(raw.get("daysOnMarket"))
    return Listing(
        source=SOURCE,
        source_listing_id=listing_id,
        status=_STATUSES.get(str(raw.get("status", "")).lower(), "unknown"),  # type: ignore[arg-type]
        price=_number(raw.get("price")),
        address_line1=_text(raw.get("addressLine1")),
        address_line2=_text(raw.get("addressLine2")),
        city=_text(raw.get("city")),
        state=_text(raw.get("state")),
        zip_code=_text(raw.get("zipCode")),
        county_fips=(
            state_fips.zfill(2) + county_code.zfill(3) if state_fips and county_code else None
        ),
        latitude=_number(raw.get("latitude")),
        longitude=_number(raw.get("longitude")),
        property_kind=KINDS.get(raw_type or "", "other"),
        property_type_raw=raw_type,
        bedrooms=_number(raw.get("bedrooms")),
        bathrooms=_number(raw.get("bathrooms")),
        building_sqft=_number(raw.get("squareFootage")),
        lot_sqft=_number(raw.get("lotSize")),
        year_built=None if year_built is None else int(year_built),
        hoa_fee_month=_number(hoa.get("fee")),
        listed_date=_day(raw.get("listedDate")),
        removed_date=_day(raw.get("removedDate")),
        days_on_market=None if days_on_market is None else int(days_on_market),
        listing_type=_text(raw.get("listingType")),
        mls_name=_text(raw.get("mlsName")),
        mls_number=_text(raw.get("mlsNumber")),
        agent_name=_text(agent.get("name")),
        agent_phone=_text(agent.get("phone")),
        office_name=_text(office.get("name")),
        history=[
            PriceChange(
                day=day, event=str(entry.get("event", "")), price=_number(entry.get("price"))
            )
            for key, entry in sorted(history.items())
            if isinstance(entry, dict) and (day := _day(key)) is not None
        ],
        raw=raw,
    )


class PropertyRecord(BaseModel):
    """What RentCast's property record adds to a listing: the unit count and the tax bill."""

    property_id: str
    unit_count: int | None
    tax_annual: float | None  # the most recent year's property tax
    tax_year: int | None
    owner_occupied: bool | None
    property_type_raw: str | None
    bedrooms: float | None
    bathrooms: float | None
    building_sqft: float | None
    year_built: int | None


def parse_property(body: bytes) -> PropertyRecord:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise SourceUnavailable(SOURCE, "the property record was not JSON") from error
    if isinstance(data, list):  # the address search returns a list; the id lookup, one object
        data = data[0] if data else None
    if not isinstance(data, dict) or not _text(data.get("id")):
        raise SourceUnavailable(SOURCE, "the response was not a property record")
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    taxes = data.get("propertyTaxes") if isinstance(data.get("propertyTaxes"), dict) else {}
    bills = sorted(
        (int(year), _number(entry.get("total")))
        for year, entry in taxes.items()
        if str(year).isdigit() and isinstance(entry, dict) and _number(entry.get("total"))
    )
    units = _number(features.get("unitCount"))
    year_built = _number(data.get("yearBuilt"))
    owner = data.get("ownerOccupied")
    return PropertyRecord(
        property_id=str(data["id"]),
        unit_count=int(units) if units and units >= 1 else None,
        tax_annual=bills[-1][1] if bills else None,
        tax_year=bills[-1][0] if bills else None,
        owner_occupied=owner if isinstance(owner, bool) else None,
        property_type_raw=_text(data.get("propertyType")),
        bedrooms=_number(data.get("bedrooms")),
        bathrooms=_number(data.get("bathrooms")),
        building_sqft=_number(data.get("squareFootage")),
        year_built=None if year_built is None else int(year_built),
    )


class RentComp(BaseModel):
    address: str | None
    rent: float | None
    bedrooms: float | None
    bathrooms: float | None
    sqft: float | None
    distance_miles: float | None
    days_old: int | None
    correlation: float | None  # RentCast's 0-1 similarity to the subject


class RentEstimate(BaseModel):
    """RentCast's rent estimate for ONE unit of the given layout, with its comparables."""

    rent: float
    low: float | None
    high: float | None
    comps: list[RentComp]

    def describe(self) -> str:
        spread = (
            f" (range ${self.low:,.0f} to ${self.high:,.0f})"
            if self.low is not None and self.high is not None
            else ""
        )
        return f"RentCast rent estimate from {len(self.comps)} comparable rentals{spread}"


def parse_rent_estimate(body: bytes) -> RentEstimate:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise SourceUnavailable(SOURCE, "the rent estimate was not JSON") from error
    rent = _number(data.get("rent")) if isinstance(data, dict) else None
    if rent is None or rent <= 0:
        raise SourceUnavailable(SOURCE, "the response held no rent estimate")
    comps = data.get("comparables") if isinstance(data.get("comparables"), list) else []
    return RentEstimate(
        rent=rent,
        low=_number(data.get("rentRangeLow")),
        high=_number(data.get("rentRangeHigh")),
        comps=[
            RentComp(
                address=_text(comp.get("formattedAddress")),
                rent=_number(comp.get("price")),
                bedrooms=_number(comp.get("bedrooms")),
                bathrooms=_number(comp.get("bathrooms")),
                sqft=_number(comp.get("squareFootage")),
                distance_miles=_number(comp.get("distance")),
                days_old=None if _number(comp.get("daysOld")) is None else int(comp["daysOld"]),
                correlation=_number(comp.get("correlation")),
            )
            for comp in comps
            if isinstance(comp, dict)
        ],
    )


class RentCastSource:
    """The RentCast listing source."""

    name = SOURCE
    env_vars: tuple[str, ...] = ("RENTCAST_API_KEY",)
    is_scraper = False

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        api_key: str | None,
        guard: BudgetGuard,
        sweep: Sweep,
        max_price: float | None,
        inventory: Callable[[str], int | None] = lambda scope: None,
        today: Callable[[], date] = lambda: datetime.now().date(),
        record_cache_days: float = 180,
    ) -> None:
        self._record_cache = timedelta(days=record_cache_days)
        self.requests_billed = 0  # billed requests made through this object
        self._fetcher = fetcher
        self._api_key = api_key
        self._guard = guard
        self._sweep = sweep
        self._max_price = max_price if sweep.filter_by_max_price else None
        self._inventory = inventory
        self._today = today

    def scope(self, geo: Geo, types: Sequence[PropertyType]) -> str:
        """A label for one search: state, property types and the price filter. Checkpoints and
        inventory counts are kept per scope."""
        price = f"|<={self._max_price:.0f}" if self._max_price else ""
        return f"{geo.state}|{'|'.join(raw_types(types))}{price}"

    def _query(self, geo: Geo, types: Sequence[PropertyType], since: date | None) -> dict[str, str]:
        query = {
            "state": geo.state,
            "propertyType": "|".join(raw_types(types)),
            "status": "Active",
            "limit": str(self._sweep.page_size),
        }
        if self._max_price:
            query["price"] = f"*:{self._max_price:.0f}"
        if since is not None:
            query["daysOld"] = f"*:{max(1, (self._today() - since).days)}"
        return query

    def rent_estimate(
        self,
        *,
        address: str,
        kind: str,
        bedrooms: float,
        bathrooms: float | None,
        sqft: float | None,
        comp_count: int,
        cache_days: float,
    ) -> RentEstimate:
        """RentCast's long-term rent estimate for one unit of this layout at this address.

        For a multifamily building RentCast prices a single unit, and only reliably when it is
        told the unit's own bedrooms, bathrooms and size and told not to look the building up:
        left to itself it fills the gaps with the whole building's totals.

        One billed request unless a copy is still cached. Raises BudgetExceeded past the cap
        and SourceUnavailable when there is no key or no estimate.
        """
        if not self._api_key:
            raise SourceUnavailable(SOURCE, "RENTCAST_API_KEY is not set", missing_key=True)
        params = {
            "address": address,
            "propertyType": RENT_TYPES.get(kind, "Multi-Family"),
            "bedrooms": f"{bedrooms:g}",
            "compCount": str(comp_count),
            "lookupSubjectAttributes": "false",
        }
        if bathrooms is not None:
            params["bathrooms"] = f"{bathrooms:g}"
        if sqft is not None:
            params["squareFootage"] = f"{sqft:.0f}"
        fetched = self._fetcher.get(
            SOURCE,
            RENT_URL,
            params=params,
            headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
            max_age=timedelta(days=cache_days),
            validate=parse_rent_estimate,
            before_send=self._guard.check,
            on_success=lambda: self._billed("avm/rent/long-term"),
        )
        return parse_rent_estimate(fetched.body)

    def _billed(self, endpoint: str) -> None:
        """RentCast bills every HTTP 200, so each one is counted the moment it arrives."""
        self._guard.record(endpoint)
        self.requests_billed += 1

    def pages(
        self,
        geo: Geo,
        types: Sequence[PropertyType],
        since: date | None = None,
        *,
        start_offset: int = 0,
        max_pages: int | None = None,
    ) -> Iterator[RawPage]:
        """Pages of raw listings, newest first, starting at ``start_offset``.

        Raises BudgetExceeded when the next request would go past the cap, and
        SourceUnavailable when RentCast cannot be reached or has no key.
        """
        if not self._api_key:
            raise SourceUnavailable(SOURCE, "RENTCAST_API_KEY is not set", missing_key=True)
        query = self._query(geo, types, since)
        size = self._sweep.page_size
        offset = start_offset
        fetched_pages = 0
        while max_pages is None or fetched_pages < max_pages:
            params = {**query, "offset": str(offset)}
            if offset == 0:
                params["includeTotalCount"] = "true"  # slow, so only asked for once
            fetched = self._fetcher.get(
                SOURCE,
                LISTINGS_URL,
                params=params,
                headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
                max_age=timedelta(hours=self._sweep.page_cache_hours),
                validate=parse_page,
                before_send=self._guard.check,
                on_success=lambda: self._billed("listings/sale"),
                keep_headers=(TOTAL_HEADER,),
            )
            if fetched.stale:
                raise SourceUnavailable(SOURCE, "RentCast could not be reached for a fresh page")
            items = parse_page(fetched.body)
            total = fetched.headers.get(TOTAL_HEADER, "")
            yield RawPage(
                offset=offset,
                items=items,
                total=int(total) if total.isdigit() else None,
                from_cache=fetched.from_cache,
            )
            fetched_pages += 1
            if len(items) < size:
                return
            offset += size

    def search(
        self, geo: Geo, types: Sequence[PropertyType], since: date | None
    ) -> Iterator[dict[str, Any]]:
        for page in self.pages(geo, types, since):
            yield from page.items

    def estimate_calls(self, geo: Geo, types: Sequence[PropertyType]) -> CallEstimate:
        """Requests a full search of this state would take, from the last count on hand."""
        total = self._inventory(self.scope(geo, types))
        if total is None:
            return CallEstimate(calls=1, known=False, note="no count yet: at least one request")
        # A full page means "ask again", so an exact multiple of the page size costs one extra.
        calls = total // self._sweep.page_size + 1
        return CallEstimate(
            calls=calls,
            known=True,
            note=f"{total:,} listings last counted, {math.ceil(total / self._sweep.page_size)} "
            "full or partial pages",
        )

    def to_listing(self, raw: dict[str, Any]) -> Listing:
        return to_listing(raw)

    def property_record(self, listing_id: str) -> PropertyRecord:
        """The property record behind a listing, which shares the listing's id.

        One billed request unless a copy is still cached. Raises BudgetExceeded past the cap,
        and SourceUnavailable when there is no key or RentCast has no record for the id.
        """
        if not self._api_key:
            raise SourceUnavailable(SOURCE, "RENTCAST_API_KEY is not set", missing_key=True)
        fetched = self._fetcher.get(
            SOURCE,
            f"{PROPERTIES_URL}/{quote(listing_id, safe='')}",
            headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
            max_age=self._record_cache,
            validate=parse_property,
            before_send=self._guard.check,
            on_success=lambda: self._billed("properties"),
        )
        return parse_property(fetched.body)
