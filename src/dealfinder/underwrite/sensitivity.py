"""Sensitivity: rerun the deal with one assumption moved at a time."""

from __future__ import annotations

from collections.abc import Sequence

from dealfinder.underwrite.engine import compute_kpis
from dealfinder.underwrite.financing import size_loan
from dealfinder.underwrite.inputs import DealInputs, Frozen, LoanTerms, Shock

_MAX_VACANCY = 0.99


class SensitivityRow(Frozen):
    name: str
    cash_flow_door_month: float
    change_door_month: float  # against the unshocked deal
    coc: float | None
    dscr: float | None


def apply_shock(deal: DealInputs, terms: LoanTerms, shock: Shock) -> tuple[DealInputs, LoanTerms]:
    """Rent and insurance factors hit every rent and both hazard and flood insurance. The tax
    factor hits the rate and the last-bill floor alike."""
    units = deal.units
    if shock.rent_factor != 1.0:
        units = tuple(
            u.model_copy(
                update={
                    "in_place_rent": None
                    if u.in_place_rent is None
                    else u.in_place_rent * shock.rent_factor,
                    "market_rent": None
                    if u.market_rent is None
                    else u.market_rent * shock.rent_factor,
                }
            )
            for u in deal.units
        )
    shocked_deal = deal.replace(
        units=units,
        vacancy_rate=min(_MAX_VACANCY, max(0.0, deal.vacancy_rate + shock.vacancy_delta)),
        insurance_per_unit_year=deal.insurance_per_unit_year * shock.insurance_factor,
        flood_insurance_year=deal.flood_insurance_year * shock.insurance_factor,
        tax_rate=deal.tax_rate * shock.tax_factor,
        tax_floor=deal.tax_floor * shock.tax_factor,
    )
    shocked_terms = terms.model_copy(
        update={"annual_rate": max(0.0, terms.annual_rate + shock.rate_delta)}
    )
    return shocked_deal, shocked_terms


def sensitivity(
    deal: DealInputs, terms: LoanTerms, shocks: Sequence[Shock]
) -> tuple[SensitivityRow, ...]:
    base = compute_kpis(deal, size_loan(terms, deal.price))
    rows: list[SensitivityRow] = []
    for shock in shocks:
        shocked_deal, shocked_terms = apply_shock(deal, terms, shock)
        kpis = compute_kpis(shocked_deal, size_loan(shocked_terms, shocked_deal.price))
        rows.append(
            SensitivityRow(
                name=shock.name,
                cash_flow_door_month=kpis.cash_flow_door_month,
                change_door_month=kpis.cash_flow_door_month - base.cash_flow_door_month,
                coc=kpis.coc,
                dscr=kpis.dscr,
            )
        )
    return tuple(rows)
