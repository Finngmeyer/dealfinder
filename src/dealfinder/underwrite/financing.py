"""Loan sizing, mortgage insurance and the FHA self-sufficiency test."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from dealfinder.underwrite.amortization import balance_after, monthly_payment
from dealfinder.underwrite.inputs import DealInputs, Frozen, LoanTerms, MiTier

_LTV_EPSILON = 1e-9  # so a loan at exactly the threshold is not pushed over it by float noise
_PRICE_TOLERANCE = 0.5  # dollars; bisection stops once the bracket is this narrow
_PRICE_CEILING = 1e10


class Loan(Frozen):
    """A loan sized for one purchase price."""

    price: float
    down_payment: float
    base_loan: float
    financed_mi: float  # upfront mortgage insurance rolled into the loan
    amount: float  # base loan + financed mortgage insurance
    ltv: float  # base loan / price
    annual_rate: float
    term_months: int
    monthly_pi: float
    annual_mi_rate: float
    monthly_mi: float
    points_cost: float
    capped_by_limit: bool

    @property
    def monthly_debt_service(self) -> float:
        return self.monthly_pi + self.monthly_mi

    @property
    def annual_debt_service(self) -> float:
        """ADS = 12 x (P&I + monthly mortgage insurance)."""
        return 12 * self.monthly_debt_service

    def balance_after(self, payments_made: int) -> float:
        return balance_after(self.amount, self.annual_rate, self.term_months, payments_made)


def annual_mi_rate(tiers: Sequence[MiTier], base_loan: float, ltv: float) -> float:
    """Annual mortgage-insurance rate for a base loan and loan-to-value."""
    for tier in tiers:
        if tier.max_base_loan is None or base_loan <= tier.max_base_loan:
            above = ltv > tier.ltv_above + _LTV_EPSILON
            return tier.rate_above if above else tier.rate_at_or_below
    return 0.0


def size_loan(terms: LoanTerms, price: float) -> Loan:
    """Size the loan for a price.

    The down payment is the preset's share of price. When the resulting base loan is over the
    preset's limit, the loan is held at the limit and the gap moves into the down payment.
    Upfront mortgage insurance is financed on top of the base loan, which is why the limit
    applies to the base loan only.
    """
    term_months = terms.term_years * 12
    if terms.is_cash:
        return Loan(
            price=price,
            down_payment=price,
            base_loan=0.0,
            financed_mi=0.0,
            amount=0.0,
            ltv=0.0,
            annual_rate=0.0,
            term_months=term_months,
            monthly_pi=0.0,
            annual_mi_rate=0.0,
            monthly_mi=0.0,
            points_cost=0.0,
            capped_by_limit=False,
        )

    base_loan = price * (1 - terms.down_payment_pct)
    capped = terms.max_loan is not None and base_loan > terms.max_loan
    if capped:
        assert terms.max_loan is not None
        base_loan = terms.max_loan
    financed_mi = base_loan * terms.upfront_mi_pct
    amount = base_loan + financed_mi
    ltv = base_loan / price
    mi_rate = annual_mi_rate(terms.annual_mi, base_loan, ltv)
    return Loan(
        price=price,
        down_payment=price - base_loan,
        base_loan=base_loan,
        financed_mi=financed_mi,
        amount=amount,
        ltv=ltv,
        annual_rate=terms.annual_rate,
        term_months=term_months,
        monthly_pi=monthly_payment(amount, terms.annual_rate, term_months),
        annual_mi_rate=mi_rate,
        monthly_mi=base_loan * mi_rate / 12,
        points_cost=amount * terms.points_pct,
        capped_by_limit=capped,
    )


def property_taxes(deal: DealInputs) -> float:
    """Annual property tax: price x rate, never below the last tax bill."""
    return max(deal.price * deal.tax_rate, deal.tax_floor)


def monthly_pitia(deal: DealInputs, loan: Loan) -> float:
    """Principal, interest, taxes, insurance (hazard, flood, mortgage) and association dues."""
    insurance = deal.insurance_per_unit_year * deal.unit_count + deal.flood_insurance_year
    return (
        loan.monthly_pi + loan.monthly_mi + (property_taxes(deal) + insurance) / 12 + deal.hoa_month
    )


def highest_passing_price(passes: Callable[[float], bool], start: float) -> float | None:
    """Largest price for which ``passes`` holds, assuming it holds below a threshold and fails
    above it. Returns None when it fails even at one dollar or never fails."""
    low = 1.0
    if not passes(low):
        return None
    high = max(start, 2.0)
    while passes(high):
        low = high
        high *= 2
        if high > _PRICE_CEILING:
            return None
    while high - low > _PRICE_TOLERANCE:
        mid = (low + high) / 2
        if passes(mid):
            low = mid
        else:
            high = mid
    return low


class SelfSufficiency(Frozen):
    """FHA test for 3-4 units: a share of the market rent of ALL units, the owner's included,
    must cover principal, interest, taxes, insurance, mortgage insurance and dues."""

    market_rent_month: float
    allowed_payment: float  # factor x market rent
    payment: float
    margin: float  # allowed - payment; negative fails
    passes: bool
    max_passing_price: float | None


def self_sufficiency(deal: DealInputs, terms: LoanTerms) -> SelfSufficiency | None:
    """Run the test, or return None when it does not apply to this loan or unit count."""
    factor = terms.self_sufficiency_factor
    if factor is None or deal.unit_count not in terms.self_sufficiency_units:
        return None
    market_rent = sum(u.pro_forma_rent for u in deal.units)
    allowed = factor * market_rent

    def payment_at(price: float) -> float:
        priced = deal.replace(price=price)
        return monthly_pitia(priced, size_loan(terms, price))

    payment = payment_at(deal.price)
    return SelfSufficiency(
        market_rent_month=market_rent,
        allowed_payment=allowed,
        payment=payment,
        margin=allowed - payment,
        passes=payment <= allowed,
        max_passing_price=highest_passing_price(lambda p: payment_at(p) <= allowed, deal.price),
    )
