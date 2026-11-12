"""Inputs to the underwriting engine: frozen data, no I/O, no defaults for business values.

Every number the engine uses arrives through these models. Defaults live in
``config/settings.yaml`` and are filled in by ``dealfinder.analysis`` before the engine runs.
Rates and shares are fractions (0.07 is 7%). Money is dollars.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Unit(Frozen):
    """One unit. Rents are dollars per month.

    ``in_place_rent`` is what the unit rents for today according to the listing or lease, and is
    None when the unit is vacant or the rent is unknown. ``market_rent`` is an estimate of what
    it would rent for.
    """

    in_place_rent: float | None = Field(default=None, ge=0)
    market_rent: float | None = Field(default=None, ge=0)
    beds: float | None = Field(default=None, ge=0)
    baths: float | None = Field(default=None, ge=0)
    sqft: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _needs_a_rent(self) -> Unit:
        if self.in_place_rent is None and self.market_rent is None:
            raise ValueError("a unit needs an in-place rent, a market rent, or both")
        return self

    @property
    def current_rent(self) -> float:
        """Rent used for current cash flow: the lower of in-place and market."""
        return min(r for r in (self.in_place_rent, self.market_rent) if r is not None)

    @property
    def pro_forma_rent(self) -> float:
        """Market rent, or in-place rent when there is no market estimate."""
        if self.market_rent is not None:
            return self.market_rent
        assert self.in_place_rent is not None
        return self.in_place_rent


class DealInputs(Frozen):
    """A property and its operating assumptions, before financing."""

    price: float = Field(gt=0)
    units: tuple[Unit, ...] = Field(min_length=1)
    building_sqft: float | None = Field(default=None, gt=0)

    vacancy_rate: float = Field(ge=0, lt=1)
    management_pct: float = Field(ge=0, le=1)  # share of EGI
    repairs_pct: float = Field(ge=0, le=1)  # share of GSR
    reserves_pct: float = Field(ge=0, le=1)  # share of GSR
    utilities_per_unit_month: float = Field(ge=0)  # paid by the owner
    insurance_per_unit_year: float = Field(ge=0)  # hazard, after any risk multiplier
    flood_insurance_year: float = Field(default=0.0, ge=0)
    tax_rate: float = Field(ge=0, le=1)  # share of price
    tax_floor: float = Field(default=0.0, ge=0)  # last tax bill; taxes never go below it
    hoa_month: float = Field(default=0.0, ge=0)
    misc_per_unit_year: float = Field(ge=0)

    closing_cost_pct: float = Field(ge=0, le=1)
    rehab: float = Field(default=0.0, ge=0)
    credits: float = Field(default=0.0, ge=0)

    @property
    def unit_count(self) -> int:
        return len(self.units)

    def replace(self, **changes: Any) -> DealInputs:
        """Copy with some fields changed. Skips validation, so pass values already in range."""
        return self.model_copy(update=changes)


class MiTier(Frozen):
    """One row of an annual mortgage-insurance table.

    The first tier whose ``max_base_loan`` is None or at least the base loan applies. Within it
    the rate depends on whether the loan-to-value is above ``ltv_above``.
    """

    max_base_loan: float | None = None
    ltv_above: float
    rate_above: float
    rate_at_or_below: float


class LoanTerms(Frozen):
    """One financing scenario with its interest rate already decided."""

    name: str
    label: str = ""
    program: Literal["fha", "conventional", "dscr", "commercial", "cash"]
    owner_occupied: bool = False

    down_payment_pct: float = Field(default=0.0, ge=0, le=1)
    annual_rate: float = Field(default=0.0, ge=0)
    term_years: int = Field(default=30, gt=0)
    points_pct: float = Field(default=0.0, ge=0)  # share of the loan amount

    upfront_mi_pct: float = Field(default=0.0, ge=0)  # share of the base loan, financed
    annual_mi: tuple[MiTier, ...] = ()  # charged monthly on the base loan

    max_loan: float | None = None  # cap on the base loan (county or national limit)
    self_sufficiency_factor: float | None = None  # FHA: share of market rent that must cover PITI
    self_sufficiency_units: tuple[int, ...] = (3, 4)
    min_lender_dscr: float | None = None
    best_lender_dscr: float | None = None
    reserves_months: float = Field(default=0.0, ge=0)

    @property
    def is_cash(self) -> bool:
        return self.program == "cash"


class Targets(Frozen):
    """Buy-box targets. None switches a target off."""

    min_cash_flow_per_door_month: float | None
    min_coc: float | None
    min_dscr: float | None

    @property
    def any_set(self) -> bool:
        return any(
            t is not None for t in (self.min_cash_flow_per_door_month, self.min_coc, self.min_dscr)
        )


class HoldAssumptions(Frozen):
    """Hold-period model: growth, appreciation and sale costs."""

    years: int = Field(gt=0)
    rent_growth: float  # per year, from year 2
    expense_growth: float  # per year on dollar-based expenses, from year 2
    appreciation: float  # per year on the purchase price
    selling_cost_pct: float = Field(ge=0, lt=1)


class Shock(Frozen):
    """One sensitivity case. Deltas are added, factors are multiplied."""

    name: str
    rate_delta: float = 0.0
    rent_factor: float = Field(default=1.0, ge=0)
    vacancy_delta: float = 0.0
    insurance_factor: float = Field(default=1.0, ge=0)
    tax_factor: float = Field(default=1.0, ge=0)
