"""Operating statement and KPIs for one property under one loan."""

from __future__ import annotations

from dealfinder.underwrite.financing import Loan, monthly_pitia, property_taxes
from dealfinder.underwrite.inputs import DealInputs, Frozen


class OperatingStatement(Frozen):
    """One year of income and expenses.

    ``gsr`` covers the rented units. ``building_gsr`` covers every unit, including one the owner
    lives in, and is the base for repairs and reserves because those cover the whole building.
    """

    gsr: float
    building_gsr: float
    vacancy_loss: float
    egi: float
    taxes: float
    insurance: float
    flood_insurance: float
    repairs: float
    reserves: float
    management: float
    utilities: float
    hoa: float
    misc: float
    opex: float
    noi: float

    @property
    def percent_based_expenses(self) -> float:
        """Expenses that move with rents: repairs, reserves and management."""
        return self.repairs + self.reserves + self.management

    @property
    def dollar_based_expenses(self) -> float:
        return self.opex - self.percent_based_expenses


def operating_statement(
    deal: DealInputs, *, owner_unit: int | None = None, pro_forma: bool = False
) -> OperatingStatement:
    """Build the statement.

    ``owner_unit`` (0-based) removes that unit's rent from income for a house hack. ``pro_forma``
    uses market rents instead of the lower of in-place and market.
    """
    rents = [u.pro_forma_rent if pro_forma else u.current_rent for u in deal.units]
    building_gsr = 12 * sum(rents)
    gsr = building_gsr
    if owner_unit is not None:
        if not 0 <= owner_unit < len(rents):
            raise IndexError(f"owner_unit {owner_unit} is not one of {len(rents)} units")
        gsr -= 12 * rents[owner_unit]

    units = deal.unit_count
    egi = gsr * (1 - deal.vacancy_rate)
    taxes = property_taxes(deal)
    insurance = deal.insurance_per_unit_year * units
    repairs = deal.repairs_pct * building_gsr
    reserves = deal.reserves_pct * building_gsr
    management = deal.management_pct * egi
    utilities = deal.utilities_per_unit_month * units * 12
    hoa = deal.hoa_month * 12
    misc = deal.misc_per_unit_year * units
    opex = (
        taxes
        + insurance
        + deal.flood_insurance_year
        + repairs
        + reserves
        + management
        + utilities
        + hoa
        + misc
    )
    return OperatingStatement(
        gsr=gsr,
        building_gsr=building_gsr,
        vacancy_loss=gsr - egi,
        egi=egi,
        taxes=taxes,
        insurance=insurance,
        flood_insurance=deal.flood_insurance_year,
        repairs=repairs,
        reserves=reserves,
        management=management,
        utilities=utilities,
        hoa=hoa,
        misc=misc,
        opex=opex,
        noi=egi - opex,
    )


def cash_invested(deal: DealInputs, loan: Loan) -> float:
    """Down payment + closing costs + rehab + points - credits."""
    return (
        loan.down_payment
        + deal.price * deal.closing_cost_pct
        + deal.rehab
        + loan.points_cost
        - deal.credits
    )


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


class Kpis(Frozen):
    """Year-one results with every unit rented. Ratios are None when undefined, for example
    DSCR on a cash purchase."""

    statement: OperatingStatement
    cap_rate: float
    annual_debt_service: float
    cash_flow_year: float
    cash_flow_month: float
    cash_flow_door_month: float
    cash_invested: float
    coc: float | None
    dscr: float | None  # NOI / ADS
    lender_dscr: float | None  # monthly gross rent / PITIA
    grm: float | None  # price / GSR
    rent_to_price: float  # monthly rent / price (the 1% rule)
    expense_ratio: float | None  # OpEx / EGI
    break_even_occupancy: float | None  # (OpEx + ADS) / GSR
    price_per_door: float
    price_per_sqft: float | None
    year1_principal_paydown: float
    year1_total_return: float | None  # (cash flow + principal paydown) / cash invested
    rent_upside: float | None  # market GSR / current GSR - 1


def compute_kpis(deal: DealInputs, loan: Loan) -> Kpis:
    statement = operating_statement(deal)
    ads = loan.annual_debt_service
    cash_flow = statement.noi - ads
    invested = cash_invested(deal, loan)
    paydown = loan.amount - loan.balance_after(12)
    market_gsr = 12 * sum(u.pro_forma_rent for u in deal.units)
    has_debt = ads > 0
    return Kpis(
        statement=statement,
        cap_rate=statement.noi / deal.price,
        annual_debt_service=ads,
        cash_flow_year=cash_flow,
        cash_flow_month=cash_flow / 12,
        cash_flow_door_month=cash_flow / 12 / deal.unit_count,
        cash_invested=invested,
        coc=_ratio(cash_flow, invested),
        dscr=statement.noi / ads if has_debt else None,
        lender_dscr=statement.gsr / 12 / monthly_pitia(deal, loan) if has_debt else None,
        grm=_ratio(deal.price, statement.gsr),
        rent_to_price=statement.gsr / 12 / deal.price,
        expense_ratio=_ratio(statement.opex, statement.egi),
        break_even_occupancy=_ratio(statement.opex + ads, statement.gsr),
        price_per_door=deal.price / deal.unit_count,
        price_per_sqft=deal.price / deal.building_sqft if deal.building_sqft else None,
        year1_principal_paydown=paydown,
        year1_total_return=_ratio(cash_flow + paydown, invested),
        rent_upside=(market_gsr / statement.gsr - 1) if statement.gsr > 0 else None,
    )
