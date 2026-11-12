from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from dealfinder.analysis import DISCLAIMER
from dealfinder.cli import app, parse_rents

runner = CliRunner()
WIDE = {"COLUMNS": "200"}

GOLDEN = [
    "analyze",
    "--price",
    "480000",
    "--rents",
    "1350x4",
    "--zip",
    "43215",
    "--year-built",
    "1978",
    "--tax-rate",
    "1.2",
    "--vacancy",
    "6",
    "--insurance",
    "900",
]


class TestParseRents:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1350,1350,1200", [1350, 1350, 1200]),
            ("1350x4", [1350] * 4),
            ("1350x2, 1200, $950", [1350, 1350, 1200, 950]),
            ("1100.50", [1100.5]),
            ("1350X3", [1350] * 3),
        ],
    )
    def test_formats(self, text, expected):
        assert parse_rents(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "1350xfour", ","])
    def test_rejects_nonsense(self, text):
        with pytest.raises(typer.BadParameter):
            parse_rents(text)


def test_golden_scenario_a():
    result = runner.invoke(
        app,
        [*GOLDEN, "--management", "manager", "--scenario", "conv_investor", "--rate", "7.0"],
        env=WIDE,
    )
    assert result.exit_code == 0, result.output
    for expected in (
        "$45.02",
        "1.61%",
        "1.075",
        "1.701",
        "90.67%",
        "$3,656.92",
        "9.08%",
        "1.522",
        "$413,431",
        "CoC",
        "43215 Franklin County, OH",
    ):
        assert expected in result.output, expected
    assert DISCLAIMER in result.output


def test_golden_scenario_b():
    result = runner.invoke(
        app, [*GOLDEN, "--scenario", "fha_house_hack", "--rate", "6.5"], env=WIDE
    )
    assert result.exit_code == 0, result.output
    for expected in (
        "$471,306",
        "$2,978.97",
        "$212.30",
        "$1,428.27",
        "$3,971.27",
        "$4,050.00",
        "passes by $78.73",
        "$490,293",
        "-$154.03",
    ):
        assert expected in result.output, expected


def test_price_rents_and_zip_are_enough():
    result = runner.invoke(
        app, ["analyze", "--price", "480000", "--rents", "1350x4", "--zip", "43215"], env=WIDE
    )
    assert result.exit_code == 0, result.output
    for name in ("fha_house_hack", "conv_owner_5", "conv_investor", "dscr", "cash"):
        assert name in result.output
    assert "commercial: not offered for 4 units" in result.output
    assert "stand-in" in result.output


def test_json_output():
    result = runner.invoke(
        app,
        [
            *GOLDEN,
            "--management",
            "manager",
            "--scenario",
            "conv_investor",
            "--rate",
            "7",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    scenario = data["scenarios"][0]
    assert scenario["terms"]["name"] == "conv_investor"
    assert scenario["kpis"]["cash_flow_door_month"] == pytest.approx(45.02, abs=0.01)
    assert scenario["engine_version"]
    assert data["place"]["county_fips"] == "39049"
    assert {line["kind"] for line in data["inputs"]} <= {"actual", "estimated", "override"}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--scenario", "hard_money"], "Unknown scenario hard_money"),
        (["--market-rents", "1500x3"], "market rents"),
        (["--owner-unit", "9"], "owner unit"),
        (["--management", "nobody"], "self or manager"),
        (["--vacancy", "150"], "vacancy_rate"),
    ],
)
def test_bad_input_exits_with_a_message(extra, message):
    result = runner.invoke(app, [*GOLDEN, *extra])
    assert result.exit_code == 2
    assert message in result.output


def test_bad_rents():
    result = runner.invoke(app, ["analyze", "--price", "1", "--rents", "abc", "--zip", "43215"])
    assert result.exit_code == 2
    assert "not a rent" in result.output


def test_settings_command_shows_files_and_key_status(monkeypatch):
    monkeypatch.setenv("HUD_API_TOKEN", "do-not-print-me")
    result = runner.invoke(app, ["settings"], env=WIDE)
    assert result.exit_code == 0, result.output
    assert "settings.yaml" in result.output
    assert "HUD_API_TOKEN: set" in result.output
    assert "RENTCAST_API_KEY: not set" in result.output
    assert "do-not-print-me" not in result.output
    assert "ranking_scenario: conv_investor" in result.output


class TestMarketCommand:
    def test_offline_with_an_empty_cache_still_reports(self):
        result = runner.invoke(app, ["market", "--zip", "43215"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "43215 Franklin County, OH" in result.output
        assert "offline and nothing is cached yet" in result.output
        assert "HUD_API_TOKEN is not set" in result.output
        assert "$1,136,750" in result.output  # county FHA limit comes from config, not the network
        assert "stand-in" in result.output

    def test_live_sources_are_shown(self, http_mock, monkeypatch):
        from test_market_context import mock_everything

        monkeypatch.delenv("DEALFINDER_OFFLINE")
        monkeypatch.setenv("HUD_API_TOKEN", "hud-token")
        monkeypatch.setenv("CENSUS_API_KEY", "census-key")
        mock_everything(http_mock)
        result = runner.invoke(app, ["market", "--zip", "43215"], env=WIDE)
        assert result.exit_code == 0, result.output
        for expected in (
            "6.88%",  # base rate
            "7.63%",  # conv_investor: base + 0.75
            "$1,500",  # two-bedroom Small Area FMR
            "$1,340",  # net of the utility allowance
            "$1,560",  # Zillow index
            "+4.0% over 12 months",
            "1.44%",  # county tax rate
            "6.40%",  # vacancy: 5.4% + 1 point
            "Relatively High",
            "This product uses the HUD User Data API but is not endorsed or certified by HUD User.",
        ):
            assert expected in result.output, expected

    def test_refresh_downloads_again(self, http_mock, monkeypatch):
        from test_market_context import mock_everything

        monkeypatch.delenv("DEALFINDER_OFFLINE")
        routes = mock_everything(http_mock)
        runner.invoke(app, ["market", "--zip", "43215"], env=WIDE)
        runner.invoke(app, ["market", "--zip", "43215"], env=WIDE)
        assert routes["fred"].call_count == 1
        runner.invoke(app, ["market", "--zip", "43215", "--refresh"], env=WIDE)
        assert routes["fred"].call_count == 2

    def test_json(self):
        result = runner.invoke(app, ["market", "--zip", "43215", "--json"])
        data = json.loads(result.output)
        assert data["place"]["county_fips"] == "39049"
        assert {source["key"] for source in data["sources"]} == {
            "fred",
            "hud_fmr",
            "acs",
            "zori",
            "nri",
        }

    def test_not_a_zip(self):
        result = runner.invoke(app, ["market", "--zip", "4321"])
        assert result.exit_code == 2
        assert "not a 5-digit ZIP" in result.output


class TestAnalyzeWithMarketData:
    def test_market_data_fills_the_gaps(self, http_mock, monkeypatch):
        from test_market_context import mock_everything

        monkeypatch.delenv("DEALFINDER_OFFLINE")
        monkeypatch.setenv("HUD_API_TOKEN", "hud-token")
        monkeypatch.setenv("CENSUS_API_KEY", "census-key")
        mock_everything(http_mock)
        result = runner.invoke(
            app,
            [
                "analyze",
                "--price",
                "480000",
                "--rents",
                "1350x4",
                "--zip",
                "43215",
                "--beds",
                "2x4",
                "--scenario",
                "conv_investor",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        deal = data["deal"]
        assert deal["tax_rate"] == pytest.approx(0.0144)
        assert deal["vacancy_rate"] == pytest.approx(0.064)
        assert deal["insurance_per_unit_year"] == 1350  # Relatively High hazard rating
        assert deal["units"][0]["market_rent"] == 1340
        assert data["rates"]["conv_investor"]["rate"] == pytest.approx(0.0688 + 0.0075)
        assert data["attributions"]

    def test_no_market_flag_skips_the_lookup(self, http_mock, monkeypatch):
        monkeypatch.delenv("DEALFINDER_OFFLINE")  # any request would now fail the test
        result = runner.invoke(
            app,
            ["analyze", "--price", "480000", "--rents", "1350x4", "--zip", "43215", "--no-market"],
            env=WIDE,
        )
        assert result.exit_code == 0, result.output
        assert "Market data" not in result.output

    def test_offline_flag(self, http_mock, monkeypatch):
        monkeypatch.delenv("DEALFINDER_OFFLINE")
        result = runner.invoke(
            app,
            ["analyze", "--price", "480000", "--rents", "1350x4", "--zip", "43215", "--offline"],
            env=WIDE,
        )
        assert result.exit_code == 0, result.output
        assert "offline and nothing is cached yet" in result.output


class TestSweepCommands:
    def online(self, monkeypatch, http_mock, records_by_state):
        from test_rentcast_sweep import serve

        monkeypatch.delenv("DEALFINDER_OFFLINE")
        monkeypatch.setenv("RENTCAST_API_KEY", "rc-key")
        return serve(http_mock, records_by_state)

    def test_dry_run_needs_no_key_and_sends_nothing(self, engine):
        result = runner.invoke(
            app, ["sweep", "--mode", "weekly", "--states", "OH,in", "--dry-run"], env=WIDE
        )
        assert result.exit_code == 0, result.output
        for expected in (
            "Dry run: weekly sweep of 2 states for fourplex. Nothing was fetched.",
            "no count yet: at least one request",
            "This run: at least 2 billed requests.",
            "Budget: 0 of 50 used this billing period, 50 left. The run fits.",
            "developer   $0.00 a month",
            "growth      $199.00 a month",
        ):
            assert expected in result.output, expected

    def test_dry_run_of_every_state_shows_it_will_not_fit_a_daily_budget(self, engine):
        result = runner.invoke(app, ["sweep", "--mode", "daily", "--dry-run"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "daily sweep of 49 states" in result.output
        assert "about 1,490 requests" in result.output
        assert "developer   $288.00 a month" in result.output  # 1,440 over the free 50 at $0.20

    def test_a_real_sweep_without_a_key_explains_itself(self, engine):
        result = runner.invoke(app, ["sweep", "--mode", "weekly", "--states", "OH"])
        assert result.exit_code == 2
        assert "RENTCAST_API_KEY is not set" in result.output
        assert "--dry-run" in result.output

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            (["sweep", "--mode", "monthly"], "Use daily or weekly"),
            (["sweep", "--mode", "weekly", "--states", "OH,AK"], "AK not covered"),
        ],
    )
    def test_bad_arguments(self, engine, arguments, message):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2
        assert message in result.output

    def test_sweep_then_listings_then_usage(self, engine, http_mock, monkeypatch):
        from factories import rentcast_record

        self.online(monkeypatch, http_mock, {"OH": [rentcast_record(n) for n in (1, 2, 3)]})
        swept = runner.invoke(app, ["sweep", "--mode", "weekly", "--states", "OH"], env=WIDE)
        assert swept.exit_code == 0, swept.output
        assert "Run 1: weekly sweep, completed" in swept.output
        assert "Requests billed now: 1. This billing period: 1 of 50." in swept.output
        assert "Listings seen 3; new 3" in swept.output

        # RentCast gives no unit count, so nothing is a confirmed fourplex yet: 8 bedrooms and
        # 4 baths only hint at one.
        assert "0 confirmed, 3 with an unconfirmed unit count" in swept.output
        listed = runner.invoke(app, ["listings", "--state", "oh"], env=WIDE)
        assert listed.exit_code == 0, listed.output
        assert "fourplex: 0 confirmed, 3 with an unconfirmed unit count, 0 of other kinds" in (
            listed.output
        )
        assert "Sample St" not in listed.output and "--unverified" in listed.output

        maybes = runner.invoke(app, ["listings", "--state", "oh", "--unverified"], env=WIDE)
        assert "101 Sample St, Columbus, OH, 43215" in maybes.output
        assert "$401,000" in maybes.output and "4?" in maybes.output
        assert "resolve-units" in maybes.output

        everything = runner.invoke(app, ["listings", "--types", "all"], env=WIDE)
        assert everything.output.count("Sample St") == 3
        unknown = runner.invoke(app, ["listings", "--types", "castle"], env=WIDE)
        assert unknown.exit_code == 2 and "Unknown property type castle" in unknown.output

        usage = runner.invoke(app, ["usage"], env=WIDE)
        assert usage.exit_code == 0, usage.output
        assert "1 of 50 requests used" in usage.output and "49 left" in usage.output
        assert "weekly" in usage.output and "completed" in usage.output

        # With a count on hand, the next dry run knows the price exactly.
        plan = runner.invoke(
            app, ["sweep", "--mode", "weekly", "--states", "OH", "--dry-run"], env=WIDE
        )
        assert "This run: 1 billed requests." in plan.output
        assert "3 listings last counted" in plan.output

    def test_the_cap_from_settings_stops_the_sweep(
        self, engine, http_mock, monkeypatch, config_dir
    ):
        from factories import rentcast_record

        (config_dir / "settings.local.yaml").write_text(
            "budgets: {rentcast: {monthly_request_cap: 1}}\nsweep: {page_size: 2}\n"
        )
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        route = self.online(monkeypatch, http_mock, {"OH": [rentcast_record(n) for n in range(5)]})
        result = runner.invoke(app, ["sweep", "--mode", "weekly", "--states", "OH"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "stopped budget" in result.output
        assert "1 of 1 requests used" in result.output
        assert route.call_count == 1

    def test_an_outage_exits_nonzero_for_the_scheduler(self, engine, http_mock, monkeypatch):
        import httpx

        from dealfinder import http as http_module
        from dealfinder.adapters.rentcast import LISTINGS_URL

        monkeypatch.delenv("DEALFINDER_OFFLINE")
        monkeypatch.setenv("RENTCAST_API_KEY", "rc-key")
        monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
        http_mock.get(LISTINGS_URL).mock(return_value=httpx.Response(503))
        result = runner.invoke(app, ["sweep", "--mode", "weekly", "--states", "OH"], env=WIDE)
        assert result.exit_code == 1
        assert "failed" in result.output and "HTTP 503" in result.output

    def test_probe(self, engine, http_mock, monkeypatch):
        from factories import rentcast_record

        self.online(monkeypatch, http_mock, {"OH": [rentcast_record(1)], "IN": []})
        dry = runner.invoke(app, ["probe", "--states", "OH,IN", "--dry-run"], env=WIDE)
        assert "probe sweep of 2 states" in dry.output and "first page only" in dry.output
        result = runner.invoke(app, ["probe", "--states", "OH,IN"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "Run 1: probe sweep, completed" in result.output
        assert "Requests billed now: 2." in result.output

    def test_rentcast_can_be_switched_off(self, engine, monkeypatch, config_dir):
        (config_dir / "settings.local.yaml").write_text("sources: {rentcast: {enabled: false}}\n")
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        result = runner.invoke(app, ["sweep", "--mode", "weekly", "--dry-run"])
        assert result.exit_code == 2
        assert "switched off in settings" in result.output

    def test_listings_on_an_empty_database(self, engine):
        result = runner.invoke(app, ["listings"])
        assert result.exit_code == 0
        assert "Nothing stored yet" in result.output


class TestImportCommand:
    @pytest.fixture
    def csv_file(self, tmp_path):
        from test_csv_import import MLS_FILE

        path = tmp_path / "export.csv"
        path.write_text(MLS_FILE, encoding="utf-8")
        return path

    @pytest.fixture(autouse=True)
    def own_config(self, config_dir, monkeypatch):
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        return config_dir

    def test_import_then_list(self, engine, csv_file, own_config):
        result = runner.invoke(app, ["import-csv", str(csv_file), "--mapping", "my_mls"], env=WIDE)
        assert result.exit_code == 0, result.output
        assert "Imported 3 of 5 rows" in result.output
        assert "Mapping 'my_mls' (guessed now)" in result.output
        assert "address_line1: Address" in result.output
        assert "row 5 skipped: no street address" in result.output
        assert (own_config / "csv_mappings" / "my_mls.yaml").is_file()

        listed = runner.invoke(app, ["listings", "--status", "all"], env=WIDE)
        assert "101 Sample St" in listed.output and "fourplex" in listed.output
        assert "csv:my_mls" in listed.output

        again = runner.invoke(app, ["import-csv", str(csv_file), "--mapping", "my_mls"], env=WIDE)
        assert "Mapping 'my_mls' (saved)" in again.output
        assert "New listings 0; updated 3" in again.output

    def test_dry_run(self, engine, csv_file, own_config):
        result = runner.invoke(
            app, ["import-csv", str(csv_file), "--mapping", "my_mls", "--dry-run"], env=WIDE
        )
        assert result.exit_code == 0, result.output
        assert "Would import 3 of 5 rows" in result.output
        assert not (own_config / "csv_mappings" / "my_mls.yaml").exists()

    def test_missing_file(self, engine, tmp_path):
        result = runner.invoke(app, ["import-csv", str(tmp_path / "nope.csv"), "--mapping", "x"])
        assert result.exit_code == 2
        assert "No such file" in result.output

    def test_csv_import_can_be_switched_off(self, engine, csv_file, own_config):
        (own_config / "settings.local.yaml").write_text("sources: {csv: {enabled: false}}\n")
        result = runner.invoke(app, ["import-csv", str(csv_file), "--mapping", "x"])
        assert result.exit_code == 2
        assert "switched off in settings" in result.output
