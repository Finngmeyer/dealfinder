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
