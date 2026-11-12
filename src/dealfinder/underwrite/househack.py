"""House-hack view: the owner lives in one unit and rents out the rest."""

from __future__ import annotations

from dealfinder.underwrite.engine import OperatingStatement, operating_statement
from dealfinder.underwrite.financing import Loan
from dealfinder.underwrite.inputs import DealInputs, Frozen


class HouseHack(Frozen):
    owner_unit: int  # 0-based
    statement: OperatingStatement
    owner_net_monthly_cost: float  # what living there costs after the other units' rent
    comparison_rent: float  # what renting instead would cost
    savings_vs_renting: float  # comparison rent - net cost; negative means renting is cheaper


def default_owner_unit(deal: DealInputs) -> int:
    """The highest-rent unit, since giving that one up is the pessimistic case."""
    rents = [u.current_rent for u in deal.units]
    return rents.index(max(rents))


def house_hack(
    deal: DealInputs,
    loan: Loan,
    *,
    owner_unit: int | None = None,
    comparison_rent: float | None = None,
    owner_management_pct: float = 0.0,
) -> HouseHack:
    """Income comes from the other units and the owner manages. Repairs, reserves, utilities
    and misc still cover the whole building.

    ``comparison_rent`` defaults to the market rent of the unit the owner takes.
    """
    unit = default_owner_unit(deal) if owner_unit is None else owner_unit
    statement = operating_statement(
        deal.replace(management_pct=owner_management_pct), owner_unit=unit
    )
    net_cost = (statement.opex + loan.annual_debt_service - statement.egi) / 12
    comparison = deal.units[unit].pro_forma_rent if comparison_rent is None else comparison_rent
    return HouseHack(
        owner_unit=unit,
        statement=statement,
        owner_net_monthly_cost=net_cost,
        comparison_rent=comparison,
        savings_vs_renting=comparison - net_cost,
    )
