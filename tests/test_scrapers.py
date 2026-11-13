"""The optional scraper adapters: off by default, gentle, and stopped by the first refusal.

Nothing here reaches Realtor.com, Zillow or Apify: the HomeHarvest library is replaced by a
function, and Apify's API is mocked.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pandas as pd
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from dealfinder.adapters import apify_zillow, homeharvest
from dealfinder.adapters.apify_zillow import ApifyZillowSource
from dealfinder.adapters.homeharvest import HomeHarvestSource, location_state
from dealfinder.cli import app
from dealfinder.http import SourceUnavailable
from dealfinder.models import Geo
from dealfinder.pipeline.ingest import ingest
from dealfinder.pipeline.underwrite import source_label
from dealfinder.store.db import session_scope
from dealfinder.store.tables import ListingRow, PropertyRow
from dealfinder.store.usage import BudgetGuard

OH = Geo(state="OH")
FOURPLEX = ["fourplex"]
ZILLOW_URL = "https://www.zillow.com/homes/for_sale/?searchQueryState=%7B%22a%22%3A1%7D"
RUN_URL = f"{apify_zillow.API}/maxcopell~zillow-scraper/run-sync-get-dataset-items"


def realtor_row(number: int = 1, **changes) -> dict:
    """One row of the HomeHarvest table, with the gaps it really has."""
    row = {
        "property_url": f"https://www.realtor.com/realestateandhomes-detail/{number}",
        "property_id": f"R{number:06d}",
        "listing_id": f"L{number}",
        "mls": "SAMP",
        "mls_id": f"M{number}",
        "status": "FOR_SALE",
        "text": "Quadplex with separate meters. All units rented at $1,200/mo.",
        "style": "MULTI_FAMILY",
        "full_street_line": f"{100 + number} Sample St",
        "street": f"{100 + number} Sample St",
        "unit": float("nan"),
        "city": "Columbus",
        "state": "OH",
        "zip_code": "43215",
        "beds": 8,
        "full_baths": 4,
        "half_baths": 1,
        "sqft": 3600,
        "year_built": 1978.0,
        "days_on_mls": 42,
        "list_price": 400_000 + number,
        "list_date": pd.Timestamp("2026-10-01"),
        "tax": 5760,
        "lot_sqft": 6000,
        "latitude": 39.96 + number * 0.01,
        "longitude": -83.0,
        "fips_code": "39049",
        "hoa_fee": float("nan"),
        "new_construction": False,
        "agent_name": "Sam Sample",
        "agent_phones": [{"number": "5550100", "type": "Office"}],
        "office_name": "Sample Realty",
        "last_sold_date": pd.NaT,
    }
    row.update(changes)
    return row


class Scrape:
    """Stands in for homeharvest.scrape_property and remembers how it was called."""

    def __init__(self, tables=None, error: Exception | None = None) -> None:
        self.tables, self.error, self.calls = tables or {}, error, []

    def __call__(self, **options):
        self.calls.append(options)
        if self.error:
            raise self.error
        return pd.DataFrame(self.tables.get(options["location"], []))


def realtor(config, scrape, locations, **options) -> tuple[HomeHarvestSource, list[float]]:
    config.settings.scrapers.homeharvest.locations = locations
    pauses: list[float] = []
    source = HomeHarvestSource(
        config.settings.scrapers,
        max_price=config.settings.buy_box.max_price,
        scrape=scrape,
        sleep=pauses.append,
        today=lambda: date(2026, 11, 13),
        **options,
    )
    return source, pauses


class TestHomeHarvest:
    def test_a_row_becomes_a_listing(self):
        listing = homeharvest.to_listing(realtor_row(1))
        assert (listing.source, listing.source_listing_id) == ("homeharvest", "R000001")
        assert (listing.status, listing.price) == ("active", 400_001.0)
        assert (listing.address_line1, listing.address_line2) == ("101 Sample St", None)
        assert (listing.zip_code, listing.county_fips) == ("43215", "39049")
        assert (
            listing.property_kind == "multifamily_2_4"
            and listing.property_type_raw == "MULTI_FAMILY"
        )
        assert (listing.bedrooms, listing.bathrooms) == (8.0, 4.5)  # four full and a half
        assert listing.description.startswith("Quadplex") and listing.tax_annual == 5760.0
        assert listing.listed_date == date(2026, 10, 1) and listing.days_on_market == 42
        assert listing.hoa_fee_month is None and listing.year_built == 1978
        assert (listing.agent_phone, listing.mls_number) == ("5550100", "M1")
        assert listing.raw["last_sold_date"] is None and listing.raw["unit"] is None
        json.dumps(listing.raw)  # storable as it is

    @pytest.mark.parametrize(
        ("style", "kind"),
        [
            ("SINGLE_FAMILY", "sfr"),
            ("CONDOS", "condo"),
            ("TOWNHOMES", "townhouse"),
            ("DUPLEX_TRIPLEX", "multifamily_2_4"),
            ("APARTMENT", "multifamily_5plus"),
            ("LAND", "land"),
            ("MOBILE", "manufactured"),
            ("SOMETHING_NEW", "other"),
            (float("nan"), "other"),
        ],
    )
    def test_kinds(self, style, kind):
        assert homeharvest.to_listing(realtor_row(style=style)).property_kind == kind

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("FOR_SALE", "active"),
            ("PENDING", "pending"),
            ("SOLD", "sold"),
            ("OFF_MARKET", "off_market"),
        ],
    )
    def test_statuses(self, status, expected):
        assert homeharvest.to_listing(realtor_row(status=status)).status == expected

    def test_a_row_without_an_id_is_refused(self):
        with pytest.raises(ValueError, match="no property id"):
            homeharvest.to_listing(realtor_row(property_id=float("nan"), listing_id=None))

    @pytest.mark.parametrize(
        ("location", "state"),
        [
            ("Columbus, OH", "OH"),
            ("columbus, oh", "OH"),
            ("123 Main St, Dayton, OH 45402", "OH"),
            ("43215", "OH"),
            ("46204", "IN"),
            ("Franklin County", None),
            ("00000", None),
        ],
    )
    def test_location_state(self, location, state):
        assert location_state(location) == state

    def test_search_is_gentle_and_never_uses_a_proxy(self, config):
        scrape = Scrape(
            {"Columbus, OH": [realtor_row(1), realtor_row(2)], "43206": [realtor_row(3)]}
        )
        source, pauses = realtor(config, scrape, ["Columbus, OH", "43206", "Fort Wayne, IN"])
        rows = list(source.search(OH, FOURPLEX, None))
        assert [row["property_id"] for row in rows] == ["R000001", "R000002", "R000003"]
        assert [call["location"] for call in scrape.calls] == ["Columbus, OH", "43206"]
        first = scrape.calls[0]
        assert first["listing_type"] == "for_sale" and first["property_type"] == ["multi_family"]
        assert first["parallel"] is False and first["extra_property_data"] is False
        assert first["price_max"] == 750_000 and first["limit"] == 400
        assert "proxy" not in first and "past_days" not in first
        assert pauses == [10]  # one pause, between the two searches
        # A second pass over the same state does not ask again; Indiana gets its own turn.
        assert list(source.search(OH, FOURPLEX, None)) == []
        list(source.search(Geo(state="IN"), FOURPLEX, None))
        assert scrape.calls[-1]["location"] == "Fort Wayne, IN" and len(pauses) == 2

    def test_daily_mode_and_type_filters(self, config):
        scrape = Scrape()
        source, _ = realtor(config, scrape, ["Columbus, OH"])
        list(source.search(OH, ["duplex", "sfr"], date(2026, 11, 11)))
        call = scrape.calls[0]
        assert call["past_days"] == 2
        assert call["property_type"] == ["duplex_triplex", "multi_family", "single_family"]

    def test_a_refusal_stops_everything(self, config):
        scrape = Scrape(error=RuntimeError("Received 403 Forbidden from realtor.com"))
        source, _ = realtor(config, scrape, ["Columbus, OH", "Dayton, OH"])
        with pytest.raises(SourceUnavailable, match=r"Realtor\.com refused the request") as caught:
            list(source.search(OH, FOURPLEX, None))
        assert caught.value.status == 403 and len(scrape.calls) == 1

    def test_other_failures_name_the_search(self, config):
        source, _ = realtor(config, Scrape(error=KeyError("data")), ["Columbus, OH"])
        with pytest.raises(SourceUnavailable, match="the search for 'Columbus, OH' failed"):
            list(source.search(OH, FOURPLEX, None))

    def test_no_locations_and_no_library(self, config):
        source, _ = realtor(config, Scrape(), [])
        with pytest.raises(SourceUnavailable, match="no locations are set"):
            list(source.search(OH, FOURPLEX, None))
        assert source.estimate_calls(OH, FOURPLEX).calls == 0
        missing, _ = realtor(config, None, ["Columbus, OH"])
        with pytest.raises(SourceUnavailable, match="not installed") as caught:
            list(missing.search(OH, FOURPLEX, None))
        assert caught.value.missing_key

    def test_estimate(self, config):
        source, _ = realtor(config, Scrape(), ["Columbus, OH", "Dayton, OH", "Gary, IN"])
        estimate = source.estimate_calls(OH, FOURPLEX)
        assert estimate.calls == 2 and "not a licensed feed" in estimate.note
        assert source.is_scraper and source.name == "homeharvest" and source.env_vars == ()


def zillow_item(number: int = 1, **changes) -> dict:
    item = {
        "zpid": str(15_000_000 + number),
        "listingPrice": {"amount": 300_000 + number, "currency": "USD"},
        "listingAddress": {
            "street": f"{200 + number} Example Ave",
            "unit": None,
            "city": "Columbus",
            "state": "OH",
            "zipCode": "43206",
        },
        "coordinates": {"latitude": 39.94, "longitude": -82.97 - number * 0.001},
        "listingStatus": "forSale",
        "propertyUrl": f"https://www.zillow.com/homedetails/{number}_zpid/",
        "homeType": "MULTI_FAMILY",
        "listingType": {"isForeclosure": False, "isForAuction": False, "isPending": None},
        "bedrooms": 8,
        "bathrooms": 4,
        "livingArea": 3400,
        "livingAreaUnit": "sqft",
        "daysOnZillow": 12,
        "broker": {"name": "Example Realty"},
    }
    item.update(changes)
    return item


def zillow(engine, config, fetcher, *, token="apify-token", cap=1000, urls=(ZILLOW_URL,)):
    rule = config.settings.scrapers.apify_zillow
    rule.search_urls = list(urls)
    guard = BudgetGuard(engine, "apify", cap=cap)
    return ApifyZillowSource(fetcher, token=token, settings=rule, guard=guard), guard


class TestApifyZillow:
    def test_an_item_becomes_a_listing(self):
        listing = apify_zillow.to_listing(zillow_item(1))
        assert (listing.source, listing.source_listing_id) == ("apify_zillow", "15000001")
        assert (listing.price, listing.status) == (300_001.0, "active")
        assert (listing.address_line1, listing.zip_code) == ("201 Example Ave", "43206")
        assert listing.property_kind == "multifamily_2_4" and listing.building_sqft == 3400.0
        assert listing.days_on_market == 12 and listing.office_name == "Example Realty"
        assert listing.description is None and listing.tax_annual is None

    def test_distress_and_status(self):
        bank = zillow_item(listingType={"isBankOwned": True})
        assert apify_zillow.to_listing(bank).listing_type == "Foreclosure"
        auction = zillow_item(listingType={"isForAuction": True, "isPending": True})
        listing = apify_zillow.to_listing(auction)
        assert (listing.listing_type, listing.status) == ("Auction", "pending")
        assert apify_zillow.to_listing(zillow_item(homeType="LOT")).property_kind == "land"
        acres = zillow_item(livingArea=2.5, livingAreaUnit="acres")
        assert apify_zillow.to_listing(acres).building_sqft is None

    def test_search_runs_the_actor_and_counts_what_it_is_billed(
        self, engine, config, fetcher, http_mock
    ):
        route = http_mock.post(RUN_URL).mock(
            return_value=httpx.Response(201, json=[zillow_item(1), zillow_item(2), {"note": "x"}])
        )
        source, guard = zillow(engine, config, fetcher)
        items = list(source.search(OH, FOURPLEX, None))
        assert [item["zpid"] for item in items] == ["15000001", "15000002"]
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer apify-token"
        assert "apify-token" not in str(request.url)  # the token is never in the address
        assert json.loads(request.content) == {
            "searchUrls": [{"url": ZILLOW_URL}],
            "extractionMethod": "PAGINATION_WITH_ZOOM_IN",
            "resultsLimit": 200,
        }
        assert guard.used() == 2 and source.results_billed == 2
        # The searches run once per run, whatever states are asked for afterwards.
        assert list(source.search(Geo(state="IN"), FOURPLEX, None)) == []
        assert route.call_count == 1

    def test_the_cap_limits_what_is_asked_for_and_then_stops(
        self, engine, config, fetcher, http_mock
    ):
        route = http_mock.post(RUN_URL).mock(
            return_value=httpx.Response(201, json=[zillow_item(n) for n in range(1, 4)])
        )
        source, guard = zillow(engine, config, fetcher, cap=3, urls=(ZILLOW_URL, ZILLOW_URL + "2"))
        from dealfinder.store.usage import BudgetExceeded

        with pytest.raises(BudgetExceeded):
            list(source.search(OH, FOURPLEX, None))
        assert json.loads(route.calls[0].request.content)["resultsLimit"] == 3
        assert route.call_count == 1 and guard.used() == 3  # the second search never ran

    def test_what_stops_it_before_any_request(self, engine, config, fetcher):
        no_token, _ = zillow(engine, config, fetcher, token=None)
        with pytest.raises(SourceUnavailable, match="APIFY_TOKEN is not set"):
            list(no_token.search(OH, FOURPLEX, None))
        no_urls, _ = zillow(engine, config, fetcher, urls=())
        with pytest.raises(SourceUnavailable, match="no search URLs are set"):
            list(no_urls.search(OH, FOURPLEX, None))
        bad_url, _ = zillow(
            engine, config, fetcher, urls=("https://www.zillow.com/homes/43215_rb/",)
        )
        with pytest.raises(SourceUnavailable, match="no searchQueryState"):
            list(bad_url.search(OH, FOURPLEX, None))

    def test_api_errors(self, engine, config, fetcher, http_mock):
        http_mock.post(RUN_URL).mock(return_value=httpx.Response(401))
        source, guard = zillow(engine, config, fetcher)
        with pytest.raises(SourceUnavailable, match="key or address was not accepted"):
            list(source.search(OH, FOURPLEX, None))
        assert guard.used() == 0
        http_mock.post(RUN_URL).mock(return_value=httpx.Response(201, json={"error": "x"}))
        again, _ = zillow(engine, config, fetcher)
        with pytest.raises(SourceUnavailable, match="not a list of results"):
            list(again.search(OH, FOURPLEX, None))

    def test_estimate(self, engine, config, fetcher):
        source, _ = zillow(engine, config, fetcher, cap=150)
        estimate = source.estimate_calls(OH, FOURPLEX)
        assert estimate.calls == 150 and not estimate.known
        assert estimate.note == "up to 150 results at $0.0023 each: at most $0.34"
        empty, _ = zillow(engine, config, fetcher, urls=())
        assert empty.estimate_calls(OH, FOURPLEX).calls == 0


class TestIngest:
    def test_stores_and_is_safe_to_repeat(self, engine, config):
        table = {
            "Columbus, OH": [
                realtor_row(1),
                realtor_row(2),
                realtor_row(3, property_id=None, listing_id=None),
            ]
        }
        source, _ = realtor(config, Scrape(table), ["Columbus, OH"])
        report = ingest(engine, config, source, ["OH"], FOURPLEX)
        assert (report.seen, report.new_listings, report.skipped) == (2, 2, 1)
        assert report.text().startswith("homeharvest: 2 listings seen; new 2, updated 0")
        assert "1 records could not be read" in report.text()
        again, _ = realtor(config, Scrape(table), ["Columbus, OH"])
        second = ingest(engine, config, again, ["OH"], FOURPLEX)
        assert (second.new_listings, second.updated) == (0, 2)
        with session_scope(engine) as session:
            rows = session.scalars(select(ListingRow)).all()
            assert {row.source for row in rows} == {"homeharvest"} and len(rows) == 2
            assert rows[0].description.startswith("Quadplex") and rows[0].tax_annual == 5760.0

    def test_a_refusal_keeps_what_was_stored(self, engine, config):
        class Half(Scrape):
            def __call__(self, **options):
                if options["location"] == "Dayton, OH":
                    raise RuntimeError("403")
                return super().__call__(**options)

        source, _ = realtor(
            config, Half({"Columbus, OH": [realtor_row(1)]}), ["Columbus, OH", "Dayton, OH"]
        )
        report = ingest(engine, config, source, ["OH"], FOURPLEX)
        assert report.seen == 1 and report.stopped.startswith("Realtor.com refused the request")
        assert "Stopped early" in report.text()

    def test_the_same_building_from_two_sources_is_one_property(
        self, engine, config, fetcher, http_mock
    ):
        source, _ = realtor(config, Scrape({"Columbus, OH": [realtor_row(1)]}), ["Columbus, OH"])
        ingest(engine, config, source, ["OH"], FOURPLEX)
        same = zillow_item(
            1,
            listingAddress={
                "street": "101 Sample Street",
                "city": "Columbus",
                "state": "OH",
                "zipCode": "43215",
            },
            coordinates={"latitude": 39.97, "longitude": -83.0},
        )
        http_mock.post(RUN_URL).mock(return_value=httpx.Response(201, json=[same]))
        zillow_source, _ = zillow(engine, config, fetcher)
        ingest(engine, config, zillow_source, ["OH"], FOURPLEX)
        with session_scope(engine) as session:
            assert len(session.scalars(select(PropertyRow)).all()) == 1
            assert len(session.scalars(select(ListingRow)).all()) == 2


class TestCommands:
    def test_scrapers_are_off_by_default(self, engine):
        for name in ("homeharvest", "apify_zillow"):
            result = CliRunner().invoke(app, ["fetch", "--source", name])
            assert result.exit_code == 2 and "is a scraper and is switched off" in result.output
        unknown = CliRunner().invoke(app, ["fetch", "--source", "craigslist"])
        assert unknown.exit_code == 2 and "Unknown source craigslist" in unknown.output

    def test_switched_on_without_the_library(self, engine, config_dir, monkeypatch):
        (config_dir / "settings.local.yaml").write_text(
            "sources: {homeharvest: {enabled: true}}\n"
            "scrapers: {homeharvest: {locations: ['Columbus, OH']}}\n"
        )
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        dry = CliRunner().invoke(
            app, ["fetch", "--source", "homeharvest", "--dry-run"], env={"COLUMNS": "200"}
        )
        assert dry.exit_code == 0, dry.output
        assert "1 searches of Realtor.com" in dry.output and "nothing was fetched" in dry.output
        real = CliRunner().invoke(app, ["fetch", "--source", "homeharvest"], env={"COLUMNS": "200"})
        assert real.exit_code == 0, real.output
        assert "is a scraper, not a licensed feed" in real.output
        assert "the HomeHarvest library is not installed" in real.output

    def test_labels(self):
        assert source_label("homeharvest") == "Realtor.com via HomeHarvest (scraped)"
        assert source_label("apify_zillow") == "Zillow via Apify (scraped)"
