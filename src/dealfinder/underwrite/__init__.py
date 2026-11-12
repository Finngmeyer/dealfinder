"""Underwriting engine: pure functions over frozen inputs. Nothing in this package does I/O."""

from dealfinder.underwrite.amortization import balance_after, monthly_payment
from dealfinder.underwrite.engine import (
    Kpis,
    OperatingStatement,
    cash_invested,
    compute_kpis,
    operating_statement,
)
from dealfinder.underwrite.financing import (
    Loan,
    SelfSufficiency,
    annual_mi_rate,
    monthly_pitia,
    property_taxes,
    self_sufficiency,
    size_loan,
)
from dealfinder.underwrite.hold import HoldResult, HoldYear, hold_projection, irr
from dealfinder.underwrite.househack import HouseHack, default_owner_unit, house_hack
from dealfinder.underwrite.inputs import (
    DealInputs,
    HoldAssumptions,
    LoanTerms,
    MiTier,
    Shock,
    Targets,
    Unit,
)
from dealfinder.underwrite.mao import MaoResult, failing_targets, max_allowable_offer
from dealfinder.underwrite.scenario import ScenarioResult, run_scenario
from dealfinder.underwrite.sensitivity import SensitivityRow, apply_shock, sensitivity
from dealfinder.underwrite.version import ENGINE_VERSION

__all__ = [
    "ENGINE_VERSION",
    "DealInputs",
    "HoldAssumptions",
    "HoldResult",
    "HoldYear",
    "HouseHack",
    "Kpis",
    "Loan",
    "LoanTerms",
    "MaoResult",
    "MiTier",
    "OperatingStatement",
    "ScenarioResult",
    "SelfSufficiency",
    "SensitivityRow",
    "Shock",
    "Targets",
    "Unit",
    "annual_mi_rate",
    "apply_shock",
    "balance_after",
    "cash_invested",
    "compute_kpis",
    "default_owner_unit",
    "failing_targets",
    "hold_projection",
    "house_hack",
    "irr",
    "max_allowable_offer",
    "monthly_payment",
    "monthly_pitia",
    "operating_statement",
    "property_taxes",
    "run_scenario",
    "self_sufficiency",
    "sensitivity",
    "size_loan",
]
