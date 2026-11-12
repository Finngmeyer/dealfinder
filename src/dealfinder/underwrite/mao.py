"""Maximum allowable offer: the highest price that still meets every buy-box target."""

from __future__ import annotations

from dealfinder.underwrite.engine import Kpis, compute_kpis
from dealfinder.underwrite.financing import highest_passing_price, size_loan
from dealfinder.underwrite.inputs import DealInputs, Frozen, LoanTerms, Targets


class MaoResult(Frozen):
    price: float | None
    binding: tuple[str, ...]  # targets that fail first just above the price
    reason: str | None = None  # why there is no price


def failing_targets(kpis: Kpis, targets: Targets) -> tuple[str, ...]:
    """Names of the targets these results miss. A target set to None is skipped, and so is
    DSCR when there is no debt."""
    failing: list[str] = []
    floor = targets.min_cash_flow_per_door_month
    if floor is not None and kpis.cash_flow_door_month < floor:
        failing.append("cash flow per door")
    if targets.min_coc is not None and kpis.coc is not None and kpis.coc < targets.min_coc:
        failing.append("CoC")
    if targets.min_dscr is not None and kpis.dscr is not None and kpis.dscr < targets.min_dscr:
        failing.append("DSCR")
    return tuple(failing)


def max_allowable_offer(deal: DealInputs, terms: LoanTerms, targets: Targets) -> MaoResult:
    """Bisect on price. Taxes, closing costs, the down payment, the loan and its mortgage
    insurance are all recomputed at each price; rents and the other expenses stay put."""
    if not targets.any_set:
        return MaoResult(price=None, binding=(), reason="no targets are set")

    def kpis_at(price: float) -> Kpis:
        return compute_kpis(deal.replace(price=price), size_loan(terms, price))

    def passes(price: float) -> bool:
        return not failing_targets(kpis_at(price), targets)

    price = highest_passing_price(passes, deal.price)
    if price is None:
        met_nowhere = not passes(1.0)
        reason = "targets cannot be met at any price" if met_nowhere else "no upper limit found"
        return MaoResult(price=None, binding=(), reason=reason)
    return MaoResult(price=price, binding=failing_targets(kpis_at(price + 1.0), targets))
