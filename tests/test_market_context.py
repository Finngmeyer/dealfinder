"""The per-ZIP market context, and how market data flows into a deal."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from dealfinder.analysis import AnalyzeRequest, analyze
from dealfinder.market import acs, fred, hud, nri, zori
from dealfinder.market.acs import CountyHousing
from dealfinder.market.context import MarketContext, SourceStatus, load_market
from dealfinder.market.fred import MortgageRate
from dealfinder.market.hud import ATTRIBUTION, FairMarketRents
from dealfinder.market.nri import CountyRisk
from dealfinder.market.zori import RentIndex
from dealfinder.settings import Environment
from test_market_sources import (
    ACS_DETAIL,
    ACS_PROFILE,
    FRED_CSV,
    HUD_SMALL_AREA,
    ZORI_COUNTY,
    ZORI_ZIP,
    nri_page,
)


def mock_everything(http_mock) -> dict[str, object]:
    return {
        "fred": http_mock.get(fred.CSV_URL).mock(return_value=httpx.Response(200, text=FRED_CSV)),
        "hud": http_mock.get(f"{hud.BASE_URL}/fmr/data/3904999999").mock(
            return_value=httpx.Response(200, json=HUD_SMALL_AREA)
        ),
        "acs_detail": http_mock.get(f"{acs.BASE_URL}/2024/acs/acs5").mock(
            return_value=httpx.Response(200, json=ACS_DETAIL)
        ),
        "acs_profile": http_mock.get(f"{acs.BASE_URL}/2024/acs/acs5/profile").mock(
            return_value=httpx.Response(200, json=ACS_PROFILE)
        ),
        "zori": http_mock.get(zori.ZIP_URL).mock(return_value=httpx.Response(200, text=ZORI_ZIP)),
        "zori_county": http_mock.get(zori.COUNTY_URL).mock(
            return_value=httpx.Response(200, text=ZORI_COUNTY)
        ),
        "nri": http_mock.get(nri.SERVICE_URL).mock(
            return_value=httpx.Response(
                200,
                json=nri_page(
                    [{"STCOFIPS": "39049", "RISK_SCORE": 98.1, "RISK_RATNG": "Relatively High"}]
                ),
            )
        ),
    }


def states(context: MarketContext) -> dict[str, str]:
    return {status.key: status.state for status in context.sources}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("HUD_API_TOKEN", "hud-token")
    monkeypatch.setenv("CENSUS_API_KEY", "census-key")
    return Environment()


class TestLoadMarket:
    def test_everything_connected(self, config, fetcher, http_mock, keys):
        mock_everything(http_mock)
        context = load_market("43215", config, keys, fetcher)
        assert set(states(context).values()) == {"ok"}
        assert context.place.county_fips == "39049"
        assert context.mortgage_rate.rate == pytest.approx(0.0688)
        assert context.fair_market_rents.zip_level
        assert context.housing.rental_vacancy == pytest.approx(0.054)
        assert context.rent_index.level == 1560.0
        assert context.risk.rating == "Relatively High"
        assert context.attributions == [ATTRIBUTION]
        assert context.missing() == []

    def test_without_keys_the_keyless_sources_still_work(self, config, fetcher, http_mock):
        routes = mock_everything(http_mock)
        context = load_market("43215", config, Environment(), fetcher)
        assert states(context) == {
            "fred": "ok",
            "hud_fmr": "off",
            "acs": "off",
            "zori": "ok",
            "nri": "ok",
        }
        details = {status.key: status.detail for status in context.sources}
        assert details["hud_fmr"] == "HUD_API_TOKEN is not set"
        assert details["acs"] == "CENSUS_API_KEY is not set"
        assert routes["hud"].call_count == 0 and routes["acs_detail"].call_count == 0
        assert context.attributions == []  # no HUD data shown, so no HUD notice
        assert [status.key for status in context.missing()] == ["hud_fmr", "acs"]

    def test_a_source_can_be_switched_off_in_settings(self, config, fetcher, http_mock, keys):
        routes = mock_everything(http_mock)
        config.settings.market.sources.zori = False
        config.settings.market.sources.fred = False
        context = load_market("43215", config, keys, fetcher)
        assert states(context)["zori"] == "off"
        assert context.rent_index is None and context.mortgage_rate is None
        assert routes["zori"].call_count == 0 and routes["fred"].call_count == 0
        assert context.fair_market_rents is not None

    def test_one_failing_source_does_not_stop_the_others(self, config, fetcher, http_mock, keys):
        mock_everything(http_mock)
        http_mock.get(nri.SERVICE_URL).mock(return_value=httpx.Response(404))
        http_mock.get(f"{hud.BASE_URL}/fmr/data/3904999999").mock(return_value=httpx.Response(401))
        context = load_market("43215", config, keys, fetcher)
        assert states(context) == {
            "fred": "ok",
            "hud_fmr": "unavailable",
            "acs": "ok",
            "zori": "ok",
            "nri": "unavailable",
        }
        assert context.risk is None and context.fair_market_rents is None

    def test_cached_copies_are_reused_and_marked_stale_when_refresh_fails(
        self, config, tmp_path, http_mock
    ):
        from datetime import UTC, datetime, timedelta

        from dealfinder.http import Fetcher

        moment = [datetime(2026, 11, 12, tzinfo=UTC)]
        fetcher = Fetcher(tmp_path / "cache", sleep=lambda s: None, now=lambda: moment[0])
        mock_everything(http_mock)
        load_market("43215", config, Environment(), fetcher)
        http_mock.get(fred.CSV_URL).mock(return_value=httpx.Response(404))
        moment[0] += timedelta(days=3)  # past the one-day life of the rate, within the others
        context = load_market("43215", config, Environment(), fetcher)
        assert states(context)["fred"] == "stale"
        assert context.mortgage_rate.stale
        assert states(context)["zori"] == "ok"

    def test_unknown_zip(self, config, fetcher, http_mock):
        mock_everything(http_mock)
        context = load_market("00000", config, Environment(), fetcher)
        assert context.place.state is None
        assert states(context)["nri"] == "unavailable"
        assert states(context)["fred"] == "ok"  # the rate does not depend on the place


def market(**parts) -> MarketContext:
    """A hand-built context, so deal assembly can be tested without any HTTP."""
    defaults = {
        "zip_code": "43215",
        "place": None,
        "sources": [SourceStatus(key="fred", label="Mortgage rate (FRED)", state="ok", detail="")],
    }
    defaults.update(parts)
    return MarketContext(**defaults)


def housing(**changes) -> CountyHousing:
    fields = {
        "year": 2024,
        "county_fips": "39049",
        "name": "Franklin County, Ohio",
        "median_taxes": 3_600,
        "median_value": 250_000,
        "aggregate_taxes": 900_000_000,
        "aggregate_value": 60_000_000_000,
        "rental_vacancy": 0.054,
    }
    fields.update(changes)
    return CountyHousing(**fields)


RENTS = FairMarketRents(
    fiscal_year=2027,
    area_name="Sampleville, OH HUD Metro FMR Area",
    zip_level=True,
    rents={0: 1100, 1: 1250, 2: 1500, 3: 1900, 4: 2200},
)


def request(**changes) -> AnalyzeRequest:
    fields = {"price": 480_000, "rents": [1_350] * 4, "zip_code": "43215"}
    fields.update(changes)
    return AnalyzeRequest(**fields)


def line(result, key):
    return next(item for item in result.inputs if item.key == key)


def codes(result) -> set[str]:
    return {flag.code for flag in result.flags}


class TestMarketDataInADeal:
    def test_vacancy_is_county_vacancy_plus_a_point_with_a_floor(self, config):
        result = analyze(request(), config, market(housing=housing(rental_vacancy=0.054)))
        assert result.deal.vacancy_rate == pytest.approx(0.064)
        assert "ACS 2020-2024" in line(result, "vacancy_rate").source
        assert not line(result, "vacancy_rate").flagged
        assert "vacancy_unknown" not in codes(result)

        low = analyze(request(), config, market(housing=housing(rental_vacancy=0.02)))
        assert low.deal.vacancy_rate == 0.06  # the floor

        missing = analyze(request(), config, market(housing=housing(rental_vacancy=None)))
        assert missing.deal.vacancy_rate == config.settings.when_unknown.vacancy_rate
        assert "vacancy_unknown" in codes(missing)

    def test_tax_rate_from_the_county(self, config):
        result = analyze(request(), config, market(housing=housing()))
        assert result.deal.tax_rate == pytest.approx(0.0144)
        assert line(result, "tax_rate").kind == "estimated"
        assert "median taxes / median value" in line(result, "tax_rate").source
        assert result.statement.taxes == pytest.approx(480_000 * 0.0144)

    def test_top_coded_county_uses_aggregates_and_says_so(self, config):
        capped = housing(median_taxes=10_001, aggregate_taxes=1.5e9, aggregate_value=5e10)
        result = analyze(request(), config, market(housing=capped))
        assert result.deal.tax_rate == pytest.approx(0.03)
        assert "tax_rate_top_coded" in codes(result)

    def test_last_tax_bill_still_wins_when_higher(self, config):
        result = analyze(request(tax_bill=9_000), config, market(housing=housing()))
        assert result.statement.taxes == 9_000

    def test_an_entered_figure_beats_market_data(self, config):
        result = analyze(
            request(tax_rate=0.01, vacancy_rate=0.05), config, market(housing=housing())
        )
        assert (result.deal.tax_rate, result.deal.vacancy_rate) == (0.01, 0.05)
        assert line(result, "tax_rate").kind == "override"

    @pytest.mark.parametrize(
        ("rating", "expected", "flagged_high"),
        [
            ("Very High", 1_350, True),
            ("Relatively High", 1_350, True),
            ("Relatively Moderate", 900, False),
            ("Very Low", 900, False),
        ],
    )
    def test_insurance_multiplier_follows_the_hazard_rating(
        self, config, rating, expected, flagged_high
    ):
        risk = CountyRisk(county_fips="39049", score=50.0, rating=rating)
        result = analyze(request(), config, market(risk=risk))
        assert result.deal.insurance_per_unit_year == expected
        assert ("high_hazard" in codes(result)) is flagged_high
        assert rating in line(result, "insurance").source
        assert line(result, "insurance").flagged  # still a placeholder, always flagged

    def test_which_ratings_count_as_high_is_a_setting(self, config):
        config.settings.market.high_risk_ratings = ["Very High"]
        risk = CountyRisk(county_fips="39049", score=90.0, rating="Relatively High")
        assert analyze(request(), config, market(risk=risk)).deal.insurance_per_unit_year == 900

    def test_live_rate_plus_spreads(self, config):
        rate = MortgageRate(rate=0.0711, as_of=date(2026, 11, 12), source="FRED MORTGAGE30US")
        result = analyze(request(), config, market(mortgage_rate=rate))
        assert result.rates["conv_investor"].rate == pytest.approx(0.0786)
        assert result.rates["conv_investor"].source == (
            "FRED MORTGAGE30US, week of Nov 12, 2026 7.11% + 0.75 pt spread"
        )
        assert not result.rates["conv_investor"].flagged
        assert "rate_stand_in" not in codes(result)

    def test_rate_precedence_entered_then_settings_then_live(self, config):
        rate = MortgageRate(rate=0.0711, as_of=date(2026, 11, 12), source="FRED MORTGAGE30US")
        context = market(mortgage_rate=rate)
        assert analyze(request(base_rate=0.06), config, context).rates[
            "dscr"
        ].rate == pytest.approx(0.0725)
        config.settings.financing.base_rate_manual = 0.065
        assert analyze(request(), config, context).rates["dscr"].rate == pytest.approx(0.0775)


class TestMarketRents:
    def test_fair_market_rents_by_bedroom_net_of_utilities(self, config):
        result = analyze(request(beds=[2, 2, 1, 3]), config, market(fair_market_rents=RENTS))
        # Who pays utilities is unknown, so the allowance comes off: the cautious reading.
        expected = [1500 - 160, 1500 - 160, 1250 - 125, 1900 - 195]
        assert [unit.market_rent for unit in result.deal.units] == expected
        source = line(result, "market_rents").source
        assert "HUD FY2027 Small Area FMR for the ZIP" in source
        assert "less a placeholder utility allowance" in source
        assert line(result, "market_rents").kind == "estimated"
        assert result.attributions == [ATTRIBUTION]
        assert "market_rent_unknown" not in codes(result)

    def test_cash_flow_uses_the_lower_of_in_place_and_market(self, config):
        result = analyze(
            request(rents=[1_350, 1_350, 1_350, 2_000], beds=[2, 2, 1, 3]),
            config,
            market(fair_market_rents=RENTS),
        )
        assert [unit.current_rent for unit in result.deal.units] == [1_340, 1_340, 1_125, 1_705]
        assert "rent_above_market" in codes(result)

    def test_owner_paid_utilities_keep_the_gross_rent(self, config):
        result = analyze(
            request(beds=[2] * 4, tenants_pay_utilities=False),
            config,
            market(fair_market_rents=RENTS),
        )
        assert result.deal.units[0].market_rent == 1500
        assert "utility allowance" not in line(result, "market_rents").source

    def test_tenant_paid_utilities_take_the_allowance_off(self, config):
        result = analyze(
            request(beds=[2] * 4, tenants_pay_utilities=True),
            config,
            market(fair_market_rents=RENTS),
        )
        assert result.deal.units[0].market_rent == 1340
        assert result.deal.utilities_per_unit_month == 0

    def test_five_bedroom_units_use_hud_s_rule(self, config):
        result = analyze(
            request(rents=[3_000], beds=[5], tenants_pay_utilities=False),
            config,
            market(fair_market_rents=RENTS),
        )
        assert result.deal.units[0].market_rent == pytest.approx(2200 * 1.15)

    def test_entered_market_rents_win(self, config):
        result = analyze(
            request(beds=[2] * 4, market_rents=[1_600] * 4), config, market(fair_market_rents=RENTS)
        )
        assert result.deal.units[0].market_rent == 1_600

    def test_without_bedroom_counts_there_is_nothing_to_match(self, config):
        result = analyze(request(), config, market(fair_market_rents=RENTS))
        assert result.deal.units[0].market_rent is None
        message = next(f.message for f in result.flags if f.code == "market_rent_unknown")
        assert "no bedroom counts" in message

    def test_fha_self_sufficiency_uses_the_market_rent(self, config):
        result = analyze(
            request(beds=[2] * 4, scenarios=["fha_house_hack"]),
            config,
            market(fair_market_rents=RENTS),
        )
        test = result.scenarios[0].self_sufficiency
        assert test.market_rent_month == 4 * 1340

    def test_bedroom_counts_must_match_the_units(self):
        with pytest.raises(ValueError, match="bedroom counts"):
            request(beds=[2, 2])


class TestMarketFlags:
    def test_rents_far_above_the_local_index_are_flagged(self, config):
        index = RentIndex(
            geography="zip", region="43215", level=1_000, as_of=date(2026, 9, 30), yoy_growth=0.02
        )
        assert "rent_above_index" in codes(analyze(request(), config, market(rent_index=index)))
        close = index.model_copy(update={"level": 1_200})
        assert "rent_above_index" not in codes(analyze(request(), config, market(rent_index=close)))

    def test_missing_sources_are_named_once(self, config):
        context = market(
            sources=[
                SourceStatus(
                    key="hud_fmr",
                    label="Fair Market Rents (HUD)",
                    state="off",
                    detail="HUD_API_TOKEN is not set",
                ),
                SourceStatus(
                    key="nri", label="Hazard rating (FEMA)", state="unavailable", detail="HTTP 404"
                ),
                SourceStatus(key="fred", label="Mortgage rate (FRED)", state="ok", detail=""),
            ]
        )
        result = analyze(request(), config, context)
        messages = [flag.message for flag in result.flags if flag.code == "market_data_missing"]
        assert messages == [
            "Not connected: Fair Market Rents (HUD) (HUD_API_TOKEN is not set); "
            "Hazard rating (FEMA) (HTTP 404)."
        ]

    def test_stale_data_is_flagged(self, config):
        context = market(
            housing=housing(stale=True),
            sources=[
                SourceStatus(
                    key="acs", label="Taxes and vacancy (Census ACS)", state="stale", detail=""
                )
            ],
        )
        result = analyze(request(), config, context)
        assert "market_data_stale" in codes(result)
        assert line(result, "tax_rate").flagged and line(result, "vacancy_rate").flagged

    def test_no_market_context_behaves_as_before(self, config):
        result = analyze(request(), config)
        assert result.market is None and result.attributions == []
        assert {"tax_rate_unknown", "vacancy_unknown", "rate_stand_in"} <= codes(result)
        assert "market_data_missing" not in codes(result)
