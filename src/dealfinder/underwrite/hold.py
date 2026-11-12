"""Hold-period model: yearly cash flows, sale at the end, IRR and equity multiple."""

from __future__ import annotations

from collections.abc import Sequence

from dealfinder.underwrite.engine import cash_invested, operating_statement
from dealfinder.underwrite.financing import Loan
from dealfinder.underwrite.inputs import DealInputs, Frozen, HoldAssumptions


class HoldYear(Frozen):
    year: int
    gsr: float
    egi: float
    opex: float
    noi: float
    debt_service: float
    cash_flow: float


class HoldResult(Frozen):
    years: tuple[HoldYear, ...]
    sale_price: float
    selling_costs: float
    loan_balance_at_sale: float
    net_sale_proceeds: float
    cash_invested: float
    irr: float | None
    equity_multiple: float | None


def irr(flows: Sequence[float]) -> float | None:
    """Annual internal rate of return for flows at years 0, 1, 2, ...

    Returns None when no rate between -99% and a very large number sets the net present value
    to zero, which happens when nothing is invested up front or the money is never recovered.
    """

    def npv(rate: float) -> float:
        return sum(flow / (1 + rate) ** year for year, flow in enumerate(flows))

    if len(flows) < 2 or flows[0] >= 0:
        return None
    low, high = -0.99, 1.0
    if npv(low) <= 0:
        return None
    while npv(high) > 0:
        high *= 2
        if high > 1e6:
            return None
    for _ in range(200):
        mid = (low + high) / 2
        if npv(mid) > 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def hold_projection(deal: DealInputs, loan: Loan, hold: HoldAssumptions) -> HoldResult:
    """Project the hold.

    Rents grow from year 2. Expenses set as a share of rent (repairs, reserves, management)
    follow that year's rents. Dollar-based expenses grow at the expense rate. Debt service is
    flat. The property sells at the end of the last year at price x (1 + appreciation)^years,
    less selling costs and the loan balance. Year 0 is minus the cash invested.
    """
    base = operating_statement(deal)
    debt_service = loan.annual_debt_service
    years: list[HoldYear] = []
    for year in range(1, hold.years + 1):
        rent_factor = (1 + hold.rent_growth) ** (year - 1)
        expense_factor = (1 + hold.expense_growth) ** (year - 1)
        egi = base.egi * rent_factor
        opex = (
            base.percent_based_expenses * rent_factor + base.dollar_based_expenses * expense_factor
        )
        noi = egi - opex
        years.append(
            HoldYear(
                year=year,
                gsr=base.gsr * rent_factor,
                egi=egi,
                opex=opex,
                noi=noi,
                debt_service=debt_service,
                cash_flow=noi - debt_service,
            )
        )

    sale_price = deal.price * (1 + hold.appreciation) ** hold.years
    selling_costs = sale_price * hold.selling_cost_pct
    balance = loan.balance_after(hold.years * 12)
    net_proceeds = sale_price - selling_costs - balance

    invested = cash_invested(deal, loan)
    operating_total = sum(y.cash_flow for y in years)
    flows = [-invested, *(y.cash_flow for y in years)]
    flows[-1] += net_proceeds
    return HoldResult(
        years=tuple(years),
        sale_price=sale_price,
        selling_costs=selling_costs,
        loan_balance_at_sale=balance,
        net_sale_proceeds=net_proceeds,
        cash_invested=invested,
        irr=irr(flows),
        equity_multiple=(operating_total + net_proceeds) / invested if invested > 0 else None,
    )
