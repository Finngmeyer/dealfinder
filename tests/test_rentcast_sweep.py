"""The RentCast adapter and the sweep pipeline, against a mocked RentCast."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from dealfinder.adapters import rentcast
from dealfinder.adapters.rentcast import LISTINGS_URL, RentCastSource
from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.models import Geo
from dealfinder.pipeline.sweep import SweepError, plan_sweep, run_sweep, sweep_states
from dealfinder.store import repo
from dealfinder.store.db import session_scope
from dealfinder.store.tables import (
    ApiUsageRow,
    CheckpointRow,
    ListingRow,
    PriceEventRow,
    PropertyRow,
    RunRow,
)
from dealfinder.store.usage import BudgetExceeded, BudgetGuard
from factories import rentcast_record

OH = Geo(state="OH")
FOURPLEX = ["fourplex"]


def serve(http_mock, records_by_state, *, count_header=True, reported_total=None):
    """A stand-in RentCast: pages through ``records_by_state`` like the real endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        records = records_by_state.get(params["state"], [])
        offset, limit = int(params.get("offset", 0)), int(params["limit"])
        headers = {}
        if count_header and params.get("includeTotalCount") == "true":
            headers["X-Total-Count"] = str(reported_total or len(records))
        return httpx.Response(200, json=records[offset : offset + limit], headers=headers)

    return http_mock.get(LISTINGS_URL).mock(side_effect=handler)


def records(count: int, start: int = 1, **changes):
    return [rentcast_record(number, **changes) for number in range(start, start + count)]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def small_pages(config):
    config.settings.sweep.page_size = 2
    return config


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def timed_fetcher(tmp_path, clock):
    return Fetcher(tmp_path / "cache", sleep=lambda s: None, now=clock)


def make_source(engine, config, fetcher, *, cap=50, key="rc-key", clock=None):
    guard = BudgetGuard(
        engine, "rentcast", cap=cap, cycle_day=1, **({"now": clock} if clock else {})
    )

    def last_count(scope):
        with session_scope(engine) as session:
            return repo.inventory_total(session, "rentcast", scope)

    source = RentCastSource(
        fetcher,
        api_key=key,
        guard=guard,
        sweep=config.settings.sweep,
        max_price=config.settings.buy_box.max_price,
        inventory=last_count,
        # The source and the sweep must agree on what day it is.
        today=(lambda: clock().date()) if clock else (lambda: date(2026, 11, 12)),
    )
    return source, guard


def sweep(engine, config, fetcher, *, mode="weekly", states=("OH",), cap=50, clock=None, **extra):
    source, guard = make_source(engine, config, fetcher, cap=cap, clock=clock)
    return run_sweep(
        engine, config, source, guard, mode=mode, states=list(states), types=FOURPLEX,
        **({"now": clock} if clock else {}), **extra,
    )  # fmt: skip


def usage_rows(engine) -> int:
    with session_scope(engine) as session:
        return len(session.scalars(select(ApiUsageRow)).all())


class TestToListing:
    def test_fields_are_mapped(self):
        listing = rentcast.to_listing(rentcast_record(7))
        assert listing.source == "rentcast"
        assert listing.source_listing_id == "107-Sample-St,-Columbus,-OH-43215"
        assert (listing.status, listing.price) == ("active", 407_000)
        assert (listing.address_line1, listing.city, listing.state, listing.zip_code) == (
            "107 Sample St", "Columbus", "OH", "43215",
        )  # fmt: skip
        assert listing.county_fips == "39049"
        assert (listing.property_kind, listing.property_type_raw) == (
            "multifamily_2_4", "Multi-Family",
        )  # fmt: skip
        assert (listing.bedrooms, listing.bathrooms, listing.building_sqft) == (8, 4, 3600)
        assert (listing.year_built, listing.days_on_market) == (1978, 42)
        assert listing.listed_date == date(2026, 10, 1)
        assert (listing.agent_name, listing.office_name, listing.mls_number) == (
            "Sam Sample", "Sample Realty", "S00007",
        )  # fmt: skip
        assert [(change.day, change.price) for change in listing.history] == [
            (date(2026, 10, 1), 407_000)
        ]
        assert listing.raw["formattedAddress"] == "107 Sample St, Columbus, OH 43215"

    def test_rentcast_says_nothing_about_units_text_or_taxes(self):
        listing = rentcast.to_listing(rentcast_record(1))
        assert listing.unit_count is None
        assert listing.description is None
        assert listing.tax_annual is None

    @pytest.mark.parametrize(
        ("raw_type", "kind"),
        [
            ("Single Family", "sfr"),
            ("Condo", "condo"),
            ("Townhouse", "townhouse"),
            ("Apartment", "multifamily_5plus"),
            ("Land", "land"),
            ("Castle", "other"),
            (None, "other"),
        ],
    )
    def test_property_types(self, raw_type, kind):
        assert rentcast.to_listing(rentcast_record(1, propertyType=raw_type)).property_kind == kind

    def test_inactive_missing_and_odd_values(self):
        listing = rentcast.to_listing(
            rentcast_record(1, status="Inactive", price=None, hoa=None, listingAgent=None,
                            history=None, countyFips=None, listedDate="not a date")
        )  # fmt: skip
        assert listing.status == "off_market"
        assert listing.price is None and listing.county_fips is None
        assert listing.listed_date is None and listing.history == []

    def test_a_record_without_an_id_is_refused(self):
        with pytest.raises(ValueError, match="without an id"):
            rentcast.to_listing(rentcast_record(1, id=None))

    @pytest.mark.parametrize(
        ("types", "expected"),
        [(["fourplex"], ["Multi-Family"]), (["duplex", "triplex", "fourplex"], ["Multi-Family"]),
         (["sfr", "fourplex"], ["Multi-Family", "Single Family"]),
         (["multifamily_5plus"], ["Apartment"])],
    )  # fmt: skip
    def test_buy_box_types_share_rentcast_types(self, types, expected):
        assert rentcast.raw_types(types) == expected


class TestPages:
    def test_request_shape_and_paging(self, engine, small_pages, fetcher, http_mock):
        route = serve(http_mock, {"OH": records(5)})
        source, _ = make_source(engine, small_pages, fetcher)
        pages = list(source.pages(OH, FOURPLEX))
        assert [(page.offset, len(page.items)) for page in pages] == [(0, 2), (2, 2), (4, 1)]
        assert pages[0].total == 5 and pages[1].total is None

        first, second = (call.request for call in route.calls[:2])
        assert dict(first.url.params) == {
            "state": "OH",
            "propertyType": "Multi-Family",
            "status": "Active",
            "limit": "2",
            "price": "*:750000",
            "offset": "0",
            "includeTotalCount": "true",
        }
        assert first.headers["X-Api-Key"] == "rc-key"
        assert "includeTotalCount" not in second.url.params  # the count is asked for once
        assert second.url.params["offset"] == "2"

    def test_an_exact_multiple_of_the_page_size_costs_one_empty_page(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"OH": records(4)})
        source, _ = make_source(engine, small_pages, fetcher)
        pages = list(source.pages(OH, FOURPLEX))
        assert [len(page.items) for page in pages] == [2, 2, 0]
        assert route.call_count == 3

    def test_price_filter_can_be_switched_off(self, engine, small_pages, fetcher, http_mock):
        small_pages.settings.sweep.filter_by_max_price = False
        route = serve(http_mock, {"OH": records(1)})
        source, _ = make_source(engine, small_pages, fetcher)
        list(source.pages(OH, FOURPLEX))
        assert "price" not in route.calls.last.request.url.params
        assert source.scope(OH, FOURPLEX) == "OH|Multi-Family"

    def test_since_becomes_a_days_old_window(self, engine, small_pages, fetcher, http_mock):
        route = serve(http_mock, {"OH": records(1)})
        source, _ = make_source(engine, small_pages, fetcher)
        list(source.pages(OH, FOURPLEX, date(2026, 11, 10)))
        assert route.calls.last.request.url.params["daysOld"] == "*:2"

    def test_every_billed_page_is_recorded_and_cached_pages_are_free(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"OH": records(3)})
        source, guard = make_source(engine, small_pages, fetcher)
        first = list(source.pages(OH, FOURPLEX))
        assert (route.call_count, guard.used()) == (2, 2)
        assert not any(page.from_cache for page in first)

        again = list(source.pages(OH, FOURPLEX))
        assert (route.call_count, guard.used()) == (2, 2)
        assert all(page.from_cache for page in again)
        assert again[0].total == 3  # the count survives in the cache

    def test_the_guard_refuses_before_a_request_is_sent(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"OH": records(5)})
        source, guard = make_source(engine, small_pages, fetcher, cap=1)
        pages = source.pages(OH, FOURPLEX)
        assert len(next(pages).items) == 2
        with pytest.raises(BudgetExceeded):
            next(pages)
        assert route.call_count == 1
        assert guard.used() == 1

    def test_no_key_means_no_request(self, engine, small_pages, fetcher, http_mock):
        route = serve(http_mock, {"OH": records(1)})
        source, _ = make_source(engine, small_pages, fetcher, key=None)
        with pytest.raises(SourceUnavailable, match="RENTCAST_API_KEY is not set") as caught:
            list(source.pages(OH, FOURPLEX))
        assert caught.value.missing_key and route.call_count == 0

    def test_a_rejected_key_is_reported_and_not_billed(
        self, engine, small_pages, fetcher, http_mock
    ):
        http_mock.get(LISTINGS_URL).mock(return_value=httpx.Response(401, json={"status": 401}))
        source, guard = make_source(engine, small_pages, fetcher)
        with pytest.raises(SourceUnavailable, match="not accepted"):
            list(source.pages(OH, FOURPLEX))
        assert guard.used() == 0

    def test_an_error_object_in_a_200_is_rejected(self, engine, small_pages, fetcher, http_mock):
        http_mock.get(LISTINGS_URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
        source, guard = make_source(engine, small_pages, fetcher)
        with pytest.raises(SourceUnavailable, match="not a list of listings"):
            list(source.pages(OH, FOURPLEX))
        assert guard.used() == 1  # RentCast bills every HTTP 200, usable or not

    def test_search_yields_raw_records(self, engine, small_pages, fetcher, http_mock):
        serve(http_mock, {"OH": records(3)})
        source, _ = make_source(engine, small_pages, fetcher)
        found = list(source.search(OH, FOURPLEX, None))
        assert [record["mlsNumber"] for record in found] == ["S00001", "S00002", "S00003"]
        assert source.to_listing(found[0]).source_listing_id == found[0]["id"]

    def test_the_key_never_reaches_the_cache(self, engine, small_pages, fetcher, http_mock):
        serve(http_mock, {"OH": records(1)})
        source, _ = make_source(engine, small_pages, fetcher, key="rc-secret-key-999")
        list(source.pages(OH, FOURPLEX))
        stored = b"".join(p.read_bytes() for p in fetcher.cache_dir.rglob("*") if p.is_file())
        assert b"rc-secret-key-999" not in stored


class TestPlanning:
    def test_states_default_to_the_48_and_dc(self, config):
        states = sweep_states(config, None)
        assert len(states) == 49 and "DC" in states and "AK" not in states

    def test_states_from_the_buy_box_or_the_command(self, config):
        config.settings.buy_box.states = ["OH", "IN"]
        assert sweep_states(config, None) == ["IN", "OH"]
        assert sweep_states(config, [" oh ", "mi", "OH"]) == ["MI", "OH"]

    def test_states_outside_coverage_are_refused(self, config):
        with pytest.raises(SweepError, match="AK, ZZ"):
            sweep_states(config, ["OH", "AK", "ZZ"])

    def test_unknown_inventory_is_a_floor(self, engine, config, fetcher):
        source, guard = make_source(engine, config, fetcher)
        plan = plan_sweep(source, guard, config, mode="weekly", states=["IN", "OH"], types=FOURPLEX)
        assert (plan.calls, plan.all_known) == (2, False)
        assert plan.rows[0].note == "no count yet: at least one request"
        assert plan.monthly_calls == pytest.approx(2 * 52 / 12)

    def test_known_inventory_gives_pages_and_cost_per_plan(self, engine, config, fetcher):
        source, guard = make_source(engine, config, fetcher)
        with session_scope(engine) as session:
            repo.record_inventory(session, "rentcast", source.scope(OH, FOURPLEX), 1_234)
            repo.record_inventory(
                session, "rentcast", source.scope(Geo(state="IN"), FOURPLEX), 1_000
            )
        plan = plan_sweep(source, guard, config, mode="weekly", states=["IN", "OH"], types=FOURPLEX)
        assert [row.calls for row in plan.rows] == [
            3,
            3,
        ]  # 1,000 is two full pages plus an empty one
        assert plan.all_known
        assert plan.monthly_calls == pytest.approx(26)
        assert plan.monthly_cost == {
            "developer": 0.0,
            "foundation": 74.0,
            "growth": 199.0,
            "scale": 449.0,
        }

    def test_overage_is_priced(self, engine, config, fetcher):
        source, guard = make_source(engine, config, fetcher)
        with session_scope(engine) as session:
            repo.record_inventory(session, "rentcast", source.scope(OH, FOURPLEX), 12_000)
        plan = plan_sweep(source, guard, config, mode="weekly", states=["OH"], types=FOURPLEX)
        monthly = round(25 * 52 / 12)  # 108 requests
        assert plan.monthly_cost["developer"] == pytest.approx((monthly - 50) * 0.20)

    def test_what_fits_in_the_remaining_budget(self, engine, config, fetcher):
        source, guard = make_source(engine, config, fetcher, cap=5)
        guard.record("listings/sale", units=3)
        plan = plan_sweep(
            source, guard, config, mode="daily", states=["IN", "MI", "OH"], types=FOURPLEX
        )
        assert (plan.budget_used, plan.remaining, plan.calls) == (3, 2, 3)
        assert plan.states_that_fit() == 2
        assert plan.monthly_calls == pytest.approx(3 * 365 / 12)


class TestWeeklySweep:
    def test_listings_are_stored_and_the_run_is_reported(
        self, engine, small_pages, fetcher, http_mock
    ):
        serve(
            http_mock, {"OH": records(5), "IN": records(1, start=50, state="IN", zipCode="46204")}
        )
        report = sweep(engine, small_pages, fetcher, states=("IN", "OH"))

        assert (report.status, report.resumed, report.requests) == ("completed", False, 4)
        by_state = {scope.state: scope for scope in report.scopes}
        assert (by_state["OH"].seen, by_state["OH"].new_listings, by_state["OH"].requests) == (
            5,
            5,
            3,
        )
        assert (by_state["IN"].seen, by_state["IN"].requests, by_state["IN"].complete) == (
            1,
            1,
            True,
        )
        assert report.budget_used == 4
        assert "Listings seen 6; new 6" in report.text()

        with session_scope(engine) as session:
            assert len(session.scalars(select(ListingRow)).all()) == 6
            assert len(session.scalars(select(PropertyRow)).all()) == 6
            run = session.scalars(select(RunRow)).one()
            assert (run.mode, run.status) == ("weekly", "completed")
            assert run.finished_at is not None and run.report == report.text()
            assert repo.inventory_total(session, "rentcast", by_state["OH"].scope) == 5
            assert all(
                row.last_seen_run_id == run.id for row in session.scalars(select(ListingRow))
            )

    def test_running_it_again_at_once_costs_nothing_and_changes_nothing(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"OH": records(5)})
        sweep(engine, small_pages, fetcher)
        again = sweep(engine, small_pages, fetcher)
        assert route.call_count == 3
        assert (again.requests, again.scopes[0].cached_pages) == (0, 3)
        assert again.totals() == {
            "seen": 5, "new_listings": 0, "new_properties": 0, "price_changes": 0, "relisted": 0,
            "off_market": 0,
        }  # fmt: skip
        assert again.budget_used == 3

    def test_next_week_s_changes_are_picked_up(
        self, engine, small_pages, timed_fetcher, clock, http_mock
    ):
        first_week = records(4)
        serve(http_mock, {"OH": first_week})
        sweep(engine, small_pages, timed_fetcher, clock=clock)

        clock.now += timedelta(days=7)
        second_week = [
            {**first_week[0], "price": first_week[0]["price"] - 20_000},  # a price cut
            first_week[1],  # unchanged
            *records(1, start=9),  # new; listings 3 and 4 are gone
        ]
        serve(http_mock, {"OH": second_week})
        report = sweep(engine, small_pages, timed_fetcher, clock=clock)

        assert report.totals() == {
            "seen": 3, "new_listings": 1, "new_properties": 1, "price_changes": 1, "relisted": 0,
            "off_market": 2,
        }  # fmt: skip
        with session_scope(engine) as session:
            statuses = [row.status for row in session.scalars(select(ListingRow))]
            assert (statuses.count("active"), statuses.count("off_market")) == (3, 2)
            events = [event.event for event in session.scalars(select(PriceEventRow))]
            assert events.count("price_change") == 1 and events.count("off_market") == 2

        # The week after, one of them is back at a lower price.
        clock.now += timedelta(days=7)
        serve(http_mock, {"OH": [*second_week, {**first_week[2], "price": 390_000}]})
        third = sweep(engine, small_pages, timed_fetcher, clock=clock)
        assert third.totals()["relisted"] == 1

    def test_a_short_fetch_marks_nothing_off_market(
        self, engine, small_pages, timed_fetcher, clock, http_mock
    ):
        serve(http_mock, {"OH": records(4)})
        sweep(engine, small_pages, timed_fetcher, clock=clock)
        clock.now += timedelta(days=7)
        # RentCast says ten listings exist but hands back only two.
        serve(http_mock, {"OH": records(2)}, reported_total=10)
        report = sweep(engine, small_pages, timed_fetcher, clock=clock)
        scope = report.scopes[0]
        assert (scope.complete, scope.off_market) == (False, 0)
        assert "only 2 of 10 listings came back" in scope.detail
        with session_scope(engine) as session:
            assert all(row.status == "active" for row in session.scalars(select(ListingRow)))

    def test_a_grace_sweep_setting_delays_off_market(
        self, engine, small_pages, timed_fetcher, clock, http_mock
    ):
        small_pages.settings.sweep.missed_sweeps_before_off_market = 2
        serve(http_mock, {"OH": records(3)})
        sweep(engine, small_pages, timed_fetcher, clock=clock)
        serve(http_mock, {"OH": records(1)})
        clock.now += timedelta(days=7)
        assert sweep(engine, small_pages, timed_fetcher, clock=clock).totals()["off_market"] == 0
        clock.now += timedelta(days=7)
        assert sweep(engine, small_pages, timed_fetcher, clock=clock).totals()["off_market"] == 2


class TestStoppingAndResuming:
    def test_the_budget_stops_the_run_and_the_same_command_resumes_it(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"IN": records(1, start=50, state="IN"), "OH": records(5)})
        stopped = sweep(engine, small_pages, fetcher, states=("IN", "OH"), cap=2)

        assert stopped.status == "stopped_budget"
        assert [scope.status for scope in stopped.scopes] == ["completed", "stopped_budget"]
        assert (stopped.requests, stopped.budget_used) == (2, 2)
        assert "Stopped at OH" in stopped.message and "2 of 2 requests used" in stopped.message
        assert route.call_count == 2  # nothing was sent past the cap
        with session_scope(engine) as session:
            mark = session.scalars(
                select(CheckpointRow).where(CheckpointRow.scope.startswith("OH"))
            ).one()
            assert (mark.next_offset, mark.completed, mark.fetched) == (2, False, 2)
            assert session.scalars(select(RunRow)).one().status == "stopped_budget"

        resumed = sweep(engine, small_pages, fetcher, states=("IN", "OH"), cap=10)
        assert (resumed.status, resumed.resumed, resumed.run_id) == (
            "completed", True, stopped.run_id,
        )  # fmt: skip
        assert [scope.status for scope in resumed.scopes] == ["done_earlier", "completed"]
        assert resumed.requests == 2  # only the two pages that were still missing
        offsets = [call.request.url.params["offset"] for call in route.calls]
        assert offsets == ["0", "0", "2", "4"]
        with session_scope(engine) as session:
            assert len(session.scalars(select(ListingRow)).all()) == 6
            assert len(session.scalars(select(RunRow)).all()) == 1

    def test_a_cap_already_spent_sends_nothing(self, engine, small_pages, fetcher, http_mock):
        route = serve(http_mock, {"OH": records(1)})
        report = sweep(engine, small_pages, fetcher, cap=0)
        assert (report.status, report.requests, route.call_count) == ("stopped_budget", 0, 0)

    def test_an_outage_fails_the_run_and_it_resumes_later(
        self, engine, small_pages, fetcher, http_mock
    ):
        http_mock.get(LISTINGS_URL).mock(return_value=httpx.Response(503))
        failed = sweep(engine, small_pages, fetcher)
        assert failed.status == "failed"
        assert "HTTP 503" in failed.scopes[0].detail and "Run it again" in failed.message
        assert usage_rows(engine) == 0  # failed requests are not billed

        serve(http_mock, {"OH": records(3)})
        resumed = sweep(engine, small_pages, fetcher)
        assert (resumed.status, resumed.resumed, resumed.totals()["new_listings"]) == (
            "completed", True, 3,
        )  # fmt: skip

    def test_fresh_starts_a_new_run(self, engine, small_pages, fetcher, http_mock):
        serve(http_mock, {"IN": records(1, start=50, state="IN"), "OH": records(5)})
        first = sweep(engine, small_pages, fetcher, states=("IN", "OH"), cap=2)
        second = sweep(engine, small_pages, fetcher, states=("IN", "OH"), cap=10, fresh=True)
        assert second.run_id != first.run_id and not second.resumed

    def test_different_states_are_a_different_run(self, engine, small_pages, fetcher, http_mock):
        serve(http_mock, {"OH": records(5), "IN": []})
        first = sweep(engine, small_pages, fetcher, cap=1)
        other = sweep(engine, small_pages, fetcher, states=("IN",), cap=10)
        assert other.run_id != first.run_id and not other.resumed


class TestDailyAndProbe:
    def test_daily_asks_for_new_listings_and_marks_nothing_off_market(
        self, engine, small_pages, timed_fetcher, clock, http_mock
    ):
        serve(http_mock, {"OH": records(4)})
        sweep(engine, small_pages, timed_fetcher, clock=clock)
        clock.now += timedelta(days=1)
        route = serve(http_mock, {"OH": records(1, start=9)})
        report = sweep(engine, small_pages, timed_fetcher, mode="daily", clock=clock)

        assert route.calls.last.request.url.params["daysOld"] == "*:2"
        assert report.totals()["new_listings"] == 1 and report.totals()["off_market"] == 0
        assert report.scopes[0].complete is None
        with session_scope(engine) as session:
            assert all(row.status == "active" for row in session.scalars(select(ListingRow)))
            # A daily count is not the state's inventory, so the weekly figure stands.
            assert repo.inventory_total(session, "rentcast", report.scopes[0].scope) == 4

    def test_probe_counts_each_state_with_one_request_and_keeps_the_listings(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"OH": records(5), "IN": records(3, start=50, state="IN")})
        report = sweep(engine, small_pages, fetcher, mode="probe", states=("IN", "OH"))
        assert (report.status, report.requests, route.call_count) == ("completed", 2, 2)
        assert [scope.total_reported for scope in report.scopes] == [3, 5]
        with session_scope(engine) as session:
            assert len(session.scalars(select(ListingRow)).all()) == 4  # two per first page
            assert all(row.status == "active" for row in session.scalars(select(ListingRow)))

        source, guard = make_source(engine, small_pages, fetcher)
        plan = plan_sweep(
            source, guard, small_pages, mode="weekly", states=["IN", "OH"], types=FOURPLEX
        )
        assert [(row.state, row.calls, row.known) for row in plan.rows] == [
            ("IN", 2, True), ("OH", 3, True),
        ]  # fmt: skip

    def test_the_first_weekly_page_after_a_probe_is_free(
        self, engine, small_pages, fetcher, http_mock
    ):
        route = serve(http_mock, {"OH": records(5)})
        sweep(engine, small_pages, fetcher, mode="probe")
        weekly = sweep(engine, small_pages, fetcher)
        assert route.call_count == 3  # one for the probe, two more for the rest
        assert (weekly.requests, weekly.scopes[0].cached_pages) == (2, 1)
