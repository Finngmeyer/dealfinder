"""Optional scraper adapter: Zillow search results through a paid Apify actor.

OFF BY DEFAULT. It needs ``sources.apify_zillow.enabled``, an ``APIFY_TOKEN`` and at least one
Zillow search URL in ``scrapers.apify_zillow.search_urls``. Apify bills every result, so the
adapter counts results against ``monthly_result_cap`` and asks for no more than is left.
Check Zillow's terms of use before switching it on; this is not a licensed feed.

The actor (``maxcopell/zillow-scraper``) takes search URLs that contain ``searchQueryState``
and returns one record per listing with no description and no tax data, so unit counts for
multifamily listings stay unconfirmed until something else confirms them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any

from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.models import CallEstimate, Geo, Listing, PropertyKind
from dealfinder.settings import ApifyZillowSettings, PropertyType
from dealfinder.store.usage import BudgetExceeded, BudgetGuard

SOURCE = "apify_zillow"
API = "https://api.apify.com/v2/acts"
RUN_TIMEOUT_SECONDS = 330  # the synchronous endpoint waits up to five minutes
KINDS: dict[str, PropertyKind] = {
    "SINGLE_FAMILY": "sfr",
    "CONDO": "condo",
    "TOWNHOUSE": "townhouse",
    "MULTI_FAMILY": "multifamily_2_4",
    "MANUFACTURED": "manufactured",
    "LOT": "land",
}
_STATUSES = {"forsale": "active", "pending": "pending", "sold": "sold"}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _part(record: dict[str, Any], name: str) -> dict[str, Any]:
    value = record.get(name)
    return value if isinstance(value, dict) else {}


def parse_items(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise SourceUnavailable(SOURCE, "the response was not JSON") from error
    if not isinstance(data, list):
        raise SourceUnavailable(SOURCE, "the response was not a list of results")
    return [item for item in data if isinstance(item, dict) and item.get("zpid")]


def to_listing(raw: dict[str, Any]) -> Listing:
    address, kinds = _part(raw, "listingAddress"), _part(raw, "listingType")
    where, price = _part(raw, "coordinates"), _part(raw, "listingPrice")
    home_type = (_text(raw.get("homeType")) or "").upper()
    status = (_text(raw.get("listingStatus")) or "").lower()
    if kinds.get("isPending"):
        status = "pending"
    distress = (
        "Foreclosure"
        if kinds.get("isForeclosure") or kinds.get("isBankOwned")
        else "Auction"
        if kinds.get("isForAuction")
        else None
    )
    area = _number(raw.get("livingArea"))
    days = _number(raw.get("daysOnZillow"))
    return Listing(
        source=SOURCE,
        source_listing_id=str(raw["zpid"]),
        status=_STATUSES.get(status, "active"),  # type: ignore[arg-type]
        price=_number(price.get("amount")),
        address_line1=_text(address.get("street")),
        address_line2=_text(address.get("unit")),
        city=_text(address.get("city")),
        state=_text(address.get("state")),
        zip_code=(_text(address.get("zipCode")) or "")[:5] or None,
        latitude=_number(where.get("latitude")),
        longitude=_number(where.get("longitude")),
        property_kind=KINDS.get(home_type, "other"),
        property_type_raw=home_type or None,
        bedrooms=_number(raw.get("bedrooms")),
        bathrooms=_number(raw.get("bathrooms")),
        building_sqft=area if raw.get("livingAreaUnit") in (None, "sqft") else None,
        days_on_market=None if days is None else int(days),
        listing_type=distress,
        office_name=_text(_part(raw, "broker").get("name")),
        url=_text(raw.get("propertyUrl")),
        raw=raw,
    )


class ApifyZillowSource:
    """Zillow search results through Apify. A scraper, and a paid one: off by default."""

    name = SOURCE
    env_vars: tuple[str, ...] = ("APIFY_TOKEN",)
    is_scraper = True

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        token: str | None,
        settings: ApifyZillowSettings,
        guard: BudgetGuard,
    ) -> None:
        self._fetcher = fetcher
        self._token = token
        self._settings = settings
        self._guard = guard
        self._done = False
        self.results_billed = 0

    def estimate_calls(self, geo: Geo, types: Sequence[PropertyType]) -> CallEstimate:
        rule = self._settings
        if not rule.search_urls:
            return CallEstimate(
                calls=0, known=True, note="no search URLs set (scrapers.apify_zillow.search_urls)"
            )
        most = len(rule.search_urls) * rule.results_limit
        left = self._guard.remaining()
        if left is not None:
            most = min(most, left)
        return CallEstimate(
            calls=most,
            known=False,
            note=f"up to {most:,} results at ${rule.price_per_result:.4f} each: "
            f"at most ${most * rule.price_per_result:,.2f}",
        )

    def search(
        self, geo: Geo, types: Sequence[PropertyType], since: date | None
    ) -> Iterator[dict[str, Any]]:
        """Every configured search, once per run whatever the state asked for: a Zillow URL
        carries its own area and filters."""
        if self._done:
            return
        self._done = True
        rule = self._settings
        if not self._token:
            raise SourceUnavailable(SOURCE, "APIFY_TOKEN is not set", missing_key=True)
        if not rule.search_urls:
            raise SourceUnavailable(
                SOURCE, "no search URLs are set (scrapers.apify_zillow.search_urls)"
            )
        for url in rule.search_urls:
            if "searchQueryState" not in url:
                raise SourceUnavailable(
                    SOURCE,
                    "a search URL has no searchQueryState in it: on Zillow, move the map a "
                    "little and copy the address again",
                )
            left = self._guard.remaining()
            if left is not None and left <= 0:
                raise BudgetExceeded(SOURCE, self._guard.used(), self._guard.cap or 0, 1)
            limit = rule.results_limit if left is None else min(rule.results_limit, left)
            response = self._fetcher.post_json(
                SOURCE,
                f"{API}/{rule.actor}/run-sync-get-dataset-items",
                {
                    "searchUrls": [{"url": url}],
                    "extractionMethod": rule.extraction_method,
                    "resultsLimit": limit,
                },
                headers={"Authorization": f"Bearer {self._token}"},
                timeout_seconds=RUN_TIMEOUT_SECONDS,
            )
            items = parse_items(response.content)
            if items:
                self._guard.record("zillow-scraper", units=len(items))
                self.results_billed += len(items)
            yield from items

    def to_listing(self, raw: dict[str, Any]) -> Listing:
        return to_listing(raw)
