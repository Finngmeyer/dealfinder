from __future__ import annotations

import pytest
from pydantic import ValidationError

from dealfinder.analysis import DISCLAIMER, AnalysisError, AnalyzeRequest, analyze


def request(**changes) -> AnalyzeRequest:
    fields = {"price": 480_000, "rents": [1_350] * 4, "zip_code": "43215"}
    fields.update(changes)
    return AnalyzeRequest(**fields)


def golden_request(**changes) -> AnalyzeRequest:
    fields = {
        "year_built": 1978,
        "tax_rate": 0.012,
        "vacancy_rate": 0.06,
        "insurance_per_unit_year": 900,
        "management": "manager",
    }
    fields.update(changes)
    return request(**fields)


def codes(result) -> set[str]:
    return {flag.code for flag in result.flags}


def line(result, key):
    return next(item for item in result.inputs if item.key == key)


def scenario(result, name):
    return next(item for item in result.scenarios if item.terms.name == name)


class TestGoldenThroughTheAssembly:
    def test_scenario_a(self, config):
        result = analyze(golden_request(scenarios=["conv_investor"], rate_quote=0.07), config)
        kpis = scenario(result, "conv_investor").kpis
        assert kpis.cash_flow_door_month == pytest.approx(45.02, abs=0.01)
        assert kpis.coc == pytest.approx(0.0161, abs=0.0001)
        assert result.statement.noi == pytest.approx(30_901.92, abs=0.01)
        assert result.rates["conv_investor"].source == "your quote, entered"
        assert result.disclaimer == DISCLAIMER

    def test_scenario_b(self, config):
        result = analyze(
            golden_request(management=None, scenarios=["fha_house_hack"], rate_quote=0.065), config
        )
        fha = scenario(result, "fha_house_hack")
        assert fha.house_hack is not None
        assert fha.house_hack.owner_net_monthly_cost == pytest.approx(1_428.27, abs=0.01)
        assert fha.kpis.cash_flow_door_month == pytest.approx(-154.03, abs=0.01)
        assert fha.self_sufficiency is not None and fha.self_sufficiency.passes


class TestUnknownInputsArePessimisticAndFlagged:
    def test_bare_request(self, config):
        result = analyze(request(), config)
        unknown = config.settings.when_unknown
        deal = result.deal
        assert deal.tax_rate == unknown.tax_rate
        assert deal.vacancy_rate == unknown.vacancy_rate
        assert (deal.repairs_pct, deal.reserves_pct) == (0.10, 0.10)
        assert deal.utilities_per_unit_month == 75
        assert deal.insurance_per_unit_year == pytest.approx(900 * 1.5)
        assert deal.flood_insurance_year == 0
        assert {
            "tax_rate_unknown",
            "vacancy_unknown",
            "year_built_unknown",
            "utilities_unknown",
            "insurance_placeholder",
            "flood_not_checked",
            "market_rent_unknown",
            "rate_stand_in",
        } <= codes(result)
        for key in ("tax_rate", "vacancy_rate", "year_built", "utilities", "insurance"):
            assert line(result, key).flagged, key
            assert line(result, key).kind == "estimated", key

    def test_stand_in_rate_plus_spreads(self, config):
        result = analyze(request(), config)
        base = config.settings.when_unknown.base_rate
        assert result.rates["conv_investor"].rate == pytest.approx(base + 0.0075)
        assert result.rates["dscr"].rate == pytest.approx(base + 0.0125)
        assert result.rates["conv_investor"].flagged
        assert result.rates["cash"].source == "no loan"

    def test_flood_policy_can_be_flipped_in_settings(self, config):
        config.settings.when_unknown.in_flood_zone = True
        result = analyze(request(), config)
        assert result.deal.flood_insurance_year == 750 * 4
        assert "flood_unknown_charged" in codes(result)

    def test_hazard_policy_can_be_flipped_in_settings(self, config):
        config.settings.when_unknown.high_hazard_county = False
        result = analyze(request(), config)
        assert result.deal.insurance_per_unit_year == 900
        assert line(result, "insurance").flagged  # still a placeholder


class TestOverrides:
    def test_entered_values_are_used_and_labelled(self, config):
        result = analyze(golden_request(closing_cost_pct=0.02, rehab=10_000), config)
        deal = result.deal
        assert (deal.tax_rate, deal.vacancy_rate, deal.insurance_per_unit_year) == (
            0.012,
            0.06,
            900,
        )
        assert (deal.repairs_pct, deal.reserves_pct) == (0.08, 0.08)
        assert deal.closing_cost_pct == 0.02
        assert deal.rehab == 10_000
        for key in ("tax_rate", "vacancy_rate", "insurance", "closing_cost_pct", "rehab"):
            assert line(result, key).kind == "override", key
            assert not line(result, key).flagged, key
        assert line(result, "price").kind == "actual"
        assert line(result, "rents").kind == "actual"
        assert not {"tax_rate_unknown", "vacancy_unknown", "insurance_placeholder"} & codes(result)

    def test_management_follows_the_setting_unless_entered(self, config):
        assert analyze(request(), config).deal.management_pct == 0.0
        assert "self_managed" in codes(analyze(request(), config))
        assert analyze(request(management="manager"), config).deal.management_pct == 0.09
        config.settings.underwriting.management.mode = "manager"
        assert analyze(request(), config).deal.management_pct == 0.09
        assert analyze(request(management="self"), config).deal.management_pct == 0.0

    def test_utilities(self, config):
        assert (
            analyze(request(tenants_pay_utilities=True), config).deal.utilities_per_unit_month == 0
        )
        owner = analyze(request(tenants_pay_utilities=False), config)
        assert owner.deal.utilities_per_unit_month == 75
        assert "utilities_unknown" not in codes(owner)
        assert (
            analyze(request(utilities_per_unit_month=120), config).deal.utilities_per_unit_month
            == 120
        )

    def test_flood_zone(self, config):
        in_zone = analyze(request(in_flood_zone=True), config)
        assert in_zone.deal.flood_insurance_year == 750 * 4
        assert next(f for f in in_zone.flags if f.code == "flood_zone").level == "red"
        quoted = analyze(request(in_flood_zone=True, flood_insurance_year=1_900), config)
        assert quoted.deal.flood_insurance_year == 1_900
        clear = analyze(request(in_flood_zone=False), config)
        assert clear.deal.flood_insurance_year == 0
        assert not {"flood_zone", "flood_not_checked"} & codes(clear)

    def test_last_tax_bill_is_a_floor(self, config):
        result = analyze(golden_request(tax_bill=7_500), config)
        assert result.statement.taxes == pytest.approx(7_500)
        assert line(result, "tax_bill").kind == "actual"

    def test_market_rents(self, config):
        upside = analyze(request(market_rents=[1_500] * 4), config)
        assert upside.statement.gsr == pytest.approx(64_800)
        assert scenario(upside, "conv_investor").kpis.rent_upside == pytest.approx(
            1_500 / 1_350 - 1
        )
        assert "market_rent_unknown" not in codes(upside)

        below = analyze(request(market_rents=[1_200, 1_350, 1_350, 1_350]), config)
        assert below.statement.gsr == pytest.approx(12 * (1_200 + 3 * 1_350))
        assert "rent_above_market" in codes(below)

    def test_owner_unit_is_one_based(self, config):
        result = analyze(request(rents=[1_000, 1_200, 1_100, 900], owner_unit=3), config)
        assert scenario(result, "fha_house_hack").house_hack.owner_unit == 2
        default = analyze(request(rents=[1_000, 1_200, 1_100, 900]), config)
        assert scenario(default, "fha_house_hack").house_hack.owner_unit == 1

    def test_own_rent_sets_the_comparison(self, config):
        result = analyze(request(comparison_rent=1_800), config)
        assert scenario(result, "fha_house_hack").house_hack.comparison_rent == 1_800


class TestRatePrecedence:
    def test_entered_quote_beats_everything(self, config):
        config.settings.financing.presets["conv_investor"].rate_quote = 0.066
        config.settings.financing.base_rate_manual = 0.07
        result = analyze(request(rate_quote=0.0625, base_rate=0.08), config)
        assert result.rates["conv_investor"].rate == 0.0625
        assert result.rates["dscr"].rate == 0.0625

    def test_preset_quote_beats_base_plus_spread(self, config):
        config.settings.financing.presets["conv_investor"].rate_quote = 0.066
        result = analyze(request(base_rate=0.07), config)
        assert result.rates["conv_investor"].rate == 0.066
        assert result.rates["conv_investor"].source == "your quote, from settings"
        assert result.rates["dscr"].rate == pytest.approx(0.07 + 0.0125)

    def test_entered_base_beats_the_manual_base_in_settings(self, config):
        config.settings.financing.base_rate_manual = 0.07
        assert analyze(request(), config).rates["fha_house_hack"].rate == pytest.approx(0.07)
        assert analyze(request(base_rate=0.0711), config).rates["dscr"].rate == pytest.approx(
            0.0711 + 0.0125
        )
        assert "rate_stand_in" not in codes(analyze(request(), config))


class TestPresetSelection:
    def test_four_units_run_every_residential_loan(self, config):
        result = analyze(request(), config)
        assert [s.terms.name for s in result.scenarios] == [
            "fha_house_hack",
            "conv_owner_5",
            "conv_investor",
            "dscr",
            "cash",
        ]
        assert result.skipped == {"commercial": "not offered for 4 units"}

    def test_five_units_switch_to_the_commercial_loan(self, config):
        result = analyze(request(rents=[1_000] * 5), config)
        assert [s.terms.name for s in result.scenarios] == ["commercial", "cash"]
        assert result.skipped["fha_house_hack"] == "not offered for 5 units"
        commercial = scenario(result, "commercial")
        assert commercial.terms.term_years == 25
        assert commercial.loan.term_months == 300

    def test_single_family_skips_the_owner_occupied_multi_unit_loans(self, config):
        result = analyze(request(rents=[2_000]), config)
        assert [s.terms.name for s in result.scenarios] == ["conv_investor", "dscr", "cash"]
        assert result.skipped["fha_house_hack"] == "not offered for 1 unit"

    def test_duplex_has_no_self_sufficiency_test(self, config):
        result = analyze(request(rents=[1_350, 1_350]), config)
        assert scenario(result, "fha_house_hack").self_sufficiency is None

    def test_a_switched_off_preset_is_hidden_unless_asked_for(self, config):
        config.settings.financing.presets["dscr"].enabled = False
        hidden = analyze(request(), config)
        assert "dscr" not in [s.terms.name for s in hidden.scenarios]
        assert hidden.skipped["dscr"] == "switched off in settings"
        asked = analyze(request(scenarios=["dscr"]), config)
        assert [s.terms.name for s in asked.scenarios] == ["dscr"]

    def test_unknown_scenario(self, config):
        with pytest.raises(AnalysisError, match="hard_money"):
            analyze(request(scenarios=["hard_money"]), config)

    def test_targets_switched_off_in_settings_change_the_offer(self, config):
        with_all = scenario(analyze(golden_request(rate_quote=0.07), config), "conv_investor")
        config.settings.buy_box.targets = config.settings.buy_box.targets.model_copy(
            update={"min_coc": None}
        )
        without_coc = scenario(analyze(golden_request(rate_quote=0.07), config), "conv_investor")
        assert with_all.mao.binding == ("CoC",)
        assert without_coc.mao.price > with_all.mao.price
        assert without_coc.mao.binding == ("DSCR",)


class TestLoanLimits:
    def test_fha_loan_is_capped_at_the_county_limit(self, config):
        # 43215 is Franklin County, OH: four-unit FHA limit $1,136,750.
        result = analyze(request(price=1_200_000, rents=[3_000] * 4), config)
        fha = scenario(result, "fha_house_hack")
        assert fha.loan.capped_by_limit
        assert fha.loan.base_loan == 1_136_750
        assert fha.loan.down_payment == pytest.approx(1_200_000 - 1_136_750)
        assert any("limit" in note for note in fha.notes)
        assert "loan_limit_national" not in codes(result)
        assert not scenario(result, "dscr").loan.capped_by_limit

    def test_high_cost_county_has_room(self, config):
        # 90012 is Los Angeles County: four-unit FHA limit $2,402,625.
        result = analyze(request(price=1_200_000, rents=[3_000] * 4, zip_code="90012"), config)
        assert not scenario(result, "fha_house_hack").loan.capped_by_limit

    def test_unknown_county_uses_the_national_floor_and_flags_it(self, config):
        # Hartford's ZIP maps to a pre-2022 county code that the limit table no longer uses.
        result = analyze(request(price=1_200_000, rents=[3_000] * 4, zip_code="06103"), config)
        fha = scenario(result, "fha_house_hack")
        assert fha.loan.base_loan == 1_041_125
        assert "loan_limit_national" in codes(result)

    def test_no_flag_when_the_national_floor_does_not_bind(self, config):
        result = analyze(request(zip_code="06103"), config)
        assert "loan_limit_national" not in codes(result)

    def test_owner_loan_stays_at_the_baseline_but_investor_loan_uses_the_county(self, config):
        result = analyze(request(price=2_400_000, rents=[5_000] * 4, zip_code="90012"), config)
        assert scenario(result, "conv_owner_5").loan.base_loan == 1_601_750  # no high-balance
        investor = scenario(result, "conv_investor").loan
        assert investor.base_loan == pytest.approx(1_800_000)  # 75%, under the $2,402,625 limit
        assert not investor.capped_by_limit


class TestLocation:
    def test_place_is_resolved(self, config):
        result = analyze(request(), config)
        assert result.place is not None and result.place.county_fips == "39049"
        assert "placeholder for OH" in line(result, "insurance").source

    def test_outside_coverage_is_flagged(self, config):
        assert "outside_coverage" in codes(analyze(request(zip_code="99501"), config))

    @pytest.mark.parametrize(
        ("zip_code", "code"),
        [("abcde", "zip_invalid"), ("00000", "zip_unknown"), ("43216", "zip_approximate")],
    )
    def test_bad_or_unmapped_zip_still_analyzes(self, config, zip_code, code):
        result = analyze(request(zip_code=zip_code), config)
        assert code in codes(result)
        assert result.scenarios


class TestRequestValidation:
    def test_market_rents_must_match_the_unit_count(self):
        with pytest.raises(ValidationError, match="market rents"):
            request(market_rents=[1_350] * 3)

    def test_owner_unit_must_exist(self):
        with pytest.raises(ValidationError, match="owner unit"):
            request(owner_unit=5)

    @pytest.mark.parametrize("field", ["tax_rate", "base_rate", "rate_quote", "closing_cost_pct"])
    def test_rates_are_fractions_not_percents(self, field):
        with pytest.raises(ValidationError):
            request(**{field: 7.0})

    def test_price_and_rents_are_required_and_positive(self):
        with pytest.raises(ValidationError):
            request(price=0)
        with pytest.raises(ValidationError):
            request(rents=[])
        with pytest.raises(ValidationError):
            request(rents=[1_000, -5])
