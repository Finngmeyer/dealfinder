from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest
import respx

from dealfinder.http import Fetcher
from dealfinder.settings import Config, load_config
from dealfinder.underwrite import DealInputs, HoldAssumptions, LoanTerms, MiTier, Targets, Unit

PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "config"

FHA_MI = (
    MiTier(max_base_loan=726_200, ltv_above=0.95, rate_above=0.0055, rate_at_or_below=0.0050),
    MiTier(max_base_loan=None, ltv_above=0.95, rate_above=0.0075, rate_at_or_below=0.0070),
)
PMI = (MiTier(max_base_loan=None, ltv_above=0.80, rate_above=0.0060, rate_at_or_below=0.0),)

TARGETS = Targets(min_cash_flow_per_door_month=100, min_coc=0.06, min_dscr=1.20)
HOLD = HoldAssumptions(
    years=5, rent_growth=0.03, expense_growth=0.03, appreciation=0.03, selling_cost_pct=0.06
)


def make_deal(**changes) -> DealInputs:
    """The golden-test fourplex, with any fields replaced."""
    fields = {
        "price": 480_000,
        "units": tuple(Unit(in_place_rent=1_350) for _ in range(4)),
        "vacancy_rate": 0.06,
        "management_pct": 0.09,
        "repairs_pct": 0.08,
        "reserves_pct": 0.08,
        "utilities_per_unit_month": 75,
        "insurance_per_unit_year": 900,
        "tax_rate": 0.012,
        "misc_per_unit_year": 300,
        "closing_cost_pct": 0.03,
    }
    fields.update(changes)
    return DealInputs(**fields)


def investor_terms(**changes) -> LoanTerms:
    fields = {
        "name": "conv_investor",
        "program": "conventional",
        "down_payment_pct": 0.25,
        "annual_rate": 0.07,
        "term_years": 30,
    }
    fields.update(changes)
    return LoanTerms(**fields)


def fha_terms(**changes) -> LoanTerms:
    fields = {
        "name": "fha_house_hack",
        "program": "fha",
        "owner_occupied": True,
        "down_payment_pct": 0.035,
        "annual_rate": 0.065,
        "term_years": 30,
        "upfront_mi_pct": 0.0175,
        "annual_mi": FHA_MI,
        "self_sufficiency_factor": 0.75,
    }
    fields.update(changes)
    return LoanTerms(**fields)


CASH = LoanTerms(name="cash", program="cash", down_payment_pct=1.0)


@pytest.fixture
def config() -> Config:
    """The project's own configuration, safe to mutate within a test."""
    return load_config(PROJECT_CONFIG)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A private copy of the config directory, for tests that edit the files."""
    target = tmp_path / "config"
    shutil.copytree(PROJECT_CONFIG, target)
    return target


@pytest.fixture(autouse=True)
def http_mock():
    """No test may reach the network: a request without a mocked route fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def fetcher(tmp_path: Path) -> Fetcher:
    """A fetcher with its own empty cache that never really sleeps."""
    return Fetcher(tmp_path / "cache", sleep=lambda seconds: None)


def text_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep a developer's real keys and .env out of the tests, and keep the command line
    offline unless a test opts in."""
    monkeypatch.setenv("DEALFINDER_OFFLINE", "1")
    monkeypatch.delenv("DEALFINDER_DATA_DIR", raising=False)
    for name in (
        "RENTCAST_API_KEY",
        "HUD_API_TOKEN",
        "CENSUS_API_KEY",
        "FRED_API_KEY",
        "ANTHROPIC_API_KEY",
        "APIFY_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "SLACK_WEBHOOK_URL",
        "SMTP_PASSWORD",
        "DEALFINDER_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
