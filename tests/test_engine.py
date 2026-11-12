"""Golden test for the underwriting engine.

Tolerances: +/- $1 on dollars, +/- 0.01 percentage points on percentages, +/- 0.001 on ratios,
+/- $50 on prices found by bisection.
"""

from __future__ import annotations

import pytest

from dealfinder.underwrite import (
    DealInputs,
    HoldAssumptions,
    LoanTerms,
    MiTier,
    Shock,
    Targets,
    Unit,
    run_scenario,
)

MANAGER_PCT = 0.09
TARGETS = Targets(min_cash_flow_per_door_month=100, min_coc=0.06, min_dscr=1.20)
HOLD = HoldAssumptions(
    years=5, rent_growth=0.03, expense_growth=0.03, appreciation=0.03, selling_cost_pct=0.06
)
SHOCKS = (
    Shock(name="Rate +1 pt", rate_delta=0.01),
    Shock(name="Rate -1 pt", rate_delta=-0.01),
    Shock(name="Rents -10%", rent_factor=0.90),
    Shock(name="Vacancy +5 pts", vacancy_delta=0.05),
    Shock(name="Insurance +50%", insurance_factor=1.5),
    Shock(name="Taxes +25%", tax_factor=1.25),
)


def dollars(actual: float, expected: float) -> None:
    assert actual == pytest.approx(expected, abs=1.0)


def percent(actual: float | None, expected_percent: float) -> None:
    assert actual is not None
    assert actual * 100 == pytest.approx(expected_percent, abs=0.01)


def ratio(actual: float | None, expected: float) -> None:
    assert actual is not None
    assert actual == pytest.approx(expected, abs=0.001)


def bisection_price(actual: float | None, expected: float) -> None:
    assert actual is not None
    assert actual == pytest.approx(expected, abs=50.0)


def golden_deal(management_pct: float) -> DealInputs:
    """$480,000, four units at $1,350 (actual rents), built 1978 so repairs and reserves are
    8% each, 6% vacancy, $75/unit/month utilities, $900/unit/year insurance, taxes at 1.2% of
    price, $300/unit/year misc, 3% closing costs, no rehab."""
    return DealInputs(
        price=480_000,
        units=tuple(Unit(in_place_rent=1_350) for _ in range(4)),
        vacancy_rate=0.06,
        management_pct=management_pct,
        repairs_pct=0.08,
        reserves_pct=0.08,
        utilities_per_unit_month=75,
        insurance_per_unit_year=900,
        tax_rate=0.012,
        misc_per_unit_year=300,
        closing_cost_pct=0.03,
        rehab=0,
    )


CONV_INVESTOR = LoanTerms(
    name="conv_investor",
    program="conventional",
    down_payment_pct=0.25,
    annual_rate=0.07,
    term_years=30,
)

FHA_HOUSE_HACK = LoanTerms(
    name="fha_house_hack",
    program="fha",
    owner_occupied=True,
    down_payment_pct=0.035,
    annual_rate=0.065,
    term_years=30,
    upfront_mi_pct=0.0175,
    annual_mi=(
        MiTier(max_base_loan=726_200, ltv_above=0.95, rate_above=0.0055, rate_at_or_below=0.0050),
        MiTier(max_base_loan=None, ltv_above=0.95, rate_above=0.0075, rate_at_or_below=0.0070),
    ),
    self_sufficiency_factor=0.75,
)


@pytest.fixture(scope="module")
def scenario_a():
    return run_scenario(
        golden_deal(management_pct=0.09),
        CONV_INVESTOR,
        targets=TARGETS,
        hold=HOLD,
        shocks=SHOCKS,
        manager_pct=MANAGER_PCT,
    )


@pytest.fixture(scope="module")
def scenario_b():
    # The owner lives in one unit and manages the building; the moved-out view hires a manager.
    return run_scenario(
        golden_deal(management_pct=0.0),
        FHA_HOUSE_HACK,
        targets=TARGETS,
        hold=HOLD,
        shocks=SHOCKS,
        manager_pct=MANAGER_PCT,
    )


class TestScenarioAConvInvestor:
    def test_operating_statement(self, scenario_a):
        statement = scenario_a.kpis.statement
        dollars(statement.gsr, 64_800.00)
        dollars(statement.egi, 60_912.00)
        dollars(statement.opex, 30_010.08)
        dollars(statement.noi, 30_901.92)
        percent(scenario_a.kpis.cap_rate, 6.44)

    def test_debt_and_cash_flow(self, scenario_a):
        kpis = scenario_a.kpis
        dollars(scenario_a.loan.monthly_pi, 2_395.09)
        dollars(kpis.annual_debt_service, 28_741.07)
        dollars(kpis.cash_flow_year, 2_160.85)
        dollars(kpis.cash_flow_door_month, 45.02)
        assert kpis.cash_flow_door_month == pytest.approx(45.02, abs=0.01)
        dollars(kpis.cash_invested, 134_400.00)
        percent(kpis.coc, 1.61)

    def test_ratios(self, scenario_a):
        kpis = scenario_a.kpis
        ratio(kpis.dscr, 1.075)
        ratio(kpis.lender_dscr, 1.701)
        ratio(kpis.grm, 7.407)
        percent(kpis.expense_ratio, 49.27)
        percent(kpis.break_even_occupancy, 90.67)
        dollars(kpis.year1_principal_paydown, 3_656.92)

    def test_five_year_hold(self, scenario_a):
        hold = scenario_a.hold
        dollars(hold.sale_price, 556_451.56)
        dollars(hold.net_sale_proceeds, 184_190.74)
        percent(hold.irr, 9.08)
        ratio(hold.equity_multiple, 1.522)

    def test_max_allowable_offer(self, scenario_a):
        bisection_price(scenario_a.mao.price, 413_431)
        assert scenario_a.mao.binding == ("CoC",)


class TestScenarioBFhaHouseHack:
    def test_loan(self, scenario_b):
        loan = scenario_b.loan
        dollars(loan.financed_mi, 8_106.00)
        dollars(loan.amount, 471_306.00)
        dollars(loan.monthly_pi, 2_978.97)
        dollars(loan.monthly_mi, 212.30)
        assert loan.monthly_mi == pytest.approx(212.30, abs=0.01)

    def test_self_sufficiency(self, scenario_b):
        test = scenario_b.self_sufficiency
        assert test is not None
        dollars(test.payment, 3_971.27)
        dollars(test.allowed_payment, 4_050.00)
        assert test.passes
        assert test.margin == pytest.approx(78.73, abs=0.01)
        bisection_price(test.max_passing_price, 490_293)

    def test_owner_and_moved_out_views(self, scenario_b):
        assert scenario_b.house_hack is not None
        dollars(scenario_b.house_hack.owner_net_monthly_cost, 1_428.27)
        assert scenario_b.view == "moved_out"
        assert scenario_b.kpis.cash_flow_door_month == pytest.approx(-154.03, abs=0.01)
