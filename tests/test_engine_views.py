"""Operating statement, KPIs, the house-hack view and the scenario runner beyond the golden case."""

from __future__ import annotations

import pytest

from conftest import CASH, HOLD, TARGETS, fha_terms, investor_terms, make_deal
from dealfinder.underwrite import (
    ENGINE_VERSION,
    Unit,
    compute_kpis,
    default_owner_unit,
    house_hack,
    operating_statement,
    run_scenario,
    size_loan,
)


class TestUnitRents:
    def test_current_rent_is_the_lower_of_in_place_and_market(self):
        assert Unit(in_place_rent=1_000, market_rent=1_200).current_rent == 1_000
        assert Unit(in_place_rent=1_400, market_rent=1_200).current_rent == 1_200

    def test_a_vacant_unit_uses_the_market_estimate(self):
        vacant = Unit(market_rent=1_200)
        assert vacant.current_rent == 1_200
        assert vacant.pro_forma_rent == 1_200

    def test_without_an_estimate_the_pro_forma_is_the_in_place_rent(self):
        assert Unit(in_place_rent=1_000).pro_forma_rent == 1_000

    def test_a_unit_needs_some_rent(self):
        with pytest.raises(ValueError):
            Unit()


class TestOperatingStatement:
    def test_line_items(self):
        statement = operating_statement(make_deal())
        assert statement.taxes == pytest.approx(5_760)
        assert statement.insurance == pytest.approx(3_600)
        assert statement.repairs == pytest.approx(5_184)
        assert statement.reserves == pytest.approx(5_184)
        assert statement.management == pytest.approx(5_482.08)
        assert statement.utilities == pytest.approx(3_600)
        assert statement.misc == pytest.approx(1_200)
        assert statement.vacancy_loss == pytest.approx(3_888)
        assert statement.percent_based_expenses == pytest.approx(15_850.08)
        assert statement.dollar_based_expenses == pytest.approx(14_160)

    def test_flood_and_hoa_are_expenses(self):
        base = operating_statement(make_deal())
        loaded = operating_statement(make_deal(flood_insurance_year=2_400, hoa_month=100))
        assert loaded.opex - base.opex == pytest.approx(3_600)

    def test_pro_forma_uses_market_rents(self):
        units = tuple(Unit(in_place_rent=1_000, market_rent=1_350) for _ in range(4))
        deal = make_deal(units=units)
        assert operating_statement(deal).gsr == pytest.approx(48_000)
        assert operating_statement(deal, pro_forma=True).gsr == pytest.approx(64_800)

    def test_owner_unit_must_exist(self):
        with pytest.raises(IndexError):
            operating_statement(make_deal(), owner_unit=4)


class TestKpis:
    def test_rent_upside_and_per_sqft(self):
        units = tuple(Unit(in_place_rent=1_000, market_rent=1_200) for _ in range(4))
        deal = make_deal(units=units, building_sqft=4_000)
        kpis = compute_kpis(deal, size_loan(investor_terms(), deal.price))
        assert kpis.rent_upside == pytest.approx(0.20)
        assert kpis.price_per_sqft == pytest.approx(120)
        assert kpis.price_per_door == pytest.approx(120_000)
        assert kpis.rent_to_price == pytest.approx(4_000 / 480_000)

    def test_total_return_adds_principal_paydown_and_no_appreciation(self):
        deal = make_deal()
        kpis = compute_kpis(deal, size_loan(investor_terms(), deal.price))
        assert kpis.year1_total_return == pytest.approx((2_160.85 + 3_656.92) / 134_400, abs=1e-5)

    def test_cash_purchase_has_no_debt_ratios(self):
        deal = make_deal()
        kpis = compute_kpis(deal, size_loan(CASH, deal.price))
        assert kpis.dscr is None
        assert kpis.lender_dscr is None
        assert kpis.cash_flow_year == pytest.approx(kpis.statement.noi)
        assert kpis.cash_invested == pytest.approx(494_400)

    def test_rehab_points_and_credits_change_cash_invested(self):
        deal = make_deal(rehab=20_000, credits=5_000)
        kpis = compute_kpis(deal, size_loan(investor_terms(points_pct=0.01), deal.price))
        assert kpis.cash_invested == pytest.approx(134_400 + 20_000 + 3_600 - 5_000)


class TestHouseHack:
    def test_owner_takes_the_highest_rent_unit_by_default(self):
        units = (
            Unit(in_place_rent=1_000),
            Unit(in_place_rent=1_500),
            Unit(in_place_rent=1_200),
            Unit(in_place_rent=1_500),
        )
        assert default_owner_unit(make_deal(units=units)) == 1

    def test_owner_rent_leaves_income_but_stays_in_the_repairs_base(self):
        units = (Unit(in_place_rent=2_000), *(Unit(in_place_rent=1_000) for _ in range(3)))
        deal = make_deal(units=units)
        hack = house_hack(deal, size_loan(fha_terms(), deal.price))
        assert hack.owner_unit == 0
        assert hack.statement.gsr == pytest.approx(36_000)
        assert hack.statement.building_gsr == pytest.approx(60_000)
        assert hack.statement.repairs == pytest.approx(0.08 * 60_000)
        assert hack.statement.management == 0
        assert hack.comparison_rent == 2_000

    def test_chosen_unit_and_own_rent(self):
        deal = make_deal()
        loan = size_loan(fha_terms(), deal.price)
        hack = house_hack(deal, loan, owner_unit=2, comparison_rent=1_600)
        assert hack.owner_unit == 2
        assert hack.savings_vs_renting == pytest.approx(1_600 - hack.owner_net_monthly_cost)
        assert hack.owner_net_monthly_cost == pytest.approx(1_428.27, abs=0.01)


class TestRunScenario:
    def run(self, deal, terms, **extra):
        return run_scenario(
            deal, terms, targets=TARGETS, hold=HOLD, shocks=(), manager_pct=0.09, **extra
        )

    def test_investor_loans_keep_the_deal_s_own_management(self):
        result = self.run(make_deal(management_pct=0.0), investor_terms())
        assert result.view == "rented"
        assert result.house_hack is None
        assert result.kpis.statement.management == 0
        assert result.engine_version == ENGINE_VERSION

    def test_owner_occupied_loans_charge_a_manager_once_moved_out(self):
        result = self.run(make_deal(management_pct=0.0), fha_terms())
        assert result.view == "moved_out"
        assert result.kpis.statement.management == pytest.approx(5_482.08)
        assert result.house_hack is not None
        assert result.house_hack.statement.management == 0

    def test_reserves_are_months_of_pitia(self):
        result = self.run(make_deal(), investor_terms(reserves_months=6))
        pitia = 2_395.09 + 480 + 300
        assert result.reserves_required == pytest.approx(6 * pitia, abs=0.05)

    def test_notes_cover_caps_failed_tests_and_dscr_tiers(self):
        capped = self.run(make_deal(price=1_500_000), fha_terms(max_loan=1_041_125))
        assert any("limit" in note for note in capped.notes)
        assert any("Fails FHA self-sufficiency" in note for note in capped.notes)

        dscr = investor_terms(
            name="dscr", program="dscr", min_lender_dscr=1.0, best_lender_dscr=1.25
        )
        weak = self.run(make_deal(price=900_000), dscr)
        assert any("does not qualify" in note for note in weak.notes)
        middling = self.run(make_deal(price=700_000), dscr)
        assert any("best pricing" in note for note in middling.notes)
        strong = self.run(make_deal(), dscr)
        assert strong.notes == ()
