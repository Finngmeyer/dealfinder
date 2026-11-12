from __future__ import annotations

import pytest

from conftest import CASH, PMI, fha_terms, investor_terms, make_deal
from dealfinder.underwrite import (
    Unit,
    monthly_pitia,
    property_taxes,
    self_sufficiency,
    size_loan,
)

FHA_FOUR_UNIT_FLOOR = 1_041_125


@pytest.mark.parametrize(
    ("price", "down", "expected_rate"),
    [
        (480_000, 0.035, 0.0055),  # base loan under $726,200, LTV 96.5%
        (480_000, 0.05, 0.0050),  # LTV exactly 95% is not "above 95%"
        (800_000, 0.035, 0.0075),  # base loan $772,000, LTV 96.5%
        (900_000, 0.10, 0.0070),  # base loan $810,000, LTV 90%
    ],
)
def test_fha_annual_mip_tiers(price, down, expected_rate):
    loan = size_loan(fha_terms(down_payment_pct=down), price)
    assert loan.annual_mi_rate == expected_rate
    assert loan.monthly_mi == pytest.approx(loan.base_loan * expected_rate / 12)


def test_fha_upfront_mip_is_financed_on_top_of_the_base_loan():
    loan = size_loan(fha_terms(), 480_000)
    assert loan.base_loan == pytest.approx(463_200)
    assert loan.financed_mi == pytest.approx(8_106)
    assert loan.amount == pytest.approx(471_306)
    assert loan.down_payment == pytest.approx(16_800)
    assert not loan.capped_by_limit


def test_loan_limit_caps_the_base_loan_and_raises_the_down_payment():
    loan = size_loan(fha_terms(max_loan=FHA_FOUR_UNIT_FLOOR), 1_200_000)
    assert loan.capped_by_limit
    assert loan.base_loan == FHA_FOUR_UNIT_FLOOR
    assert loan.down_payment == pytest.approx(1_200_000 - FHA_FOUR_UNIT_FLOOR)
    assert loan.amount == pytest.approx(FHA_FOUR_UNIT_FLOOR * 1.0175)
    # LTV is now under 95% on a base loan over $726,200.
    assert loan.annual_mi_rate == 0.0070


def test_limit_does_not_bind_below_it():
    loan = size_loan(fha_terms(max_loan=FHA_FOUR_UNIT_FLOOR), 480_000)
    assert not loan.capped_by_limit
    assert loan.base_loan == pytest.approx(463_200)


def test_pmi_applies_only_above_80_percent_ltv():
    five_down = size_loan(investor_terms(down_payment_pct=0.05, annual_mi=PMI), 480_000)
    assert five_down.monthly_mi == pytest.approx(456_000 * 0.006 / 12)
    twenty_down = size_loan(investor_terms(down_payment_pct=0.20, annual_mi=PMI), 480_000)
    assert twenty_down.monthly_mi == 0.0


def test_cash_has_no_loan():
    loan = size_loan(CASH, 480_000)
    assert loan.amount == 0
    assert loan.down_payment == 480_000
    assert loan.annual_debt_service == 0


def test_points_are_a_share_of_the_loan():
    loan = size_loan(investor_terms(points_pct=0.01), 480_000)
    assert loan.points_cost == pytest.approx(3_600)


def test_taxes_never_fall_below_the_last_bill():
    assert property_taxes(make_deal()) == pytest.approx(5_760)
    assert property_taxes(make_deal(tax_floor=7_000)) == pytest.approx(7_000)
    assert property_taxes(make_deal(tax_floor=1_000)) == pytest.approx(5_760)


def test_pitia_includes_flood_insurance_mortgage_insurance_and_dues():
    deal = make_deal(flood_insurance_year=2_400, hoa_month=50)
    loan = size_loan(fha_terms(), deal.price)
    expected = loan.monthly_pi + loan.monthly_mi + 480 + (3_600 + 2_400) / 12 + 50
    assert monthly_pitia(deal, loan) == pytest.approx(expected)


class TestSelfSufficiency:
    def test_only_for_loans_and_unit_counts_that_have_the_test(self):
        assert self_sufficiency(make_deal(), investor_terms()) is None
        duplex = make_deal(units=(Unit(in_place_rent=1_350), Unit(in_place_rent=1_350)))
        assert self_sufficiency(duplex, fha_terms()) is None
        triplex = make_deal(units=tuple(Unit(in_place_rent=1_350) for _ in range(3)))
        assert self_sufficiency(triplex, fha_terms()) is not None

    def test_uses_market_rent_of_every_unit(self):
        units = tuple(Unit(in_place_rent=1_000, market_rent=1_350) for _ in range(4))
        test = self_sufficiency(make_deal(units=units), fha_terms())
        assert test is not None
        assert test.market_rent_month == 5_400
        assert test.allowed_payment == pytest.approx(4_050)

    def test_failing_deal_reports_the_price_that_would_pass(self):
        deal = make_deal(price=600_000)
        terms = fha_terms()
        test = self_sufficiency(deal, terms)
        assert test is not None
        assert not test.passes
        assert test.margin < 0
        assert test.max_passing_price == pytest.approx(490_293, abs=50)

    def test_max_passing_price_is_the_boundary(self):
        terms = fha_terms()
        test = self_sufficiency(make_deal(), terms)
        assert test is not None and test.max_passing_price is not None
        at_max = self_sufficiency(make_deal(price=test.max_passing_price), terms)
        above = self_sufficiency(make_deal(price=test.max_passing_price + 100), terms)
        assert at_max is not None and at_max.passes
        assert above is not None and not above.passes

    def test_no_price_passes_when_fixed_costs_exceed_the_allowed_payment(self):
        units = tuple(Unit(in_place_rent=300) for _ in range(4))
        test = self_sufficiency(make_deal(units=units, hoa_month=900), fha_terms())
        assert test is not None
        assert not test.passes
        assert test.max_passing_price is None
