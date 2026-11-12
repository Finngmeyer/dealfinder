"""Run one financing scenario end to end."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from dealfinder.underwrite.engine import Kpis, compute_kpis
from dealfinder.underwrite.financing import (
    Loan,
    SelfSufficiency,
    monthly_pitia,
    self_sufficiency,
    size_loan,
)
from dealfinder.underwrite.hold import HoldResult, hold_projection
from dealfinder.underwrite.househack import HouseHack, house_hack
from dealfinder.underwrite.inputs import (
    DealInputs,
    Frozen,
    HoldAssumptions,
    LoanTerms,
    Shock,
    Targets,
)
from dealfinder.underwrite.mao import MaoResult, max_allowable_offer
from dealfinder.underwrite.sensitivity import SensitivityRow, sensitivity
from dealfinder.underwrite.version import ENGINE_VERSION


class ScenarioResult(Frozen):
    """Everything computed for one loan preset.

    ``kpis``, ``hold``, ``mao`` and ``sensitivity`` always describe the building with every unit
    rented. For an owner-occupied loan that is the "moved out" view: all units rented, a
    property manager paid, same loan. The owner's own numbers are in ``house_hack``.
    """

    terms: LoanTerms
    view: Literal["rented", "moved_out"]
    loan: Loan
    kpis: Kpis
    hold: HoldResult
    mao: MaoResult
    sensitivity: tuple[SensitivityRow, ...]
    self_sufficiency: SelfSufficiency | None
    house_hack: HouseHack | None
    reserves_required: float  # liquid reserves the lender wants; not part of cash invested
    notes: tuple[str, ...]
    engine_version: str


def _notes(terms: LoanTerms, loan: Loan, kpis: Kpis, test: SelfSufficiency | None) -> list[str]:
    notes: list[str] = []
    if loan.capped_by_limit:
        notes.append(
            f"Loan held at the ${loan.base_loan:,.0f} limit, so the down payment rises to "
            f"${loan.down_payment:,.0f} ({loan.down_payment / loan.price:.1%} of price)."
        )
    if test is not None and not test.passes:
        notes.append(
            f"Fails FHA self-sufficiency by ${-test.margin:,.2f} a month"
            + (
                f"; the highest price that passes is ${test.max_passing_price:,.0f}."
                if test.max_passing_price is not None
                else "; no price passes."
            )
        )
    if terms.min_lender_dscr is not None and kpis.lender_dscr is not None:
        if kpis.lender_dscr < terms.min_lender_dscr:
            notes.append(
                f"Lender DSCR {kpis.lender_dscr:.2f} is below {terms.min_lender_dscr:.2f}: "
                "does not qualify."
            )
        elif terms.best_lender_dscr is not None and kpis.lender_dscr < terms.best_lender_dscr:
            notes.append(
                f"Qualifies at lender DSCR {kpis.lender_dscr:.2f}; best pricing needs "
                f"{terms.best_lender_dscr:.2f}."
            )
    return notes


def run_scenario(
    deal: DealInputs,
    terms: LoanTerms,
    *,
    targets: Targets,
    hold: HoldAssumptions,
    shocks: Sequence[Shock],
    manager_pct: float,
    owner_unit: int | None = None,
    comparison_rent: float | None = None,
) -> ScenarioResult:
    """``deal.management_pct`` is used as given for investor loans. Owner-occupied loans charge
    ``manager_pct`` in the moved-out view and nothing while the owner lives there."""
    loan = size_loan(terms, deal.price)
    rented_deal = deal.replace(management_pct=manager_pct) if terms.owner_occupied else deal
    kpis = compute_kpis(rented_deal, loan)
    test = self_sufficiency(deal, terms)
    hack = (
        house_hack(deal, loan, owner_unit=owner_unit, comparison_rent=comparison_rent)
        if terms.owner_occupied
        else None
    )
    return ScenarioResult(
        terms=terms,
        view="moved_out" if terms.owner_occupied else "rented",
        loan=loan,
        kpis=kpis,
        hold=hold_projection(rented_deal, loan, hold),
        mao=max_allowable_offer(rented_deal, terms, targets),
        sensitivity=sensitivity(rented_deal, terms, shocks),
        self_sufficiency=test,
        house_hack=hack,
        reserves_required=terms.reserves_months * monthly_pitia(deal, loan),
        notes=tuple(_notes(terms, loan, kpis, test)),
        engine_version=ENGINE_VERSION,
    )
