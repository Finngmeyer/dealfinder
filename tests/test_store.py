from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from dealfinder.store import dedupe, repo
from dealfinder.store.db import session_scope, upgrade
from dealfinder.store.tables import (
    ApiUsageRow,
    Base,
    ListingRow,
    PriceEventRow,
    PropertyRow,
    RunRow,
)
from dealfinder.store.usage import BudgetExceeded, BudgetGuard, billing_period_start
from factories import listing

RULE = repo.DedupeRule(radius_m=30, price_tolerance=0.05)
SPEC_TABLES = {
    "properties",
    "listings",
    "price_events",
    "classifications",
    "units",
    "market_data",
    "underwriting",
    "overrides",
    "api_usage",
    "alerts",
    "runs",
}


class TestSchema:
    def test_every_table_in_the_spec_exists(self, engine):
        assert set(inspect(engine).get_table_names()) >= SPEC_TABLES

    def test_migrations_match_the_models(self, engine):
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []

    def test_upgrade_is_safe_to_repeat(self, engine):
        upgrade(engine)
        upgrade(engine)
        assert "listings" in inspect(engine).get_table_names()

    def test_listings_keep_the_raw_record(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(raw={"anything": ["the", "source", "sent"]}),
                                rule=RULE)  # fmt: skip
        with session_scope(engine) as session:
            assert session.scalars(select(ListingRow)).one().raw_json == {
                "anything": ["the", "source", "sent"]
            }

    def test_timestamps_round_trip_as_utc(self, engine):
        moment = datetime(2026, 11, 12, 15, 30, tzinfo=timezone_minus_five())
        with session_scope(engine) as session:
            session.add(RunRow(mode="weekly", status="running", started_at=moment))
        with session_scope(engine) as session:
            stored = session.scalars(select(RunRow)).one().started_at
        assert stored == moment
        assert stored.utcoffset() == timedelta(0)

    def test_naive_timestamps_are_refused(self, engine):
        with pytest.raises(Exception, match="time zone"), session_scope(engine) as session:
            session.add(RunRow(mode="weekly", status="running", started_at=datetime(2026, 1, 1)))


def timezone_minus_five():
    from datetime import timezone

    return timezone(timedelta(hours=-5))


class TestAddressKey:
    @pytest.mark.parametrize(
        "spelling",
        [
            "123 North Main Street",
            "123 N Main St",
            "123 N. MAIN ST.",
            "123 north main street, Apt 2",
            "123 N Main St #2",
            "123 N Main St Unit A-D",
            " 123  N  Main   St ",
        ],
    )
    def test_spellings_of_one_address_agree(self, spelling):
        assert (
            dedupe.normalize_address(spelling, "Columbus", "OH", "43215") == "123 N MAIN ST|43215"
        )

    def test_different_addresses_differ(self):
        keys = {
            dedupe.normalize_address(line, "Columbus", "OH", "43215")
            for line in ("123 N Main St", "125 N Main St", "123 S Main St", "123 N Main Ave")
        }
        assert len(keys) == 4

    def test_zip_plus_four_and_missing_zip(self):
        assert dedupe.normalize_address("1 Elm Rd", None, None, "43215-1234") == "1 ELM RD|43215"
        assert (
            dedupe.normalize_address("1 Elm Rd", "Columbus", "oh", None) == "1 ELM RD|COLUMBUS,OH"
        )
        assert dedupe.normalize_address("1 Elm Rd", None, None, None) is None
        assert dedupe.normalize_address("", "Columbus", "OH", "43215") is None
        assert dedupe.normalize_address(None, "Columbus", "OH", "43215") is None

    def test_condos_keep_their_unit(self):
        first = dedupe.normalize_address("9 Oak Ct Apt 12", "X", "OH", "43215", keep_unit=True)
        second = dedupe.normalize_address("9 Oak Court", "X", "OH", "43215", line2="Unit 12",
                                          keep_unit=True)  # fmt: skip
        other = dedupe.normalize_address("9 Oak Ct #14", "X", "OH", "43215", keep_unit=True)
        assert first == second == "9 OAK CT UNIT 12|43215"
        assert other != first


class TestGeo:
    def test_distance(self):
        assert dedupe.distance_m(39.96, -83.0, 39.96, -83.0) == 0
        assert dedupe.distance_m(39.96, -83.0, 39.96027, -83.0) == pytest.approx(30, abs=0.5)

    def test_a_point_and_its_near_neighbors_share_candidate_cells(self):
        cells = dedupe.neighbor_cells(39.9604, -83.0004)
        assert len(cells) == 9
        assert dedupe.geo_cell(39.9606, -83.0007) in cells  # about 35 m away

    def test_prices_close(self):
        assert dedupe.prices_close(400_000, 415_000, 0.05)
        assert not dedupe.prices_close(400_000, 430_000, 0.05)
        assert not dedupe.prices_close(None, 400_000, 0.05)
        assert not dedupe.prices_close(0, 400_000, 0.05)


class TestUpsert:
    def test_new_listing_creates_a_property_and_a_listed_event(self, engine):
        with session_scope(engine) as session:
            stored = repo.upsert_listing(session, listing(), rule=RULE)
            assert (stored.created, stored.new_property) == (True, True)
        with session_scope(engine) as session:
            place = session.scalars(select(PropertyRow)).one()
            assert place.address_key == "101 SAMPLE ST|43215"
            assert place.property_kind == "multifamily_2_4"
            assert place.geo_cell is not None
            events = session.scalars(select(PriceEventRow)).all()
            assert [(e.event, e.price) for e in events] == [("listed", 400_000)]

    def test_storing_the_same_listing_again_changes_nothing(self, engine):
        first_time = datetime(2026, 11, 1, tzinfo=UTC)
        later = datetime(2026, 11, 8, tzinfo=UTC)
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(), rule=RULE, now=first_time)
        with session_scope(engine) as session:
            stored = repo.upsert_listing(session, listing(), rule=RULE, now=later)
            assert not (stored.created or stored.price_changed or stored.relisted)
        with session_scope(engine) as session:
            assert len(session.scalars(select(PropertyRow)).all()) == 1
            row = session.scalars(select(ListingRow)).one()
            assert (row.first_seen_at, row.last_seen_at) == (first_time, later)
            assert len(session.scalars(select(PriceEventRow)).all()) == 1

    def test_a_price_cut_is_recorded_with_the_old_price(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(price=400_000), rule=RULE)
        with session_scope(engine) as session:
            stored = repo.upsert_listing(session, listing(price=385_000), rule=RULE)
            assert stored.price_changed
        with session_scope(engine) as session:
            events = session.scalars(select(PriceEventRow).order_by(PriceEventRow.id)).all()
            assert [(e.event, e.price, e.previous_price) for e in events] == [
                ("listed", 400_000, None),
                ("price_change", 385_000, 400_000),
            ]
            assert session.scalars(select(ListingRow)).one().price == 385_000

    def test_a_listing_that_comes_back_is_relisted(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(), rule=RULE)
            session.scalars(select(ListingRow)).one().status = "off_market"
        with session_scope(engine) as session:
            stored = repo.upsert_listing(session, listing(price=390_000), rule=RULE)
            assert stored.relisted and stored.price_changed
        with session_scope(engine) as session:
            last = session.scalars(select(PriceEventRow).order_by(PriceEventRow.id)).all()[-1]
            assert (last.event, last.price, last.previous_price) == ("relisted", 390_000, 400_000)
            assert session.scalars(select(ListingRow)).one().status == "active"

    def test_a_missing_value_does_not_erase_a_known_one(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(description="Fully rented 4-plex"), rule=RULE)
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(description=None), rule=RULE)
        with session_scope(engine) as session:
            assert session.scalars(select(ListingRow)).one().description == "Fully rented 4-plex"


class TestDedupeAcrossSources:
    def test_same_address_from_two_sources_is_one_property(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(1), rule=RULE)
            stored = repo.upsert_listing(
                session,
                listing(
                    1,
                    source="csv:mls",
                    source_listing_id="MLS-77",
                    address_line1="101 SAMPLE STREET",
                    latitude=None,
                    longitude=None,
                    unit_count=4,
                    property_kind="fourplex",
                    description="Quadplex with separate meters",
                ),
                rule=RULE,
            )
            assert stored.created and not stored.new_property
        with session_scope(engine) as session:
            place = session.scalars(select(PropertyRow)).one()
            assert len(place.listings) == 2
            # The more specific source fills in what the first one could not say.
            assert (place.property_kind, place.unit_count, place.unit_confidence) == (
                "fourplex", 4, "high",
            )  # fmt: skip

    def test_a_vaguer_source_does_not_overwrite_a_specific_kind(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(1, source="csv:mls", property_kind="fourplex"),
                                rule=RULE)  # fmt: skip
            repo.upsert_listing(session, listing(1, source_listing_id="other"), rule=RULE)
        with session_scope(engine) as session:
            assert session.scalars(select(PropertyRow)).one().property_kind == "fourplex"

    def test_close_by_and_similarly_priced_is_the_same_property(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(1, latitude=39.96000, longitude=-83.0), rule=RULE)
            stored = repo.upsert_listing(
                session,
                listing(
                    2,
                    source="csv:mls",
                    address_line1="101-103 Sample St",  # written differently, so no address match
                    latitude=39.96015,  # about 17 m north
                    longitude=-83.0,
                    price=410_000,
                ),
                rule=RULE,
            )
            assert not stored.new_property

    @pytest.mark.parametrize(
        ("latitude", "price", "reason"),
        [
            (39.96015, 480_000, "price more than 5% apart"),
            (39.96060, 400_000, "about 67 m away"),
            (39.96015, None, "no price to compare"),
        ],
    )
    def test_near_misses_stay_separate(self, engine, latitude, price, reason):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(1, latitude=39.96000, longitude=-83.0), rule=RULE)
            stored = repo.upsert_listing(
                session,
                listing(2, source="csv:mls", address_line1="7 Other Rd", latitude=latitude,
                        longitude=-83.0, price=price),
                rule=RULE,
            )  # fmt: skip
            assert stored.new_property, reason

    def test_condo_units_are_separate_properties(self, engine):
        with session_scope(engine) as session:
            for unit in ("1A", "2B"):
                repo.upsert_listing(
                    session,
                    listing(1, source_listing_id=unit, property_kind="condo",
                            address_line2=f"Unit {unit}", latitude=None, longitude=None),
                    rule=RULE,
                )  # fmt: skip
        with session_scope(engine) as session:
            assert len(session.scalars(select(PropertyRow)).all()) == 2

    def test_county_is_filled_from_the_zip_when_the_source_omits_it(self, engine):
        with session_scope(engine) as session:
            repo.upsert_listing(session, listing(county_fips=None), rule=RULE)
        with session_scope(engine) as session:
            assert session.scalars(select(PropertyRow)).one().county_fips == "39049"


class TestOffMarket:
    def setup(self, engine) -> tuple[int, int]:
        with session_scope(engine) as session:
            first = repo.start_run(session, "weekly", {})
            second = repo.start_run(session, "weekly", {})
            for number in (1, 2, 3):
                repo.upsert_listing(session, listing(number), rule=RULE, run_id=first.id)
            repo.upsert_listing(session, listing(1), rule=RULE, run_id=second.id)
            # A different source, a different state and a different type must be left alone.
            repo.upsert_listing(session, listing(4, source="csv:mls"), rule=RULE, run_id=first.id)
            repo.upsert_listing(session, listing(5, state="IN", zip_code="46204"), rule=RULE,
                                run_id=first.id)  # fmt: skip
            repo.upsert_listing(session, listing(6, property_type_raw="Condo"), rule=RULE,
                                run_id=first.id)  # fmt: skip
            return first.id, second.id

    def statuses(self, engine) -> dict[str, str]:
        with session_scope(engine) as session:
            return {
                row.source_listing_id + "/" + row.source: row.status
                for row in session.scalars(select(ListingRow))
            }

    def test_only_this_source_state_and_type_are_touched(self, engine):
        _, second = self.setup(engine)
        with session_scope(engine) as session:
            flagged = repo.mark_off_market(
                session, source="rentcast", state="OH", raw_types=["Multi-Family"], run_id=second
            )
        assert flagged == 2
        statuses = self.statuses(engine)
        assert statuses["listing-1/rentcast"] == "active"
        assert statuses["listing-2/rentcast"] == statuses["listing-3/rentcast"] == "off_market"
        assert statuses["listing-4/csv:mls"] == "active"
        assert statuses["listing-5/rentcast"] == "active"
        assert statuses["listing-6/rentcast"] == "active"
        with session_scope(engine) as session:
            events = session.scalars(
                select(PriceEventRow).where(PriceEventRow.event == "off_market")
            ).all()
            assert len(events) == 2

    def test_a_grace_sweep_can_be_required(self, engine):
        _, second = self.setup(engine)
        arguments = {"source": "rentcast", "state": "OH", "raw_types": ["Multi-Family"]}
        with session_scope(engine) as session:
            assert repo.mark_off_market(session, run_id=second, after_missed=2, **arguments) == 0
        assert self.statuses(engine)["listing-2/rentcast"] == "active"
        with session_scope(engine) as session:
            third = repo.start_run(session, "weekly", {})
            repo.upsert_listing(session, listing(2), rule=RULE, run_id=third.id)  # seen again
            assert repo.mark_off_market(session, run_id=third.id, after_missed=2, **arguments) == 1
        statuses = self.statuses(engine)
        assert statuses["listing-2/rentcast"] == "active"  # its count was reset when seen
        assert statuses["listing-3/rentcast"] == "off_market"  # missed twice in a row


class TestBudgetGuard:
    @pytest.mark.parametrize(
        ("today", "cycle_day", "expected"),
        [
            (date(2026, 11, 12), 1, date(2026, 11, 1)),
            (date(2026, 11, 12), 12, date(2026, 11, 12)),
            (date(2026, 11, 12), 13, date(2026, 10, 13)),
            (date(2026, 1, 5), 20, date(2025, 12, 20)),
            (date(2026, 3, 1), 31, date(2026, 2, 28)),  # February has no 31st
            (date(2026, 3, 31), 31, date(2026, 3, 31)),
        ],
    )
    def test_billing_period_start(self, today, cycle_day, expected):
        assert billing_period_start(today, cycle_day) == expected

    def guard(self, engine, cap=3, moment=datetime(2026, 11, 12, tzinfo=UTC), cycle_day=1):
        clock = [moment]
        return BudgetGuard(
            engine, "rentcast", cap=cap, cycle_day=cycle_day, now=lambda: clock[0]
        ), clock

    def test_requests_are_refused_once_the_cap_is_reached(self, engine):
        guard, _ = self.guard(engine)
        for _ in range(3):
            guard.check()
            guard.record("listings/sale")
        assert (guard.used(), guard.remaining()) == (3, 0)
        with pytest.raises(BudgetExceeded, match="3 of 3 requests used") as caught:
            guard.check()
        assert (caught.value.used, caught.value.cap) == (3, 3)

    def test_a_batch_that_would_overshoot_is_refused_whole(self, engine):
        guard, _ = self.guard(engine)
        guard.record("listings/sale")
        guard.check(2)
        with pytest.raises(BudgetExceeded):
            guard.check(3)

    def test_the_count_resets_with_the_billing_period(self, engine):
        guard, clock = self.guard(engine, cycle_day=15)
        for _ in range(3):
            guard.record("listings/sale")
        assert guard.used() == 3
        clock[0] = datetime(2026, 11, 15, 0, 0, 1, tzinfo=UTC)
        assert guard.used() == 0
        guard.check()

    def test_unbilled_requests_and_other_providers_do_not_count(self, engine):
        guard, clock = self.guard(engine)
        guard.record("listings/sale", billable=False)
        with Session(engine) as session:
            session.add(ApiUsageRow(provider="apify", endpoint="x", requested_at=clock[0], units=9))
            session.commit()
        assert guard.used() == 0

    def test_units_are_counted_not_rows(self, engine):
        guard, _ = self.guard(engine, cap=100)
        guard.record("actor-run", units=40)
        assert guard.remaining() == 60

    def test_no_cap_still_records(self, engine):
        guard, _ = self.guard(engine, cap=None)
        guard.record("listings/sale")
        guard.check(10_000)
        assert (guard.used(), guard.remaining()) == (1, None)

    def test_a_cap_of_zero_blocks_everything(self, engine):
        guard, _ = self.guard(engine, cap=0)
        with pytest.raises(BudgetExceeded):
            guard.check()


class TestRunsAndCheckpoints:
    def test_resume_finds_only_an_unfinished_run_with_the_same_parameters(self, engine):
        with session_scope(engine) as session:
            done = repo.start_run(session, "weekly", {"states": ["OH"]})
            repo.finish_run(session, done, status="completed", stats={}, report="")
            stopped = repo.start_run(session, "weekly", {"states": ["OH"]})
            stopped.status = "stopped_budget"
            repo.start_run(session, "weekly", {"states": ["IN"]})
            repo.start_run(session, "daily", {"states": ["OH"]})
            found = repo.resumable_run(session, "weekly", {"states": ["OH"]})
            assert found is not None and found.id == stopped.id
            assert repo.resumable_run(session, "weekly", {"states": ["MI"]}) is None

    def test_checkpoint_is_created_once_and_reused(self, engine):
        with session_scope(engine) as session:
            run = repo.start_run(session, "weekly", {})
            mark = repo.checkpoint(session, run.id, "rentcast", "OH|Multi-Family")
            assert (mark.next_offset, mark.completed) == (0, False)
            mark.next_offset = 500
            again = repo.checkpoint(session, run.id, "rentcast", "OH|Multi-Family")
            assert again.id == mark.id and again.next_offset == 500

    def test_inventory_keeps_the_latest_count(self, engine):
        with session_scope(engine) as session:
            assert repo.inventory_total(session, "rentcast", "OH|Multi-Family") is None
            repo.record_inventory(session, "rentcast", "OH|Multi-Family", 1234)
            repo.record_inventory(session, "rentcast", "OH|Multi-Family", 1300)
            assert repo.inventory_total(session, "rentcast", "OH|Multi-Family") == 1300
