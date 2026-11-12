"""Classifying stored properties, and looking up unit counts in RentCast property records."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from dealfinder.adapters.rentcast import PROPERTIES_URL, parse_property
from dealfinder.classify.llm import LLM_METHOD, LlmExtraction, LlmUnit
from dealfinder.cli import app
from dealfinder.http import SourceUnavailable
from dealfinder.pipeline.classify import (
    RECORD_KEY,
    classifier_input,
    classify_all,
    input_hash,
    resolve_units,
    unresolved_listings,
)
from dealfinder.store import repo
from dealfinder.store.db import session_scope
from dealfinder.store.tables import ClassificationRow, ListingRow, PropertyRow, UnitRow
from factories import listing, rentcast_property, rentcast_record
from test_rentcast_sweep import make_source

RULE = repo.DedupeRule(radius_m=30, price_tolerance=0.05)
WIDE = {"COLUMNS": "200"}
runner = CliRunner()


def store(engine, *listings) -> None:
    with session_scope(engine) as session:
        for item in listings:
            repo.upsert_listing(session, item, rule=RULE)


def csv_listing(number: int, description: str | None, **changes):
    """A listing as an MLS export would give it: a description, maybe a unit count."""
    fields = {
        "source": "csv:mls",
        "source_listing_id": f"MLS-{number}",
        "description": description,
        "property_kind": "multifamily_2_4",
    }
    fields.update(changes)
    return listing(number, **fields)


def properties(engine) -> list[tuple[str, int | None, str | None]]:
    with session_scope(engine) as session:
        return [
            (row.property_kind, row.unit_count, row.unit_confidence)
            for row in session.scalars(select(PropertyRow).order_by(PropertyRow.id))
        ]


class TestClassifierInput:
    def test_gathers_the_most_specific_facts_from_every_listing(self, engine):
        store(
            engine,
            listing(1),  # RentCast: "2 to 4 units", no text
            csv_listing(
                1,
                "Quadplex with separate meters",
                address_line1="101 SAMPLE STREET",
                latitude=None,
                longitude=None,
            ),
            csv_listing(
                11,
                "Longer description of the same quadplex with separate meters",
                source="csv:broker",
                address_line1="101 Sample St.",
                unit_count=4,
                property_kind="fourplex",
            ),
        )
        with session_scope(engine) as session:
            place = session.scalars(select(PropertyRow)).one()
            entry = classifier_input(place)
        assert entry.source_kind == "fourplex" and entry.stated_units == 4
        assert entry.description.startswith("Longer description")
        assert (entry.bedrooms, entry.bathrooms) == (8.0, 4.0)

    def test_the_fingerprint_follows_the_inputs(self, config, engine):
        store(engine, csv_listing(1, "Quadplex"))
        with session_scope(engine) as session:
            entry = classifier_input(session.scalars(select(PropertyRow)).one())
        digest = input_hash(entry, config.keywords)
        assert digest == input_hash(entry.model_copy(), config.keywords) and len(digest) == 32
        assert digest != input_hash(entry.model_copy(update={"bedrooms": 9}), config.keywords)
        assert digest != input_hash(entry, {**config.keywords, "new_flag": ["phrase"]})


class TestClassifyAll:
    def test_stores_kind_units_evidence_and_rents(self, engine, config):
        store(
            engine,
            csv_listing(1, "Unit 4 is vacant; units 1-3 leased at $1,100. Sold as-is."),
            csv_listing(2, "Duplex with 4 bedrooms and a 4 car garage", bedrooms=4, bathrooms=2),
            csv_listing(3, "Lot zoned for a fourplex", bedrooms=None, bathrooms=None),
            listing(4),  # RentCast only: a hint from 8 bd / 4 ba, nothing more
            csv_listing(5, None, property_kind="sfr", bedrooms=3, bathrooms=2),
        )
        report = classify_all(engine, config)
        assert properties(engine) == [
            ("fourplex", 4, "medium"),
            ("duplex", 2, "medium"),
            ("land", None, "medium"),
            ("fourplex", 4, "low"),
            ("sfr", 1, "high"),
        ]
        assert (report.properties, report.read) == (5, 5)
        assert report.by_kind == {"fourplex": 2, "duplex": 1, "land": 1, "sfr": 1}
        assert (report.in_buy_box, report.unverified, report.with_actual_rents) == (1, 1, 1)
        assert "5 properties stored; 5 read in this run, 0 unchanged." in report.text()

        with session_scope(engine) as session:
            first = session.scalars(
                select(ClassificationRow).order_by(ClassificationRow.id)
            ).first()
            assert first.method == "rules" and first.flags == ["as_is"]
            assert first.signals[0]["name"] == "unit_numbers"
            units = session.scalars(
                select(UnitRow)
                .where(UnitRow.property_id == first.property_id)
                .order_by(UnitRow.position)
            ).all()
            assert [unit.in_place_rent for unit in units] == [1100.0, 1100.0, 1100.0, None]
            assert [unit.vacant for unit in units] == [False, False, False, True]
            assert units[0].in_place_rent_quote == "units 1-3 leased at $1,100"
            assert [unit.beds for unit in units] == [2.0] * 4 and units[0].mix_confidence == "low"
            assert session.scalars(select(UnitRow)).all().__len__() == 4 + 2 + 4 + 1

    def test_unchanged_properties_are_skipped(self, engine, config):
        store(engine, csv_listing(1, "Quadplex"), csv_listing(2, "Triplex"))
        assert classify_all(engine, config).read == 2
        again = classify_all(engine, config)
        assert (again.properties, again.read) == (2, 0)
        assert again.by_kind == {"fourplex": 1, "triplex": 1}  # still counted from what is stored
        assert classify_all(engine, config, force=True).read == 2

        # A changed description is read again; the other property is not.
        store(engine, csv_listing(2, "Triplex plus detached ADU, 4 units total"))
        changed = classify_all(engine, config)
        assert changed.read == 1 and properties(engine)[1] == ("fourplex", 4, "medium")
        with session_scope(engine) as session:
            assert len(session.scalars(select(ClassificationRow)).all()) == 2

    def test_a_keyword_change_reads_everything_again(self, engine, config):
        store(engine, csv_listing(1, "Quadplex, seller says bring a toolbox"))
        classify_all(engine, config)
        config.keywords["needs_work"] = [*config.keywords["needs_work"], "bring a toolbox"]
        assert classify_all(engine, config).read == 1
        with session_scope(engine) as session:
            assert "needs_work" in session.scalars(select(ClassificationRow)).one().flags

    def test_a_reimport_does_not_undo_the_classification(self, engine, config):
        store(engine, csv_listing(1, "Quadplex"))
        classify_all(engine, config)
        store(engine, csv_listing(1, "Quadplex", price=390_000.0))  # same text, new price
        assert properties(engine) == [("fourplex", 4, "medium")]
        assert classify_all(engine, config).read == 0

    def test_market_rent_survives_unless_the_layout_changes(self, engine, config):
        store(engine, csv_listing(1, "Quadplex", bedrooms=8, bathrooms=4))
        classify_all(engine, config)
        with session_scope(engine) as session:
            for unit in session.scalars(select(UnitRow)):
                unit.market_rent, unit.market_rent_source = 1250.0, "test"
        # New wording, same layout: the rents already looked up still apply.
        store(engine, csv_listing(1, "Quadplex with separate meters", bedrooms=8, bathrooms=4))
        classify_all(engine, config)
        with session_scope(engine) as session:
            assert [unit.market_rent for unit in session.scalars(select(UnitRow))] == [1250.0] * 4
        # The text now gives a different layout for the units, and drops one of them.
        store(engine, csv_listing(1, "Triplex, each unit 1bd/1ba", bedrooms=8, bathrooms=4))
        classify_all(engine, config)
        with session_scope(engine) as session:
            units = session.scalars(select(UnitRow).order_by(UnitRow.position)).all()
            assert [(unit.beds, unit.market_rent) for unit in units] == [(1.0, None)] * 3


class FakeClaude:
    """Stands in for ClaudeExtractor: returns a fixed answer, or raises."""

    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self.answer, self.error, self.asked = answer, error, []

    def __call__(self, description: str):
        self.asked.append(description)
        if self.error:
            raise self.error
        return self.answer


class TestClaudeStep:
    VAGUE = "Income property with good tenants in every apartment"

    def test_only_uncertain_buildings_are_sent(self, engine, config):
        store(
            engine,
            csv_listing(1, self.VAGUE),  # the rules find no count
            csv_listing(2, "Fully rented 4-plex, each unit 2bd/1ba"),  # already high
            csv_listing(3, "Quadplex"),  # medium: sent, since the setting says "below high"
            csv_listing(4, "Who knows", unit_count=4),  # the source states the count
            csv_listing(5, "Cute house", property_kind="sfr"),
            csv_listing(6, "Lot zoned for a fourplex", bedrooms=None, bathrooms=None),
            listing(7),  # no description
        )
        claude = FakeClaude(LlmExtraction(unit_count=4, unit_count_quote="q"))
        report = classify_all(engine, config, extractor=claude)
        assert claude.asked == [self.VAGUE, "Quadplex"] and report.llm_calls == 2
        assert properties(engine)[0] == ("fourplex", 4, "medium")
        with session_scope(engine) as session:
            methods = [
                row.method
                for row in session.scalars(select(ClassificationRow).order_by(ClassificationRow.id))
            ]
        assert methods == [LLM_METHOD, "rules", LLM_METHOD, "rules", "rules", "rules", "rules"]
        assert "Claude read 2 descriptions." in report.text()

    def test_the_threshold_is_a_setting(self, engine, config):
        config.settings.classify.llm.only_below_confidence = "medium"
        store(engine, csv_listing(1, self.VAGUE), csv_listing(2, "Quadplex"))
        claude = FakeClaude(None)
        classify_all(engine, config, extractor=claude)
        assert claude.asked == [self.VAGUE]

    def test_nothing_is_asked_twice(self, engine, config):
        store(engine, csv_listing(1, self.VAGUE))
        claude = FakeClaude(None)  # asked, had nothing to add
        assert classify_all(engine, config, extractor=claude).llm_calls == 1
        assert classify_all(engine, config, extractor=claude).llm_calls == 0
        assert classify_all(engine, config).read == 0  # and the rules alone leave it be
        assert len(claude.asked) == 1

    def test_a_rules_only_pass_is_caught_up_later(self, engine, config):
        store(engine, csv_listing(1, self.VAGUE))
        classify_all(engine, config)  # what an import does on its own
        claude = FakeClaude(
            LlmExtraction(
                unit_count=3,
                unit_count_quote="q",
                units=[LlmUnit(unit=2, rent=875, quote="q")],
            )
        )
        report = classify_all(engine, config, extractor=claude)
        assert (report.read, report.llm_calls) == (1, 1)
        assert properties(engine) == [("triplex", 3, "medium")]
        with session_scope(engine) as session:
            rents = [
                u.in_place_rent for u in session.scalars(select(UnitRow).order_by(UnitRow.position))
            ]
        assert rents == [None, 875.0, None]

    def test_the_cap_on_calls(self, engine, config):
        config.settings.classify.llm.max_calls_per_run = 2
        store(engine, *(csv_listing(n, f"{self.VAGUE} number {n}") for n in range(1, 6)))
        claude = FakeClaude(None)
        report = classify_all(engine, config, extractor=claude)
        assert report.llm_calls == 2 and "max_calls_per_run (2)" in report.llm_note
        # The rest are picked up by the next run.
        assert classify_all(engine, config, extractor=claude).llm_calls == 2
        assert len(claude.asked) == 4

    def test_a_dead_key_stops_the_asking_not_the_run(self, engine, config):
        store(engine, *(csv_listing(n, f"{self.VAGUE} number {n}") for n in range(1, 4)))
        claude = FakeClaude(error=SourceUnavailable("llm", "ANTHROPIC_API_KEY was not accepted"))
        report = classify_all(engine, config, extractor=claude)
        assert len(claude.asked) == 1 and report.read == 3
        assert report.llm_note == "Stopped asking: ANTHROPIC_API_KEY was not accepted."
        with session_scope(engine) as session:
            assert {row.method for row in session.scalars(select(ClassificationRow))} == {"rules"}


def serve_properties(http_mock, records: dict[int, dict | None]):
    """A stand-in for RentCast's property-record endpoint. None means it has no record."""
    by_id = {rentcast_record(number)["id"]: record for number, record in records.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        record = by_id.get(request.url.path.rsplit("/", 1)[-1].replace("%2C", ","))
        if record is None:
            return httpx.Response(404, json={"status": 404, "error": "resource/not-found"})
        return httpx.Response(200, json=record)

    return http_mock.get(url__startswith=f"{PROPERTIES_URL}/").mock(side_effect=handler)


def rentcast_listing(number: int, **changes):
    fields = {"source_listing_id": rentcast_record(number)["id"]}
    fields.update(changes)
    return listing(number, **fields)


class TestPropertyRecord:
    def test_parse(self):
        import json

        record = parse_property(json.dumps(rentcast_property(1)).encode())
        assert (record.unit_count, record.tax_annual, record.tax_year) == (4, 5760.0, 2025)
        assert record.owner_occupied is False and record.year_built == 1978

    def test_parse_tolerates_missing_parts(self):
        import json

        bare = {"id": "x", "features": None, "propertyTaxes": {"2025": {"total": None}}}
        record = parse_property(json.dumps([bare]).encode())  # a search returns a list
        assert (record.unit_count, record.tax_annual, record.owner_occupied) == (None, None, None)

    @pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"error": "x"}', b'"text"'])
    def test_parse_rejects_what_is_not_a_record(self, body):
        with pytest.raises(SourceUnavailable):
            parse_property(body)

    def test_lookup_is_billed_once_then_cached(self, engine, config, fetcher, http_mock):
        route = serve_properties(http_mock, {1: rentcast_property(1)})
        source, guard = make_source(engine, config, fetcher)
        listing_id = rentcast_record(1)["id"]
        assert source.property_record(listing_id).unit_count == 4
        assert source.property_record(listing_id).unit_count == 4
        assert route.call_count == 1 and guard.used() == 1 and source.requests_billed == 1
        assert route.calls[0].request.headers["X-Api-Key"] == "rc-key"

    def test_no_key(self, engine, config, fetcher):
        source, _ = make_source(engine, config, fetcher, key=None)
        with pytest.raises(SourceUnavailable, match="RENTCAST_API_KEY is not set"):
            source.property_record("x")

    def test_a_missing_record_is_a_404_and_is_not_billed(self, engine, config, fetcher, http_mock):
        serve_properties(http_mock, {})
        source, guard = make_source(engine, config, fetcher)
        with pytest.raises(SourceUnavailable) as caught:
            source.property_record("nowhere")
        assert caught.value.status == 404 and guard.used() == 0


class TestResolveUnits:
    def test_fills_in_unit_count_tax_bill_and_gaps(self, engine, config, fetcher, http_mock):
        store(engine, rentcast_listing(1, year_built=None))
        classify_all(engine, config)
        assert properties(engine) == [("fourplex", 4, "low")]  # a hint only
        serve_properties(http_mock, {1: rentcast_property(1)})
        source, guard = make_source(engine, config, fetcher)

        report = resolve_units(engine, config, source)
        assert (report.candidates, report.looked_up, report.requests) == (1, 1, 1)
        assert (report.with_unit_count, report.with_tax_bill, report.no_record) == (1, 1, 0)
        assert properties(engine) == [("fourplex", 4, "high")] and guard.used() == 1
        with session_scope(engine) as session:
            row = session.scalars(select(ListingRow)).one()
            assert (row.unit_count_stated, row.tax_annual) == (4, 5760.0)
            note = row.raw_json[RECORD_KEY]
            assert note["found"] is True and note["tax_year"] == 2025 and note["unit_count"] == 4
            assert row.property.year_built == 1978  # a gap filled from the record
            signal = session.scalars(select(ClassificationRow)).one().signals[0]
            assert signal["name"] == "stated_by_source"
        assert "looked up 1 (1 billed)" in report.text()

        # Nothing left to do, and a re-sweep of the listing keeps what was learned.
        assert resolve_units(engine, config, source).candidates == 0
        store(engine, rentcast_listing(1, price=395_000.0))
        with session_scope(engine) as session:
            row = session.scalars(select(ListingRow)).one()
            assert RECORD_KEY in row.raw_json and row.tax_annual == 5760.0
        assert classify_all(engine, config).read == 0
        assert properties(engine) == [("fourplex", 4, "high")]

    def test_a_record_can_show_it_is_not_a_fourplex(self, engine, config, fetcher, http_mock):
        store(engine, rentcast_listing(1))
        classify_all(engine, config)
        serve_properties(http_mock, {1: rentcast_property(1, features={"unitCount": 2})})
        source, _ = make_source(engine, config, fetcher)
        resolve_units(engine, config, source)
        assert properties(engine) == [("duplex", 2, "high")]

    def test_no_record_and_no_unit_count_stay_unverified(self, engine, config, fetcher, http_mock):
        store(engine, rentcast_listing(1), rentcast_listing(2))
        classify_all(engine, config)
        serve_properties(http_mock, {1: None, 2: rentcast_property(2, features={})})
        source, guard = make_source(engine, config, fetcher)
        report = resolve_units(engine, config, source)
        assert (report.looked_up, report.requests, report.no_record) == (2, 1, 1)
        assert (report.with_unit_count, report.with_tax_bill) == (0, 1)
        assert properties(engine) == [("fourplex", 4, "low")] * 2 and guard.used() == 1
        # Both were checked, so neither is asked about again.
        assert resolve_units(engine, config, source).candidates == 0
        with session_scope(engine) as session:
            notes = [
                row.raw_json[RECORD_KEY]["found"] for row in session.scalars(select(ListingRow))
            ]
        assert notes == [False, True]

    def test_the_budget_stops_it_and_the_rest_wait(self, engine, config, fetcher, http_mock):
        store(engine, *(rentcast_listing(n, price=300_000.0 + n) for n in range(1, 5)))
        classify_all(engine, config)
        serve_properties(http_mock, {n: rentcast_property(n) for n in range(1, 5)})
        source, guard = make_source(engine, config, fetcher, cap=2)
        report = resolve_units(engine, config, source)
        assert (report.candidates, report.looked_up, report.requests) == (4, 2, 2)
        assert report.stopped.startswith("Stopped:") and guard.used() == 2
        assert [confidence for _, _, confidence in properties(engine)] == [
            "high", "high", "low", "low",
        ]  # fmt: skip
        with session_scope(engine) as session:
            assert len(unresolved_listings(session, config)) == 2

    def test_limit(self, engine, config, fetcher, http_mock):
        store(engine, *(rentcast_listing(n) for n in range(1, 4)))
        classify_all(engine, config)
        serve_properties(http_mock, {n: rentcast_property(n) for n in range(1, 4)})
        source, guard = make_source(engine, config, fetcher)
        assert resolve_units(engine, config, source, limit=1).looked_up == 1 and guard.used() == 1

    def test_an_outage_stops_without_marking_anything(self, engine, config, fetcher, http_mock):
        store(engine, rentcast_listing(1))
        classify_all(engine, config)
        http_mock.get(url__startswith=f"{PROPERTIES_URL}/").mock(return_value=httpx.Response(401))
        source, _ = make_source(engine, config, fetcher)
        report = resolve_units(engine, config, source)
        assert report.looked_up == 0 and "key or token was not accepted" in report.stopped
        with session_scope(engine) as session:
            assert len(unresolved_listings(session, config)) == 1

    def test_which_listings_and_in_what_order(self, engine, config):
        store(
            engine,
            rentcast_listing(1, price=500_000.0, bedrooms=4, bathrooms=2),  # hints at a duplex
            rentcast_listing(2, price=450_000.0),  # hints at a fourplex
            rentcast_listing(3, price=300_000.0, bedrooms=None, bathrooms=None),  # no hint
            rentcast_listing(4, price=200_000.0),  # fourplex hint, cheapest
            rentcast_listing(5, price=900_000.0),  # over the buy box price
            rentcast_listing(6, status="off_market"),
            rentcast_listing(7, property_kind="sfr"),
            csv_listing(8, "Quadplex"),  # already confirmed by its text
            csv_listing(9, None),  # not RentCast: there is no record id to look up
        )
        classify_all(engine, config)
        with session_scope(engine) as session:
            order = [row.price for row in unresolved_listings(session, config)]
        assert order == [200_000.0, 450_000.0, 300_000.0, 500_000.0]

        config.settings.buy_box.property_types = ["sfr"]
        with session_scope(engine) as session:
            assert unresolved_listings(session, config) == []


class TestCommands:
    def test_classify_one_description(self):
        result = runner.invoke(
            app,
            ["classify", "Unit 4 is vacant; units 1-3 leased at $1,100", "--beds", "8"],
            env=WIDE,
        )
        assert result.exit_code == 0, result.output
        assert "fourplex, units: 4, confidence: medium (a fourplex)" in result.output
        assert "unit numbers" in result.output and "$1,100 actual" in result.output
        assert "vacant" in result.output

    def test_classify_json_and_tags(self):
        result = runner.invoke(
            app, ["classify", "Investor special 4 unit building, cash only", "--json"]
        )
        assert '"unit_count": 4' in result.output and '"cash_only"' in result.output

    def test_classify_with_a_stated_count_and_a_bad_kind(self):
        stated = runner.invoke(app, ["classify", "Nice building", "--units", "4"], env=WIDE)
        assert "confidence: high" in stated.output and "the source lists 4 units" in stated.output
        bad = runner.invoke(app, ["classify", "Nice building", "--kind", "castle"])
        assert bad.exit_code == 2 and "'castle' is not a property kind" in bad.output

    def test_classify_eval(self, tmp_path):
        path = tmp_path / "labeled.csv"
        path.write_text("description,units\nQuadplex,4\nNice duplex,2\n", encoding="utf-8")
        result = runner.invoke(app, ["classify-eval", str(path)], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "2 labeled descriptions" in result.output
        assert "Fourplex precision 100.0% (1 right of 1 called a fourplex)" in result.output

    def test_classify_eval_with_a_bad_file(self, tmp_path):
        missing = runner.invoke(app, ["classify-eval", str(tmp_path / "nope.csv")])
        assert missing.exit_code == 2
        path = tmp_path / "bad.csv"
        path.write_text("text\nQuadplex\n", encoding="utf-8")
        bad = runner.invoke(app, ["classify-eval", str(path)])
        assert bad.exit_code == 2 and "needs a 'description' column" in bad.output

    def test_classify_all(self, engine):
        store(engine, csv_listing(1, "Quadplex"), listing(2))
        result = runner.invoke(app, ["classify-all"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "2 properties stored; 2 read in this run, 0 unchanged." in result.output
        assert "1 confirmed, 1 with an unconfirmed unit count" in result.output
        assert "Claude" not in result.output  # off in settings, so not mentioned

    def test_classify_all_with_claude_but_no_key(self, engine):
        store(engine, csv_listing(1, "Some building"))
        result = runner.invoke(app, ["classify-all", "--llm"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "Claude is not reading descriptions: ANTHROPIC_API_KEY is not set." in result.output
        assert "1 properties stored" in result.output

    def test_resolve_units_dry_run(self, engine):
        store(engine, rentcast_listing(1), rentcast_listing(2))
        runner.invoke(app, ["classify-all"])
        result = runner.invoke(app, ["resolve-units", "--dry-run", "--limit", "1"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "2 listings have no confirmed unit count." in result.output
        assert "Looking up 1 would take up to 1 billed requests. Budget: 50 left of 50." in (
            result.output
        )

    def test_resolve_units_needs_a_key(self, engine):
        result = runner.invoke(app, ["resolve-units"])
        assert result.exit_code == 2 and "RENTCAST_API_KEY is not set" in result.output

    def test_resolve_units_online(self, engine, http_mock, monkeypatch):
        monkeypatch.delenv("DEALFINDER_OFFLINE")
        monkeypatch.setenv("RENTCAST_API_KEY", "rc-key")
        store(engine, rentcast_listing(1))
        runner.invoke(app, ["classify-all"])
        serve_properties(http_mock, {1: rentcast_property(1)})
        result = runner.invoke(app, ["resolve-units"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "Unit count found for 1; tax bill for 1; no record for 0." in result.output
        listed = runner.invoke(app, ["listings"], env=WIDE)
        assert "fourplex: 1 confirmed, 0 with an unconfirmed unit count" in listed.output
        assert "101 Sample St" in listed.output

    def test_an_import_classifies_what_it_stored(self, engine, config_dir, monkeypatch):
        from pathlib import Path

        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        example = Path(__file__).resolve().parents[1] / "examples" / "sample_mls_export.csv"
        result = runner.invoke(app, ["import-csv", str(example), "--mapping", "sample"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "7 properties stored; 7 read in this run, 0 unchanged." in result.output
        assert properties(engine) == [
            ("fourplex", 4, "high"),  # stated by the export, and the text agrees
            ("fourplex", 4, "medium"),  # "Quadplex"
            ("fourplex", 4, "medium"),  # units 1-3 leased, unit 4 vacant
            ("duplex", 2, "high"),
            ("multifamily_5plus", 8, "medium"),  # two fourplexes
            ("fourplex", 4, "medium"),  # triplex plus ADU
            ("land", None, "high"),
        ]
        listed = runner.invoke(app, ["listings", "--status", "all"], env=WIDE)
        assert "fourplex: 4 confirmed, 0 with an unconfirmed unit count, 3 of other kinds" in (
            listed.output
        )


class TestLabelSheet:
    def test_writes_a_sheet_that_scores(self, engine, tmp_path):
        store(
            engine,
            csv_listing(1, "Quadplex with separate meters", unit_count=4),
            csv_listing(2, "Charming duplex", unit_count=2),
            csv_listing(3, "Income property, call for details"),
            csv_listing(4, "Lot zoned for a fourplex", property_kind="land"),
            listing(5),  # no description: left out
        )
        target = tmp_path / "labeled" / "descriptions.csv"
        result = runner.invoke(app, ["label-sheet", "--path", str(target)], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "Added 4 descriptions" in result.output and "(0 were already there)" in result.output
        assert "3 carry the unit count the source stated; 1 need the 'units' column" in (
            result.output
        )
        lines = target.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "description,units,source_kind,bedrooms,bathrooms,address"
        assert lines[1] == 'Quadplex with separate meters,4,,8,4,"101 Sample St, Columbus, OH"'
        assert lines[3].startswith('"Income property, call for details",,,')
        assert lines[4].startswith("Lot zoned for a fourplex,land,land,")

        scored = runner.invoke(app, ["classify-eval", str(target)], env=WIDE)
        assert "3 labeled descriptions" in scored.output  # the blank row is not scored
        assert "Fourplex precision 100.0%" in scored.output

    def test_labels_typed_by_hand_survive_a_second_run(self, engine, tmp_path):
        store(engine, csv_listing(1, "Income property, call for details"))
        target = tmp_path / "sheet.csv"
        runner.invoke(app, ["label-sheet", "--path", str(target)])
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                '"Income property, call for details",', '"Income property, call for details",3'
            ),
            encoding="utf-8",
        )
        store(engine, csv_listing(2, "Brand new listing"))
        again = runner.invoke(app, ["label-sheet", "--path", str(target)], env=WIDE)
        assert "Added 1 descriptions" in again.output and "(1 were already there)" in again.output
        lines = target.read_text(encoding="utf-8").splitlines()
        assert lines[1].startswith('"Income property, call for details",3,') and len(lines) == 3

    def test_nothing_to_write(self, engine):
        result = runner.invoke(app, ["label-sheet"])
        assert result.exit_code == 2 and "No stored listing has a description" in result.output
