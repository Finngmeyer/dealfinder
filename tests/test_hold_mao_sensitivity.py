from __future__ import annotations

import pytest

from conftest import CASH, HOLD, TARGETS, investor_terms, make_deal
from dealfinder.underwrite import (
    HoldAssumptions,
    Shock,
    Targets,
    Unit,
    compute_kpis,
    failing_targets,
    hold_projection,
    irr,
    max_allowable_offer,
    sensitivity,
    size_loan,
)


class TestIrr:
    def test_simple_cases(self):
        assert irr([-100, 110]) == pytest.approx(0.10, abs=1e-9)
        assert irr([-100, 0, 121]) == pytest.approx(0.10, abs=1e-9)
        assert irr([-100, 50]) == pytest.approx(-0.50, abs=1e-9)

    @pytest.mark.parametrize("flows", [[100, 10], [-100, -5], [-100], [0, 100]])
    def test_undefined_returns_none(self, flows):
        assert irr(flows) is None


class TestHold:
    def test_everything_grows_three_percent_when_both_rates_are_three(self):
        deal = make_deal()
        hold = hold_projection(deal, size_loan(investor_terms(), deal.price), HOLD)
        assert [year.year for year in hold.years] == [1, 2, 3, 4, 5]
        assert hold.years[0].noi == pytest.approx(30_901.92, abs=0.01)
        for earlier, later in zip(hold.years, hold.years[1:], strict=False):
            assert later.noi == pytest.approx(earlier.noi * 1.03)
            assert later.debt_service == earlier.debt_service

    def test_share_of_rent_expenses_follow_rents_and_dollar_expenses_follow_their_own_rate(self):
        deal = make_deal()
        flat_rents = HoldAssumptions(
            years=2, rent_growth=0.0, expense_growth=0.10, appreciation=0.0, selling_cost_pct=0.06
        )
        hold = hold_projection(deal, size_loan(investor_terms(), deal.price), flat_rents)
        first, second = hold.years
        assert second.egi == pytest.approx(first.egi)
        # Taxes, insurance, utilities and misc are the dollar-based lines: $14,160 in year 1.
        assert second.opex - first.opex == pytest.approx(14_160 * 0.10)

    def test_cash_purchase_has_no_balance_to_repay(self):
        deal = make_deal()
        hold = hold_projection(deal, size_loan(CASH, deal.price), HOLD)
        assert hold.loan_balance_at_sale == 0
        assert hold.net_sale_proceeds == pytest.approx(hold.sale_price * 0.94)
        assert hold.cash_invested == pytest.approx(480_000 * 1.03)
        assert hold.irr is not None and hold.irr > 0


class TestMaxAllowableOffer:
    def test_price_sits_on_the_boundary(self):
        deal, terms = make_deal(), investor_terms()
        mao = max_allowable_offer(deal, terms, TARGETS)
        assert mao.price is not None

        def failing(price: float):
            return failing_targets(
                compute_kpis(deal.replace(price=price), size_loan(terms, price)), TARGETS
            )

        assert failing(mao.price) == ()
        assert failing(mao.price + 100) == ("CoC",)

    @pytest.mark.parametrize(
        ("targets", "expected", "binding"),
        [
            (
                Targets(min_cash_flow_per_door_month=100, min_coc=None, min_dscr=None),
                443_282,
                "cash flow per door",
            ),
            (
                Targets(min_cash_flow_per_door_month=None, min_coc=0.06, min_dscr=None),
                413_431,
                "CoC",
            ),
            (
                Targets(min_cash_flow_per_door_month=None, min_coc=None, min_dscr=1.20),
                437_218,
                "DSCR",
            ),
        ],
    )
    def test_each_target_alone(self, targets, expected, binding):
        mao = max_allowable_offer(make_deal(), investor_terms(), targets)
        assert mao.price == pytest.approx(expected, abs=50)
        assert mao.binding == (binding,)

    def test_no_targets_means_no_answer(self):
        nothing = Targets(min_cash_flow_per_door_month=None, min_coc=None, min_dscr=None)
        mao = max_allowable_offer(make_deal(), investor_terms(), nothing)
        assert mao.price is None
        assert mao.reason == "no targets are set"

    def test_rents_too_low_for_any_price(self):
        units = tuple(Unit(in_place_rent=300) for _ in range(4))
        mao = max_allowable_offer(make_deal(units=units), investor_terms(), TARGETS)
        assert mao.price is None
        assert mao.reason == "targets cannot be met at any price"

    def test_a_tax_bill_floor_lowers_the_offer(self):
        plain = max_allowable_offer(make_deal(), investor_terms(), TARGETS)
        floored = max_allowable_offer(make_deal(tax_floor=9_000), investor_terms(), TARGETS)
        assert plain.price is not None and floored.price is not None
        assert floored.price < plain.price

    def test_cash_skips_the_dscr_target(self):
        mao = max_allowable_offer(make_deal(), CASH, TARGETS)
        assert mao.price is not None
        assert "DSCR" not in mao.binding


class TestSensitivity:
    SHOCKS = (
        Shock(name="none"),
        Shock(name="rate up", rate_delta=0.01),
        Shock(name="rate down", rate_delta=-0.01),
        Shock(name="rents down", rent_factor=0.90),
        Shock(name="vacancy up", vacancy_delta=0.05),
        Shock(name="insurance up", insurance_factor=1.5),
        Shock(name="taxes up", tax_factor=1.25),
    )

    def test_each_case_moves_cash_flow_the_right_way(self):
        rows = {row.name: row for row in sensitivity(make_deal(), investor_terms(), self.SHOCKS)}
        assert rows["none"].change_door_month == pytest.approx(0)
        assert rows["none"].cash_flow_door_month == pytest.approx(45.02, abs=0.01)
        assert rows["rate down"].change_door_month > 0
        for name in ("rate up", "rents down", "vacancy up", "insurance up", "taxes up"):
            assert rows[name].change_door_month < 0, name

    def test_known_amounts(self):
        rows = {row.name: row for row in sensitivity(make_deal(), investor_terms(), self.SHOCKS)}
        # Insurance +50% is $1,800 a year and taxes +25% is $1,440 a year, over four doors.
        assert rows["insurance up"].change_door_month == pytest.approx(-37.50)
        assert rows["taxes up"].change_door_month == pytest.approx(-30.00)

    def test_tax_shock_also_lifts_the_last_bill_floor(self):
        rows = sensitivity(
            make_deal(tax_floor=8_000), investor_terms(), (Shock(name="taxes", tax_factor=1.25),)
        )
        assert rows[0].change_door_month == pytest.approx(-8_000 * 0.25 / 48)

    def test_rate_shock_does_nothing_to_a_cash_purchase(self):
        rows = sensitivity(make_deal(), CASH, (Shock(name="rate up", rate_delta=0.01),))
        assert rows[0].change_door_month == pytest.approx(0)
        assert rows[0].dscr is None

    def test_vacancy_cannot_reach_one_hundred_percent(self):
        rows = sensitivity(make_deal(), investor_terms(), (Shock(name="v", vacancy_delta=2.0),))
        assert rows[0].cash_flow_door_month < 0
