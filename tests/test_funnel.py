"""The two-stage funnel over stored listings: what gets underwritten, what survives stage 1,
what stage 2 spends, and how the results are scored and stored."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from dealfinder.adapters.rentcast import RENT_URL
from dealfinder.analysis import AnalysisError
from dealfinder.classify.llm import LlmExtraction
from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.pipeline.classify import RECORD_KEY, classify_all
from dealfinder.pipeline.report import deal_detail, deal_rows, problem_rows
from dealfinder.pipeline.underwrite import (
    build_request,
    candidates,
    price_cut,
    run_funnel,
    source_label,
    stage1_verdict,
)
from dealfinder.settings import Environment, Stage1
from dealfinder.store.db import session_scope
from dealfinder.store.overrides import clear_overrides, get_overrides, set_override
from dealfinder.store.tables import (
    ClassificationRow,
    ListingRow,
    PropertyRow,
    ScreenRow,
    UnderwritingRow,
    UnitRow,
)
from factories import market_context, rent_estimate, rentcast_property
from test_classify_pipeline import (
    FakeClaude,
    csv_listing,
    rentcast_listing,
    serve_properties,
    store,
)
from test_enrichment_sources import serve_fema, serve_geocoder, zone
from test_rentcast_sweep import make_source

RANKING = "conv_investor"


class Markets:
    """Stands in for the market cache: every ZIP gets the same connected market data."""

    def __init__(self, **changes) -> None:
        self.changes = changes

    def get(self, zip_code):
        return market_context(zip_code, **self.changes) if zip_code else None


@pytest.fixture
def offline(tmp_path):
    """A fetcher that reaches nothing, for tests that are not about stage 2's lookups."""
    return Fetcher(tmp_path / "cache", offline=True)


def funnel(engine, config, fetcher, **options):
    options.setdefault("markets", Markets())
    return run_funnel(engine, config, Environment(), fetcher, **options)


def fourplex(number: int, price: float, description: str = "Quadplex", **changes):
    """A confirmed fourplex from an MLS import: 8 bd / 4 ba, so four 2-bedroom units."""
    return csv_listing(number, description, price=price, **changes)


def results(engine, scenario: str = RANKING) -> dict[int, UnderwritingRow]:
    with session_scope(engine) as session:
        rows = session.scalars(select(UnderwritingRow).where(UnderwritingRow.scenario == scenario))
        found = {row.listing_id: row for row in rows}
        session.expunge_all()
    return found


def screens(engine) -> dict[int, ScreenRow]:
    with session_scope(engine) as session:
        found = {row.listing_id: row for row in session.scalars(select(ScreenRow))}
        session.expunge_all()
    return found


class TestStage1:
    def test_verdict(self):
        rule = Stage1(min_cash_flow_per_door_month=-100, min_rent_to_price=0.007)
        assert stage1_verdict(-99, 0.001, rule)[0] is True  # cash flow clears its bar
        assert stage1_verdict(-500, 0.0071, rule)[0] is True  # rent ratio clears its bar
        kept, why = stage1_verdict(-101, 0.0069, rule)
        assert kept is False
        assert why == (
            "Dropped: cash flow -$101 a door, rent 0.69% of price; needs -$100 a door or "
            "0.70% of price."
        )

    def test_bars_can_be_switched_off(self):
        only_cash = Stage1(min_cash_flow_per_door_month=0, min_rent_to_price=None)
        assert stage1_verdict(-1, 0.02, only_cash)[0] is False
        nothing = Stage1(min_cash_flow_per_door_month=None, min_rent_to_price=None)
        assert stage1_verdict(-9999, 0.0, nothing) == (
            True,
            "Kept: cash flow -$9,999 a door, rent 0.00% of price (stage 1 has no bars set).",
        )

    def test_free_data_screens_every_listing(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0), fourplex(2, 900_000.0))
        classify_all(engine, config)
        report = funnel(engine, config, offline, stage2=False)
        assert (report.candidates, report.underwritten, report.kept, report.dropped) == (2, 2, 1, 1)
        assert "Stage 2 was skipped, as asked." in report.notes
        found = screens(engine)
        assert [found[n].passed_stage1 for n in (1, 2)] == [True, False]
        assert found[1].note.startswith("Kept:") and found[2].note.startswith("Dropped:")
        assert all(row.stage == 1 for row in found.values())
        # Stage 1 runs the ranking scenario only.
        with session_scope(engine) as session:
            scenarios = session.scalars(select(UnderwritingRow.scenario)).all()
        assert scenarios == [RANKING, RANKING]

    def test_inputs_carry_their_sources(self, engine, config, offline):
        store(
            engine,
            fourplex(
                1,
                300_000.0,
                "Quadplex, sold as-is. Units 1-3 leased at $1,000. Unit 4 is vacant.",
                year_built=1962,
                tax_annual=4100.0,
            ),
        )
        classify_all(engine, config)
        funnel(engine, config, offline, stage2=False)
        lines = {line["key"]: line for line in screens(engine)[1].inputs}
        assert lines["price"]["source"] == "list price, your import 'mls'"
        assert lines["rents"]["kind"] == "actual"
        assert lines["rents"]["source"] == (
            "stated in the listing: $1,000, $1,000, $1,000, unknown"
        )
        assert lines["market_rents"]["kind"] == "estimated"
        assert lines["market_rents"]["source"].startswith("HUD FY2027 area FMR, Sample Metro")
        assert lines["year_built"]["source"] == "your import 'mls'"
        assert lines["tax_bill"]["source"] == "your import 'mls'; taxes never go below it"
        assert (
            lines["tax_rate"]["source"]
            == "ACS 2020-2024, Sample County, Ohio: median taxes / median value"
        )
        assert lines["rehab"]["kind"] == "estimated" and lines["rehab"]["flagged"]
        assert lines["rehab"]["value"] == 80_000  # heavy rehab, four units
        assert "from the listing's tags (as is)" in lines["rehab"]["source"]
        codes = {flag["code"] for flag in screens(engine)[1].flags}
        assert "rents_estimated" in codes  # unit 4 is priced from the market estimate

    def test_one_listing_per_property_and_only_wanted_types(self, engine, config, offline):
        store(
            engine,
            rentcast_listing(1),  # same building as the next one, seen earlier
            fourplex(
                1, 410_000.0, address_line1="101 SAMPLE STREET", latitude=None, longitude=None
            ),
            csv_listing(2, "Charming duplex", bedrooms=4, bathrooms=2),
            csv_listing(3, "Quadplex", status="pending"),
            rentcast_listing(4),  # unit count only hinted at: unverified, still underwritten
        )
        classify_all(engine, config)
        with session_scope(engine) as session:
            chosen = candidates(session, config)
            sources = {
                session.get_one(ListingRow, listing_id).source: match
                for listing_id, match in chosen
            }
        assert sources == {"csv:mls": "yes", "rentcast": "unverified"} and len(chosen) == 2
        funnel(engine, config, offline, stage2=False)
        found = screens(engine)
        assert sorted(row.type_match for row in found.values()) == ["unverified", "yes"]

    def test_hidden_properties_are_skipped(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        with session_scope(engine) as session:
            session.scalars(select(PropertyRow)).one().hidden = True
        assert funnel(engine, config, offline).candidates == 0


class TestProblems:
    def test_what_cannot_be_underwritten_says_why(self, engine, config, offline):
        store(
            engine,
            csv_listing(1, "Quadplex", price=None),
            rentcast_listing(2, bedrooms=None, bathrooms=None),  # no unit count at all
            fourplex(3, 300_000.0, bedrooms=None, bathrooms=None),  # no bedrooms to price by
        )
        classify_all(engine, config)
        report = funnel(engine, config, offline)
        assert (report.underwritten, sum(report.problems.values())) == (0, 3)
        notes = {row.listing_id: row.note for row in screens(engine).values()}
        assert notes[1] == "The listing has no price."
        assert notes[2].startswith("The unit count is not known.")
        assert notes[3] == (
            "No rent is stated for every unit, and bedrooms per unit are not known, so no rent "
            "can be estimated."
        )
        assert all(row.stage == 0 for row in screens(engine).values())
        assert results(engine) == {}
        with session_scope(engine) as session:
            listed = problem_rows(session)
        assert [row.listing_id for row in listed] == [3, 2, 1]  # cheapest first, no price last
        assert "could not be" in report.text() and "3 x" not in report.text()

    def test_without_hud_rents_a_stated_rent_roll_still_works(self, engine, config, offline):
        store(
            engine,
            fourplex(1, 300_000.0, "Quadplex. All units rented at $1,200/mo."),
            fourplex(2, 300_000.0),
        )
        classify_all(engine, config)
        report = funnel(engine, config, offline, markets=Markets(fair_market_rents=None))
        assert report.underwritten == 1
        assert (
            screens(engine)[2].note
            == (
                "No rent is stated for every unit, and HUD rents are not available "
                "(the listing has no ZIP code)."
            )
            or "HUD rents are not available" in screens(engine)[2].note
        )

    def test_the_missing_key_is_named(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        # The real market loader, offline with an empty cache and no keys set.
        report = run_funnel(engine, config, Environment(), offline)
        assert list(report.problems) == [
            "No rent is stated for every unit, and HUD rents are not available "
            "(HUD_API_TOKEN is not set)."
        ]

    def test_a_ranking_scenario_that_does_not_exist(self, engine, config, offline):
        config.settings.buy_box.ranking_scenario = "nope"
        with pytest.raises(AnalysisError, match="'nope' is not a loan preset"):
            funnel(engine, config, offline)

    def test_a_ranking_loan_that_does_not_fit_the_building(self, engine, config, offline):
        config.settings.buy_box.property_types = ["multifamily_5plus"]
        store(engine, csv_listing(1, "Two fourplexes on one lot", bedrooms=16, bathrooms=8))
        classify_all(engine, config)
        funnel(engine, config, offline)
        assert screens(engine)[1].note == (
            "The ranking loan 'conv_investor' is not offered for 8 units."
        )


def serve_rents(http_mock, rent: float = 1400.0):
    return http_mock.get(RENT_URL).mock(return_value=httpx.Response(200, json=rent_estimate(rent)))


class TestStage2:
    def test_survivors_get_every_scenario_and_the_paid_lookups(
        self, engine, config, fetcher, http_mock
    ):
        store(engine, fourplex(1, 300_000.0), fourplex(2, 900_000.0))
        classify_all(engine, config)
        rents = serve_rents(http_mock)
        serve_fema(http_mock, [zone("AE", "T")])
        source, guard = make_source(engine, config, fetcher)
        report = funnel(engine, config, fetcher, rentcast=source)

        assert (report.kept, report.dropped, report.enriched) == (1, 1, 1)
        assert (report.records, report.rent_requests, report.flood_lookups) == (0, 1, 1)
        assert rents.call_count == 1 and guard.used() == 1  # four identical units, one request
        found = screens(engine)
        assert (found[1].stage, found[2].stage) == (2, 1)
        assert found[1].enrichment == {
            "flood": "FEMA flood map: zone AE, a Special Flood Hazard Area",
            "rents": "rent estimated for 1 unit layout",
        }
        with session_scope(engine) as session:
            scenarios = session.scalars(
                select(UnderwritingRow.scenario).where(UnderwritingRow.listing_id == 1)
            ).all()
            dropped = session.scalars(
                select(UnderwritingRow.scenario).where(UnderwritingRow.listing_id == 2)
            ).all()
            units = session.scalars(select(UnitRow).where(UnitRow.property_id == 1)).all()
            place = session.get_one(PropertyRow, 1)
            assert [unit.market_rent for unit in units] == [1400.0] * 4
            assert units[0].market_rent_source.startswith("RentCast rent estimate from 3")
            assert len(units[0].comps) == 3 and units[0].rent_low == 1300.0
            assert (place.flood_zone, place.in_flood_zone) == ("AE", True)
        assert set(scenarios) == {"fha_house_hack", "conv_owner_5", "conv_investor", "dscr", "cash"}
        assert dropped == [RANKING]
        lines = {line["key"]: line for line in found[1].inputs}
        assert lines["market_rents"]["source"].startswith("RentCast rent estimate from 3")
        assert lines["flood_insurance"]["source"] == "placeholder: FEMA flood zone AE"
        assert lines["flood_insurance"]["value"] == 3000  # four units at the placeholder rate
        assert "flood_zone" in {flag["code"] for flag in found[1].flags}

    def test_a_second_run_spends_nothing_more(self, engine, config, fetcher, http_mock):
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        rents = serve_rents(http_mock)
        fema = serve_fema(http_mock, [zone("X", "F")])
        source, guard = make_source(engine, config, fetcher)
        funnel(engine, config, fetcher, rentcast=source)
        again = funnel(engine, config, fetcher, rentcast=source)
        assert (again.rent_requests, again.flood_lookups, again.enriched) == (0, 0, 1)
        assert rents.call_count == 1 and fema.call_count == 1 and guard.used() == 1
        assert screens(engine)[1].stage == 2

    def test_a_rentcast_listing_gets_its_record_first(self, engine, config, fetcher, http_mock):
        store(engine, rentcast_listing(1, price=300_000.0))
        classify_all(engine, config)
        serve_properties(http_mock, {1: rentcast_property(1)})
        serve_rents(http_mock)
        serve_fema(http_mock, [zone("X", "F")])
        source, guard = make_source(engine, config, fetcher)
        report = funnel(engine, config, fetcher, rentcast=source)
        assert (report.records, report.rent_requests) == (1, 1) and guard.used() == 2
        screen = screens(engine)[1]
        assert (screen.type_match, screen.confidence) == ("yes", "high")
        assert screen.enrichment["record"] == "property record found: 4 units"
        lines = {line["key"]: line for line in screen.inputs}
        assert lines["tax_bill"]["source"] == (
            "county record via RentCast, 2025; taxes never go below it"
        )
        assert results(engine)[1].buy_box_misses != ["unit count not confirmed"]

    def test_a_record_can_rule_a_listing_out(self, engine, config, fetcher, http_mock):
        store(engine, rentcast_listing(1, price=300_000.0))
        classify_all(engine, config)
        serve_properties(http_mock, {1: rentcast_property(1, features={"unitCount": 2})})
        serve_fema(http_mock)
        source, _ = make_source(engine, config, fetcher)
        report = funnel(engine, config, fetcher, rentcast=source)
        assert screens(engine)[1].note == "Not a wanted type after a closer look: duplex."
        assert results(engine) == {} and report.rent_requests == 0

    def test_the_run_cap_on_rent_requests(self, engine, config, fetcher, http_mock):
        config.settings.funnel.stage2.max_rent_requests_per_run = 1
        store(engine, fourplex(1, 300_000.0), fourplex(2, 310_000.0))
        classify_all(engine, config)
        serve_rents(http_mock)
        serve_fema(http_mock)
        source, guard = make_source(engine, config, fetcher)
        report = funnel(engine, config, fetcher, rentcast=source)
        assert report.rent_requests == 1 and guard.used() == 1
        notes = [row.enrichment.get("rents") for row in screens(engine).values()]
        assert sorted(notes) == [
            "not estimated: funnel.stage2.max_rent_requests_per_run reached",
            "rent estimated for 1 unit layout",
        ]
        assert report.enriched == 2  # the second is still underwritten, on HUD rents

    def test_the_monthly_budget_stops_the_spending(self, engine, config, fetcher, http_mock):
        store(engine, fourplex(1, 300_000.0), fourplex(2, 310_000.0))
        classify_all(engine, config)
        serve_rents(http_mock)
        serve_fema(http_mock)
        source, guard = make_source(engine, config, fetcher, cap=1)
        report = funnel(engine, config, fetcher, rentcast=source)
        assert report.rent_requests == 1 and guard.used() == 1
        assert report.notes[0].startswith("RentCast stopped early:")

    def test_without_rentcast_the_free_steps_still_run(self, engine, config, fetcher, http_mock):
        store(engine, fourplex(1, 300_000.0, latitude=None, longitude=None))
        classify_all(engine, config)
        geocoder = serve_geocoder(http_mock)
        serve_fema(http_mock, [zone("X", "F")])
        report = funnel(engine, config, fetcher)
        assert (report.geocodes, report.flood_lookups, report.rent_requests) == (1, 1, 0)
        assert geocoder.call_count == 1
        assert screens(engine)[1].enrichment == {
            "geocode": "located by the Census geocoder",
            "flood": "FEMA flood map: zone X, outside the flood zone",
            "rents": "not estimated: RENTCAST_API_KEY is not set",
        }
        assert "RentCast is not connected" in report.notes[0]
        with session_scope(engine) as session:
            place = session.get_one(PropertyRow, 1)
            assert (place.latitude, place.longitude) == (39.9612, -83.0007)
            assert place.geo_cell is not None and place.in_flood_zone is False

    def test_source_failures_are_notes_not_crashes(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0, latitude=None, longitude=None))
        classify_all(engine, config)
        report = funnel(engine, config, offline)
        assert report.enriched == 1
        assert screens(engine)[1].enrichment == {
            "geocode": "not located: offline and nothing is cached yet",
            "flood": "not checked: the listing has no coordinates",
            "rents": "not estimated: RENTCAST_API_KEY is not set",
        }

    def test_switches_in_settings(self, engine, config, fetcher, http_mock):
        rule = config.settings.funnel.stage2
        rule.rent_estimates, rule.flood_zones = False, False
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        source, guard = make_source(engine, config, fetcher)
        report = funnel(engine, config, fetcher, rentcast=source)
        assert screens(engine)[1].enrichment == {} and guard.used() == 0 and report.enriched == 1

    def test_outside_the_buy_box_area_or_price_nothing_is_spent(
        self, engine, config, fetcher, http_mock
    ):
        config.settings.buy_box.max_price = 250_000
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        source, guard = make_source(engine, config, fetcher)
        report = funnel(engine, config, fetcher, rentcast=source)
        assert (report.kept, report.enriched) == (1, 0) and guard.used() == 0
        assert results(engine)[1].buy_box_misses[0] == "price above $250,000"

    def test_claude_reads_an_unclear_survivor(self, engine, config, offline):
        store(
            engine,
            csv_listing(1, "Income property, all four rented at $1,000", price=300_000.0),
            fourplex(2, 300_000.0, "Fully rented 4-plex, each unit 2bd/1ba"),
        )
        classify_all(engine, config)
        claude = FakeClaude(LlmExtraction(unit_count=4, unit_count_quote="q"))
        report = funnel(engine, config, offline, extractor=claude)
        # Only the unconfirmed one is read; the other is already at high confidence.
        assert len(claude.asked) == 1 and report.llm_calls == 1
        screen = screens(engine)[1]
        assert screen.enrichment["llm"] == "description read by Claude"
        assert (screen.type_match, screen.confidence) == ("yes", "medium")
        again = funnel(engine, config, offline, extractor=claude)
        assert again.llm_calls == 0 and len(claude.asked) == 1

    def test_a_dead_claude_key_is_reported_once(self, engine, config, offline):
        store(engine, csv_listing(1, "Income property", price=300_000.0))
        classify_all(engine, config)
        claude = FakeClaude(error=SourceUnavailable("llm", "ANTHROPIC_API_KEY was not accepted"))
        report = funnel(engine, config, offline, extractor=claude)
        assert report.notes[0] == "Claude stopped early: ANTHROPIC_API_KEY was not accepted"


class TestScoresAndBuyBox:
    def six(self, engine, config):
        """Six fourplexes in one county, priced $200k to $450k."""
        store(engine, *(fourplex(n, 150_000.0 + 50_000.0 * n) for n in range(1, 7)))
        classify_all(engine, config)

    def test_scores_compare_against_the_county(self, engine, config, offline):
        self.six(engine, config)
        report = funnel(engine, config, offline)
        found = results(engine)
        assert len(found) == 6 and all(row.score is not None for row in found.values())
        cheapest, dearest = found[1], found[6]
        assert cheapest.score > dearest.score
        parts = {part["key"]: part for part in cheapest.score_parts["parts"]}
        # The median price per door of the six is $81,250; the cheapest is $50,000 a door.
        assert cheapest.score_parts["inputs"]["county_price_per_door"] == 81_250
        assert parts["price_per_door_vs_county"]["points"] == 10
        assert "Franklin County median" in parts["cap_rate_vs_county"]["detail"]
        assert parts["cap_rate_vs_county"]["points"] > 0
        dear = {part["key"]: part for part in dearest.score_parts["parts"]}
        assert dear["price_per_door_vs_county"]["points"] == 0
        assert cheapest.score_parts["multiplier"] == 0.85  # unit count from the text: medium
        assert sum(report.grades.values()) == 6

    def test_too_few_listings_in_a_county(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0), fourplex(2, 320_000.0))
        classify_all(engine, config)
        funnel(engine, config, offline)
        parts = {part["key"]: part for part in results(engine)[1].score_parts["parts"]}
        assert parts["cap_rate_vs_county"]["points"] == 0
        assert "too few listings in Franklin County" in parts["cap_rate_vs_county"]["detail"]

    def test_buy_box(self, engine, config, offline):
        self.six(engine, config)
        report = funnel(engine, config, offline)
        found = results(engine)
        assert found[1].in_buy_box and found[1].buy_box_misses == []
        assert found[1].cash_flow_door_month >= 100 and found[1].dscr >= 1.2
        assert not found[6].in_buy_box
        assert "misses the cash flow per door target" in found[6].buy_box_misses
        assert report.in_buy_box == sum(1 for row in found.values() if row.in_buy_box) >= 1

    def test_a_state_outside_the_buy_box(self, engine, config, offline):
        config.settings.buy_box.states = ["IN"]
        store(engine, fourplex(1, 200_000.0))
        classify_all(engine, config)
        report = funnel(engine, config, offline)
        assert results(engine)[1].buy_box_misses[0] == "OH is not one of the buy box's states"
        assert report.enriched == 0 and report.in_buy_box == 0

    def test_unverified_deals_score_lower_and_stay_outside(self, engine, config, offline):
        store(engine, rentcast_listing(1, price=200_000.0), fourplex(2, 200_000.0))
        classify_all(engine, config)
        funnel(engine, config, offline)
        found = results(engine)
        assert found[1].buy_box_misses[0] == "unit count not confirmed"
        assert found[1].score_parts["multiplier"] == 0.7
        assert found[1].score < found[2].score

    def test_price_cuts_and_days_on_market_feed_the_score(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0, days_on_market=120))
        store(engine, fourplex(1, 270_000.0, days_on_market=130))  # the same listing, cut 10%
        classify_all(engine, config)
        with session_scope(engine) as session:
            assert price_cut(session.get_one(ListingRow, 1)) == pytest.approx(0.10)
        funnel(engine, config, offline)
        parts = {part["key"]: part for part in results(engine)[1].score_parts["parts"]}
        assert parts["seller_motivation"]["points"] == 8
        assert parts["seller_motivation"]["detail"] == "130 days on market; price cut 10.0%"

    def test_results_follow_the_listings(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0), fourplex(2, 300_000.0))
        classify_all(engine, config)
        funnel(engine, config, offline)
        assert set(results(engine)) == {1, 2}
        store(engine, fourplex(2, 300_000.0, status="off_market"))
        funnel(engine, config, offline)
        assert set(results(engine)) == {1} and set(screens(engine)) == {1}


class TestOverrides:
    def test_the_users_figures_win(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0, tax_annual=4000.0))
        classify_all(engine, config)
        funnel(engine, config, offline, stage2=False)
        before = results(engine)[1]
        with session_scope(engine) as session:
            set_override(session, 1, "price", 250_000)
            set_override(session, 1, "rents", [1500, 1500, 1500, 1500], note="rent roll from agent")
            set_override(session, 1, "tax_bill", 3000)
            set_override(session, 1, "insurance_per_unit_year", 700)
            assert get_overrides(session, 1)["price"] == 250_000
        funnel(engine, config, offline, stage2=False)
        after = results(engine)[1]
        assert after.cash_flow_door_month > before.cash_flow_door_month
        screen = screens(engine)[1]
        assert screen.price == 250_000
        lines = {line["key"]: line for line in screen.inputs}
        assert (lines["price"]["kind"], lines["price"]["source"]) == ("override", "your figure")
        assert lines["rents"]["source"] == "your figure: $1,500, $1,500, $1,500, $1,500"
        assert lines["tax_bill"]["value"] == 3000 and lines["tax_bill"]["kind"] == "override"
        assert lines["insurance"]["kind"] == "override"

        with session_scope(engine) as session:
            set_override(session, 1, "price", None)  # removing one
            assert "price" not in get_overrides(session, 1)
            clear_overrides(session, 1)
            assert get_overrides(session, 1) == {}

    def test_only_known_fields(self, engine):
        with (
            session_scope(engine) as session,
            pytest.raises(ValueError, match="cannot be overridden"),
        ):
            set_override(session, 1, "score", 100)

    def test_a_bad_figure_is_reported_not_fatal(self, engine, config, offline):
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        with session_scope(engine) as session:
            set_override(session, 1, "rents", [1500, 1500])  # two rents for four units
        funnel(engine, config, offline)
        assert "A figure is not usable" in screens(engine)[1].note


class TestReading:
    def test_deal_rows_and_detail(self, engine, config, offline):
        store(
            engine,
            fourplex(1, 300_000.0, "Quadplex. All units rented at $1,200/mo."),
            fourplex(2, 250_000.0),
            rentcast_listing(3, price=200_000.0),
        )
        classify_all(engine, config)
        funnel(engine, config, offline)
        with session_scope(engine) as session:
            rows = deal_rows(session, config)
            assert [row.cash_flow_door_month for row in rows] == sorted(
                (row.cash_flow_door_month for row in rows), reverse=True
            )
            by_id = {row.listing_id: row for row in rows}
            assert (by_id[1].rents_stated, by_id[1].rents_estimated) == (4, 0)
            assert (by_id[2].rents_stated, by_id[2].rents_estimated) == (0, 4)
            assert by_id[3].type_match == "unverified" and by_id[1].type_match == "yes"
            assert by_id[1].price_per_door == 75_000 and by_id[1].stage == 2
            assert by_id[1].full_address == "101 Sample St, Columbus, OH, 43215"
            assert deal_rows(session, config, "cash")  # other scenarios are stored for survivors

            detail = deal_detail(session, 1)
            assert set(detail["scenarios"]) >= {"conv_investor", "cash", "fha_house_hack"}
            assert len(detail["units"]) == 4 and detail["screen"].stage == 2
            assert detail["scenarios"]["conv_investor"].results["kpis"]["cap_rate"] > 0
            assert deal_detail(session, 999) is None

    def test_helpers(self, engine, config):
        assert source_label("rentcast") == "RentCast listing"
        assert source_label("csv:my_mls") == "your import 'my_mls'"
        store(engine, fourplex(1, 300_000.0))
        classify_all(engine, config)
        with session_scope(engine) as session:
            listing = session.get_one(ListingRow, 1)
            assert price_cut(listing) is None
            units = session.scalars(select(UnitRow)).all()
            flags = session.scalars(select(ClassificationRow)).one().flags
            request = build_request(listing, listing.property, units, flags, config, {}, [RANKING])
            assert request.rents == [None] * 4 and request.beds == [2.0] * 4
            assert request.scenarios == [RANKING] and RECORD_KEY not in request.provenance


class TestCommands:
    """The commands run offline with no keys, so only stated rent rolls can be underwritten."""

    @pytest.fixture
    def stored(self, engine, config):
        store(
            engine,
            fourplex(1, 300_000.0, "Quadplex. All units rented at $1,200/mo."),
            fourplex(2, 320_000.0, "Quadplex. Current rents: $900, $900, $950 and $950."),
            fourplex(3, 280_000.0),  # no rents stated, and no HUD rents offline
            rentcast_listing(4, price=250_000.0),
        )
        classify_all(engine, config)

    def test_underwrite_then_report(self, stored):
        from typer.testing import CliRunner

        from dealfinder.cli import app

        runner, wide = CliRunner(), {"COLUMNS": "200"}
        ran = runner.invoke(app, ["underwrite"], env=wide)
        assert ran.exit_code == 0, ran.output
        assert "Stage 1: 4 listings of the wanted types; 2 underwritten, 2 could not be." in (
            ran.output
        )
        assert "2 x No rent is stated for every unit, and HUD rents are not available" in ran.output
        assert "RentCast is not connected (RENTCAST_API_KEY)" in ran.output

        report = runner.invoke(app, ["report", "--top", "1"], env=wide)
        assert report.exit_code == 0, report.output
        assert "Ranked by cash flow per door under conv_investor." in report.output
        assert "101 Sample St, Columbus, OH, 43215" in report.output  # the better cash flow
        assert "102 Sample St" not in report.output and "Showing 1 of 2." in report.output
        assert "Estimates only." in report.output
        assert "2 that could not be underwritten (--problems)" in report.output

        other = runner.invoke(app, ["report", "--scenario", "cash"], env=wide)
        assert "under cash" in other.output and "102 Sample St" in other.output

        problems = runner.invoke(app, ["report", "--problems"], env=wide)
        assert "103 Sample St" in problems.output and "HUD_API_TOKEN is not set" in problems.output

        none = runner.invoke(app, ["report", "--state", "IN"], env=wide)
        assert "No underwritten deals to show" in none.output
        bad = runner.invoke(app, ["report", "--scenario", "nope"])
        assert bad.exit_code == 2 and "Unknown scenario nope" in bad.output

    def test_stage1_only(self, stored):
        from typer.testing import CliRunner

        from dealfinder.cli import app

        ran = CliRunner().invoke(app, ["underwrite", "--stage1-only"], env={"COLUMNS": "200"})
        assert ran.exit_code == 0, ran.output
        assert "Stage 2 was skipped, as asked." in ran.output

    def test_report_before_anything_is_underwritten(self, engine):
        from typer.testing import CliRunner

        from dealfinder.cli import app

        result = CliRunner().invoke(app, ["report"])
        assert result.exit_code == 0 and "No underwritten deals to show" in result.output
