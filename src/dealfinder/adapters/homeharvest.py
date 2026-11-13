"""Optional scraper adapter: the HomeHarvest library, which reads Realtor.com.

OFF BY DEFAULT. Switch it on with ``sources.homeharvest.enabled`` only after reading the
site's terms of use; this is not a licensed feed. The adapter is deliberately gentle:

* one request at a time, with ``scrapers.min_seconds_between_requests`` between searches;
* never a proxy, never a retry that hides who is asking;
* the first refusal (HTTP 403 or a block page) stops the whole run with a plain message.

The library is not installed with the app. Add it with ``uv sync --extra scrapers``.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from typing import Any

from dealfinder.geo import lookup_zip
from dealfinder.http import SourceUnavailable
from dealfinder.models import CallEstimate, Geo, Listing, PropertyKind
from dealfinder.settings import PropertyType, Scrapers

SOURCE = "homeharvest"
# Our property types -> the library's search filters.
SEARCH_TYPES: dict[str, tuple[str, ...]] = {
    "sfr": ("single_family",),
    "condo": ("condos",),
    "townhouse": ("townhomes",),
    "duplex": ("duplex_triplex", "multi_family"),
    "triplex": ("duplex_triplex", "multi_family"),
    "fourplex": ("multi_family",),
    "multifamily_5plus": ("multi_family",),
}
# The library's "style" column -> what the listing is, as far as Realtor.com says.
KINDS: dict[str, PropertyKind] = {
    "SINGLE_FAMILY": "sfr",
    "CONDOS": "condo",
    "CONDO": "condo",
    "CONDOP": "condo",
    "COOP": "condo",
    "CONDO_TOWNHOME": "condo",
    "CONDO_TOWNHOME_ROWHOME_COOP": "condo",
    "TOWNHOMES": "townhouse",
    "MULTI_FAMILY": "multifamily_2_4",
    "DUPLEX_TRIPLEX": "multifamily_2_4",
    "APARTMENT": "multifamily_5plus",
    "LAND": "land",
    "FARM": "land",
    "MOBILE": "manufactured",
}
_STATUSES = {"FOR_SALE": "active", "PENDING": "pending", "CONTINGENT": "pending", "SOLD": "sold"}
_STATE_AT_END = re.compile(r",\s*([A-Za-z]{2})(?:\s+\d{5})?\s*$")
_ZIP = re.compile(r"^\s*(\d{5})\s*$")
_REFUSED = ("403", "forbidden", "blocked", "captcha", "authentication")
Scrape = Callable[..., Any]


def location_state(location: str) -> str | None:
    """The state a search location is in, when it can be told: "Columbus, OH" or a ZIP."""
    found = _ZIP.match(location)
    if found:
        place = lookup_zip(found.group(1))
        return place.state if place else None
    found = _STATE_AT_END.search(location)
    return found.group(1).upper() if found else None


def _clean(value: Any) -> Any:
    """Missing values arrive as NaN or NaT; turn them into None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if type(value).__name__ in ("NaTType", "NAType"):
        return None
    return value


def _number(value: Any) -> float | None:
    value = _clean(value)
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    value = _clean(value)
    text = str(value).strip() if value is not None else ""
    return text or None


def _day(value: Any) -> date | None:
    value = _clean(value)
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _phone(value: Any) -> str | None:
    value = _clean(value)
    if isinstance(value, list | tuple) and value:
        first = value[0]
        return _text(first.get("number") if isinstance(first, dict) else first)
    return _text(value) if isinstance(value, str) else None


def to_listing(raw: dict[str, Any]) -> Listing:
    """One row of the library's table as a canonical listing."""
    identifier = _text(raw.get("property_id")) or _text(raw.get("listing_id"))
    if identifier is None:
        raise ValueError("the row has no property id")
    style = (_text(raw.get("style")) or "").upper()
    full, half = _number(raw.get("full_baths")), _number(raw.get("half_baths"))
    baths = None if full is None and half is None else (full or 0.0) + 0.5 * (half or 0.0)
    year, days, beds = (
        _number(raw.get("year_built")),
        _number(raw.get("days_on_mls")),
        _number(raw.get("beds")),
    )
    status = (_text(raw.get("status")) or "").upper()
    fips = _text(raw.get("fips_code"))
    return Listing(
        source=SOURCE,
        source_listing_id=identifier,
        status=_STATUSES.get(status, "off_market" if status else "active"),  # type: ignore[arg-type]
        price=_number(raw.get("list_price")),
        address_line1=_text(raw.get("street")) or _text(raw.get("full_street_line")),
        address_line2=_text(raw.get("unit")),
        city=_text(raw.get("city")),
        state=_text(raw.get("state")),
        zip_code=(_text(raw.get("zip_code")) or "")[:5] or None,
        county_fips=fips.zfill(5)[:5] if fips else None,
        latitude=_number(raw.get("latitude")),
        longitude=_number(raw.get("longitude")),
        property_kind=KINDS.get(style, "other"),
        property_type_raw=style or None,
        bedrooms=beds,
        bathrooms=baths,
        building_sqft=_number(raw.get("sqft")),
        lot_sqft=_number(raw.get("lot_sqft")),
        year_built=None if year is None else int(year),
        description=_text(raw.get("text")),
        hoa_fee_month=_number(raw.get("hoa_fee")),
        tax_annual=_number(raw.get("tax")),
        listed_date=_day(raw.get("list_date")),
        days_on_market=None if days is None else int(days),
        listing_type="New Construction" if _clean(raw.get("new_construction")) is True else None,
        mls_name=_text(raw.get("mls")),
        mls_number=_text(raw.get("mls_id")),
        agent_name=_text(raw.get("agent_name")),
        agent_phone=_phone(raw.get("agent_phones")),
        office_name=_text(raw.get("office_name")) or _text(raw.get("broker_name")),
        url=_text(raw.get("property_url")),
        raw={key: _jsonable(value) for key, value in raw.items()},
    )


def _jsonable(value: Any) -> Any:
    value = _clean(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


class HomeHarvestSource:
    """Listings from Realtor.com through the HomeHarvest library. A scraper: off by default."""

    name = SOURCE
    env_vars: tuple[str, ...] = ()
    is_scraper = True

    def __init__(
        self,
        settings: Scrapers,
        *,
        max_price: float | None,
        scrape: Scrape | None = None,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._settings = settings
        self._max_price = max_price
        self._scrape = scrape
        self._sleep = sleep
        self._today = today
        self._searched: set[str] = set()
        self._asked_before = False

    def _library(self) -> Scrape:
        if self._scrape is None:
            try:
                from homeharvest import scrape_property
            except ImportError as error:
                raise SourceUnavailable(
                    SOURCE,
                    "the HomeHarvest library is not installed (uv sync --extra scrapers)",
                    missing_key=True,
                ) from error
            self._scrape = scrape_property
        return self._scrape

    def _locations(self, geo: Geo) -> list[str]:
        """Configured locations that belong to this state and have not been searched yet.
        One whose state cannot be told from its text is searched once, with the first state."""
        chosen = []
        for location in self._settings.homeharvest.locations:
            state = location_state(location)
            if location not in self._searched and state in (None, geo.state):
                chosen.append(location)
        return chosen

    def estimate_calls(self, geo: Geo, types: Sequence[PropertyType]) -> CallEstimate:
        if not self._settings.homeharvest.locations:
            return CallEstimate(
                calls=0, known=True, note="no locations set (scrapers.homeharvest.locations)"
            )
        count = len(self._locations(geo))
        return CallEstimate(
            calls=count,
            known=True,
            note=f"{count} searches of Realtor.com, one at a time; free, and not a licensed feed",
        )

    def search(
        self, geo: Geo, types: Sequence[PropertyType], since: date | None
    ) -> Iterator[dict[str, Any]]:
        rule = self._settings.homeharvest
        if not rule.locations:
            raise SourceUnavailable(
                SOURCE,
                "no locations are set: add cities or ZIP codes under "
                "scrapers.homeharvest.locations",
            )
        scrape = self._library()
        wanted = sorted({name for kind in types for name in SEARCH_TYPES.get(kind, ())})
        for location in self._locations(geo):
            if self._asked_before:
                self._sleep(self._settings.min_seconds_between_requests)
            self._asked_before = True
            self._searched.add(location)
            options: dict[str, Any] = {
                "location": location,
                "listing_type": "for_sale",
                "return_type": "pandas",
                "property_type": wanted or None,
                "limit": rule.max_results_per_location,
                "extra_property_data": rule.extra_property_data,
                "parallel": False,  # one request at a time
                # No proxy, ever: if the site says no, the answer is to stop.
            }
            if self._max_price:
                options["price_max"] = int(self._max_price)
            if since is not None:
                options["past_days"] = max(1, (self._today() - since).days)
            try:
                table = scrape(**options)
            except Exception as error:
                words = f"{type(error).__name__} {error}".lower()
                if any(sign in words for sign in _REFUSED):
                    raise SourceUnavailable(
                        SOURCE,
                        "Realtor.com refused the request. Stopping here; wait a day before "
                        "trying again. This adapter never switches addresses to get around it",
                        status=403,
                    ) from error
                raise SourceUnavailable(
                    SOURCE, f"the search for '{location}' failed ({type(error).__name__})"
                ) from error
            records = table.to_dict("records") if hasattr(table, "to_dict") else list(table or [])
            for record in records:
                yield {str(key): value for key, value in record.items()}

    def to_listing(self, raw: dict[str, Any]) -> Listing:
        return to_listing(raw)
