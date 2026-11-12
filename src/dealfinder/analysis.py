"""Assemble one deal from what is known, fill the gaps from settings, and run every loan preset.

Each input records where it came from and whether it is an actual figure, an estimate, or the
user's own override. An unknown input takes the pessimistic stand-in from settings and raises a
flag, so nothing optimistic slips in unannounced.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dealfinder.geo import Place, lookup_zip
from dealfinder.settings import Config, Preset
from dealfinder.underwrite import (
    DealInputs,
    LoanTerms,
    OperatingStatement,
    ScenarioResult,
    Unit,
    operating_statement,
    run_scenario,
)

DISCLAIMER = "Estimates only. Verify rents, taxes, insurance and financing before you offer."

Kind = Literal["actual", "estimated", "override"]
ValueUnit = Literal["money", "money_month", "money_year", "percent", "number", "text"]


class AnalysisError(Exception):
    """The request cannot be analyzed as given."""


class InputLine(BaseModel):
    """One input as shown to the user: its value, what kind of number it is, and its source."""

    key: str
    label: str
    value: float | str | None
    unit: ValueUnit
    kind: Kind
    source: str
    flagged: bool = False


class Flag(BaseModel):
    code: str
    level: Literal["info", "warning", "red"]
    message: str


class RateInfo(BaseModel):
    rate: float
    source: str
    flagged: bool = False


class AnalyzeRequest(BaseModel):
    """What the user knows about a deal. Anything left as None is filled from settings."""

    model_config = ConfigDict(extra="forbid")

    price: float = Field(gt=0)
    rents: list[float] = Field(min_length=1)  # current monthly rent, one per unit
    zip_code: str
    market_rents: list[float] | None = None
    year_built: int | None = None
    building_sqft: float | None = Field(default=None, gt=0)
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    tax_bill: float | None = Field(default=None, ge=0)  # last annual bill; taxes never go below
    vacancy_rate: float | None = Field(default=None, ge=0, lt=1)
    management: Literal["self", "manager"] | None = None
    insurance_per_unit_year: float | None = Field(default=None, ge=0)
    utilities_per_unit_month: float | None = Field(default=None, ge=0)
    tenants_pay_utilities: bool | None = None
    hoa_month: float = Field(default=0.0, ge=0)
    in_flood_zone: bool | None = None
    flood_insurance_year: float | None = Field(default=None, ge=0)
    rehab: float | None = Field(default=None, ge=0)
    credits: float = Field(default=0.0, ge=0)
    closing_cost_pct: float | None = Field(default=None, ge=0, le=1)
    base_rate: float | None = Field(default=None, ge=0, le=1)  # before each loan's spread
    rate_quote: float | None = Field(default=None, ge=0, le=1)  # final rate for the loans shown
    scenarios: list[str] | None = None  # None runs every enabled preset
    owner_unit: int | None = Field(default=None, ge=1)  # 1-based, for owner-occupied loans
    comparison_rent: float | None = Field(default=None, ge=0)  # what renting instead costs

    @model_validator(mode="after")
    def _consistent(self) -> AnalyzeRequest:
        if any(rent < 0 for rent in self.rents):
            raise ValueError("rents cannot be negative")
        if self.market_rents is not None:
            if len(self.market_rents) != len(self.rents):
                raise ValueError(
                    f"{len(self.market_rents)} market rents were given for {len(self.rents)} units"
                )
            if any(rent < 0 for rent in self.market_rents):
                raise ValueError("market rents cannot be negative")
        if self.owner_unit is not None and self.owner_unit > len(self.rents):
            raise ValueError(f"owner unit {self.owner_unit} is not one of {len(self.rents)} units")
        return self


class AnalysisResult(BaseModel):
    request: AnalyzeRequest
    place: Place | None
    deal: DealInputs
    statement: OperatingStatement
    inputs: list[InputLine]
    flags: list[Flag]
    scenarios: list[ScenarioResult]
    rates: dict[str, RateInfo]
    skipped: dict[str, str]
    ranking_scenario: str
    disclaimer: str = DISCLAIMER


class _Assembly:
    """Collects the input lines and flags while a deal is put together."""

    def __init__(self) -> None:
        self.lines: list[InputLine] = []
        self.flags: list[Flag] = []

    def line(
        self,
        key: str,
        label: str,
        value: float | str | None,
        unit: ValueUnit,
        kind: Kind,
        source: str,
        *,
        flagged: bool = False,
    ) -> None:
        self.lines.append(
            InputLine(
                key=key,
                label=label,
                value=value,
                unit=unit,
                kind=kind,
                source=source,
                flagged=flagged,
            )
        )

    def flag(self, code: str, level: Literal["info", "warning", "red"], message: str) -> None:
        self.flags.append(Flag(code=code, level=level, message=message))


def _money_list(values: list[float]) -> str:
    return ", ".join(f"${value:,.0f}" for value in values)


def _locate(request: AnalyzeRequest, out: _Assembly) -> Place | None:
    place = lookup_zip(request.zip_code)
    if place is None:
        out.flag(
            "zip_invalid",
            "warning",
            f"'{request.zip_code}' is not a 5-digit ZIP code, so no location data was used.",
        )
    elif place.state is None:
        out.flag(
            "zip_unknown",
            "warning",
            f"ZIP {place.zip_code} is not in the Census ZIP table, so no location data was used.",
        )
    else:
        if place.approximate:
            out.flag(
                "zip_approximate",
                "info",
                f"ZIP {place.zip_code} is not in the Census ZIP table; {place.state} was taken "
                "from its first three digits and the county is unknown.",
            )
        if place.in_contiguous_us is False:
            out.flag(
                "outside_coverage",
                "warning",
                f"{place.state} is outside the 48 contiguous states and DC that this tool covers.",
            )
    return place


def _units(request: AnalyzeRequest, out: _Assembly) -> tuple[Unit, ...]:
    market = request.market_rents
    units = tuple(
        Unit(in_place_rent=rent, market_rent=None if market is None else market[index])
        for index, rent in enumerate(request.rents)
    )
    out.line(
        "rents",
        "Current rents",
        sum(request.rents),
        "money_month",
        "actual",
        f"entered: {_money_list(request.rents)}",
    )
    if market is None:
        out.flag(
            "market_rent_unknown",
            "info",
            "No market rents were given, so the pro forma equals current rents.",
        )
    else:
        out.line(
            "market_rents",
            "Market rents",
            sum(market),
            "money_month",
            "estimated",
            f"entered: {_money_list(market)}",
        )
        above = [str(i + 1) for i, unit in enumerate(units) if unit.current_rent < request.rents[i]]
        if above:
            out.flag(
                "rent_above_market",
                "info",
                f"In-place rent is above market for unit {', '.join(above)}; cash flow uses "
                "the lower market figure.",
            )
    return units


def assemble_deal(
    request: AnalyzeRequest, config: Config
) -> tuple[DealInputs, list[InputLine], list[Flag], Place | None]:
    """Build engine inputs plus the list of where each one came from."""
    settings = config.settings
    underwriting = settings.underwriting
    unknown = settings.when_unknown
    out = _Assembly()

    out.line("price", "Price", request.price, "money", "actual", "entered")
    place = _locate(request, out)
    units = _units(request, out)
    unit_count = len(units)

    # Year built decides the repairs and reserves band.
    if request.year_built is None:
        year_built = unknown.year_built
        out.line(
            "year_built",
            "Year built",
            "unknown",
            "text",
            "estimated",
            "oldest band assumed",
            flagged=True,
        )
        out.flag(
            "year_built_unknown",
            "warning",
            "Year built is unknown, so repairs and reserves use the oldest band.",
        )
    else:
        year_built = request.year_built
        out.line("year_built", "Year built", year_built, "number", "actual", "entered")
    band = underwriting.age_band(year_built)
    band_name = "newest band" if band.built_before is None else f"built before {band.built_before}"
    for key, label, value in (
        ("repairs_pct", "Repairs", band.repairs_pct),
        ("reserves_pct", "Capital reserves", band.reserves_pct),
    ):
        out.line(
            key,
            label,
            value,
            "percent",
            "estimated",
            f"share of GSR, {band_name}",
            flagged=request.year_built is None,
        )

    # Vacancy
    if request.vacancy_rate is not None:
        vacancy = request.vacancy_rate
        out.line("vacancy_rate", "Vacancy", vacancy, "percent", "override", "entered")
    else:
        vacancy = unknown.vacancy_rate
        out.line(
            "vacancy_rate",
            "Vacancy",
            vacancy,
            "percent",
            "estimated",
            "county vacancy not loaded: pessimistic stand-in",
            flagged=True,
        )
        out.flag(
            "vacancy_unknown",
            "warning",
            f"County vacancy is not loaded, so vacancy uses the stand-in of {vacancy:.0%}.",
        )

    # Management
    mode = request.management or underwriting.management.mode
    management_pct = underwriting.management.pct_for(mode)
    out.line(
        "management_pct",
        "Management",
        management_pct,
        "percent",
        "override" if request.management else "estimated",
        ("self-managed" if mode == "self" else "property manager")
        + (", entered" if request.management else ", from settings"),
    )

    # Utilities
    if request.utilities_per_unit_month is not None:
        utilities = request.utilities_per_unit_month
        out.line(
            "utilities",
            "Owner-paid utilities",
            utilities,
            "money_month",
            "override",
            "entered, per unit",
        )
    else:
        if request.tenants_pay_utilities is None:
            paid_by = underwriting.utilities.paid_by
            said = "from settings"
        else:
            paid_by = "tenant" if request.tenants_pay_utilities else "owner"
            said = "entered"
        kind: Kind = "estimated" if request.tenants_pay_utilities is None else "override"
        if paid_by == "tenant":
            utilities = 0.0
            out.line(
                "utilities",
                "Owner-paid utilities",
                utilities,
                "money_month",
                kind,
                f"tenants pay, {said}",
            )
        else:
            utilities = underwriting.utilities.owner_cost_per_unit_month
            is_unknown = paid_by == "unknown"
            out.line(
                "utilities",
                "Owner-paid utilities",
                utilities,
                "money_month",
                kind,
                ("unknown who pays: owner charged" if is_unknown else f"owner pays, {said}")
                + ", per unit",
                flagged=is_unknown,
            )
            if is_unknown:
                out.flag(
                    "utilities_unknown",
                    "warning",
                    "Who pays utilities is unknown, so the owner is charged "
                    f"${utilities:,.0f} per unit per month.",
                )

    # Hazard insurance
    if request.insurance_per_unit_year is not None:
        insurance = request.insurance_per_unit_year
        out.line("insurance", "Insurance", insurance, "money_year", "override", "entered, per unit")
    else:
        state = place.state if place else None
        base = config.insurance_by_state.get(state or "", underwriting.insurance.per_unit_year)
        multiplier = (
            underwriting.insurance.high_risk_multiplier if unknown.high_hazard_county else 1
        )
        insurance = base * multiplier
        source = f"placeholder for {state}" if state else "placeholder, state unknown"
        if multiplier != 1:
            source += f" x {multiplier:g}, county hazard rating not loaded"
        out.line(
            "insurance",
            "Insurance",
            insurance,
            "money_year",
            "estimated",
            source + ", per unit",
            flagged=True,
        )
        out.flag(
            "insurance_placeholder",
            "warning",
            f"Insurance is a placeholder of ${insurance:,.0f} per unit per year, not a quote.",
        )

    # Flood insurance
    flood_placeholder = underwriting.flood_insurance_per_unit_year * unit_count
    if request.flood_insurance_year is not None:
        flood = request.flood_insurance_year
        out.line("flood_insurance", "Flood insurance", flood, "money_year", "override", "entered")
    elif request.in_flood_zone:
        flood = flood_placeholder
        out.line(
            "flood_insurance",
            "Flood insurance",
            flood,
            "money_year",
            "estimated",
            "placeholder: in a flood zone",
            flagged=True,
        )
        out.flag(
            "flood_zone",
            "red",
            f"In a flood zone: flood insurance added at a placeholder ${flood:,.0f} a year.",
        )
    elif request.in_flood_zone is None and unknown.in_flood_zone:
        flood = flood_placeholder
        out.line(
            "flood_insurance",
            "Flood insurance",
            flood,
            "money_year",
            "estimated",
            "flood zone not checked: charged anyway",
            flagged=True,
        )
        out.flag(
            "flood_unknown_charged",
            "warning",
            f"Flood zone not checked, so a placeholder ${flood:,.0f} a year is charged.",
        )
    else:
        flood = 0.0
        if request.in_flood_zone is None:
            out.flag(
                "flood_not_checked",
                "info",
                "Flood zone not checked; no flood insurance is included.",
            )

    # Property taxes
    if request.tax_rate is not None:
        tax_rate = request.tax_rate
        out.line("tax_rate", "Property tax rate", tax_rate, "percent", "override", "entered")
    else:
        tax_rate = unknown.tax_rate
        out.line(
            "tax_rate",
            "Property tax rate",
            tax_rate,
            "percent",
            "estimated",
            "county rate not loaded: pessimistic stand-in",
            flagged=True,
        )
        out.flag(
            "tax_rate_unknown",
            "warning",
            f"The county tax rate is not loaded, so taxes use the stand-in of {tax_rate:.2%} "
            "of price.",
        )
    if request.tax_bill is not None:
        out.line(
            "tax_bill",
            "Last tax bill",
            request.tax_bill,
            "money_year",
            "actual",
            "entered; taxes never go below it",
        )

    if request.hoa_month:
        out.line("hoa", "HOA dues", request.hoa_month, "money_month", "actual", "entered")
    out.line(
        "misc",
        "Misc",
        underwriting.misc_per_unit_year,
        "money_year",
        "estimated",
        "default, per unit",
    )

    closing = request.closing_cost_pct
    out.line(
        "closing_cost_pct",
        "Closing costs",
        underwriting.closing_cost_pct if closing is None else closing,
        "percent",
        "estimated" if closing is None else "override",
        "default, share of price" if closing is None else "entered",
    )
    rehab = underwriting.rehab_per_unit.get("none", 0.0) * unit_count
    if request.rehab is not None:
        rehab = request.rehab
        out.line("rehab", "Rehab", rehab, "money", "override", "entered")
    if request.credits:
        out.line("credits", "Seller credits", request.credits, "money", "actual", "entered")

    deal = DealInputs(
        price=request.price,
        units=units,
        building_sqft=request.building_sqft,
        vacancy_rate=vacancy,
        management_pct=management_pct,
        repairs_pct=band.repairs_pct,
        reserves_pct=band.reserves_pct,
        utilities_per_unit_month=utilities,
        insurance_per_unit_year=insurance,
        flood_insurance_year=flood,
        tax_rate=tax_rate,
        tax_floor=request.tax_bill or 0.0,
        hoa_month=request.hoa_month,
        misc_per_unit_year=underwriting.misc_per_unit_year,
        closing_cost_pct=underwriting.closing_cost_pct if closing is None else closing,
        rehab=rehab,
        credits=request.credits,
    )
    if mode == "self" and underwriting.management.manager_pct > management_pct:
        egi = operating_statement(deal).egi
        out.flag(
            "self_managed",
            "info",
            "Self-managed. A property manager at "
            f"{underwriting.management.manager_pct:.0%} of EGI would cost about "
            f"${underwriting.management.manager_pct * egi:,.0f} a year.",
        )
    return deal, out.lines, out.flags, place


def _terms(name: str, preset: Preset, rate: float, max_loan: float | None) -> LoanTerms:
    rule = preset.self_sufficiency
    return LoanTerms(
        name=name,
        label=preset.label or name,
        program=preset.program,
        owner_occupied=preset.owner_occupied,
        down_payment_pct=preset.down_payment_pct,
        annual_rate=rate,
        term_years=preset.term_years,
        points_pct=preset.points_pct,
        upfront_mi_pct=preset.upfront_mi_pct,
        annual_mi=tuple(preset.annual_mi),
        max_loan=max_loan,
        self_sufficiency_factor=rule.factor if rule else None,
        self_sufficiency_units=tuple(rule.units) if rule else (),
        min_lender_dscr=preset.lender_dscr.min if preset.lender_dscr else None,
        best_lender_dscr=preset.lender_dscr.best if preset.lender_dscr else None,
        reserves_months=preset.reserves_months,
    )


def resolve_terms(
    request: AnalyzeRequest, config: Config, unit_count: int, county_fips: str | None = None
) -> tuple[list[LoanTerms], dict[str, RateInfo], dict[str, str], set[str]]:
    """Pick the presets to run and decide each one's rate and loan limit.

    A rate is, in order: the quote entered with the request, the preset's own quote from
    settings, or the base rate plus the preset's spread. The base rate is the one entered, the
    manual one from settings, or the stand-in, which is flagged.

    The last item returned names the presets whose limit is a national minimum because the
    county is unknown or missing from the table.
    """
    settings = config.settings
    presets = settings.financing.presets
    requested = request.scenarios
    if requested:
        unknown_names = [name for name in requested if name not in presets]
        if unknown_names:
            raise AnalysisError(
                f"Unknown scenario {', '.join(unknown_names)}. Choose from: {', '.join(presets)}."
            )

    if request.base_rate is not None:
        base, base_source, base_flagged = request.base_rate, "base rate entered", False
    elif settings.financing.base_rate_manual is not None:
        base, base_source, base_flagged = (
            settings.financing.base_rate_manual,
            "base rate from settings",
            False,
        )
    else:
        base, base_source, base_flagged = (
            settings.when_unknown.base_rate,
            "stand-in base rate",
            True,
        )

    terms: list[LoanTerms] = []
    rates: dict[str, RateInfo] = {}
    skipped: dict[str, str] = {}
    national_limit: set[str] = set()
    for name, preset in presets.items():
        if requested and name not in requested:
            continue
        if not preset.enabled and not requested:
            skipped[name] = "switched off in settings"
            continue
        if not preset.fits(unit_count):
            noun = "unit" if unit_count == 1 else "units"
            skipped[name] = f"not offered for {unit_count} {noun}"
            continue
        if preset.program == "cash":
            info = RateInfo(rate=0.0, source="no loan")
        elif request.rate_quote is not None:
            info = RateInfo(rate=request.rate_quote, source="your quote, entered")
        elif preset.rate_quote is not None:
            info = RateInfo(rate=preset.rate_quote, source="your quote, from settings")
        else:
            info = RateInfo(
                rate=base + preset.rate_spread,
                source=f"{base_source} {base:.2%} + {preset.rate_spread * 100:.2f} pt spread",
                flagged=base_flagged,
            )
        rates[name] = info
        limit = config.loan_limits.lookup(preset.loan_limit, unit_count, county_fips)
        if not limit.county_specific:
            national_limit.add(name)
        terms.append(_terms(name, preset, info.rate, limit.amount))
    return terms, rates, skipped, national_limit


def analyze(request: AnalyzeRequest, config: Config) -> AnalysisResult:
    """Underwrite one deal under every applicable loan preset."""
    deal, lines, flags, place = assemble_deal(request, config)
    terms_list, rates, skipped, national_limit = resolve_terms(
        request, config, deal.unit_count, place.county_fips if place else None
    )
    settings = config.settings

    scenarios = [
        run_scenario(
            deal,
            terms,
            targets=settings.buy_box.targets,
            hold=settings.hold,
            shocks=settings.sensitivity,
            manager_pct=settings.underwriting.management.manager_pct,
            owner_unit=None if request.owner_unit is None else request.owner_unit - 1,
            comparison_rent=request.comparison_rent,
        )
        for terms in terms_list
    ]

    if any(info.flagged for info in rates.values()):
        flags.append(
            Flag(
                code="rate_stand_in",
                level="warning",
                message=(
                    "No live mortgage rate is connected, so loans use the stand-in base rate of "
                    f"{settings.when_unknown.base_rate:.2%} plus each loan's spread."
                ),
            )
        )
    if any(s.loan.capped_by_limit and s.terms.name in national_limit for s in scenarios):
        flags.append(
            Flag(
                code="loan_limit_national",
                level="info",
                message=(
                    "This county is not in the loan-limit table, so the lowest national limit "
                    "was used. The county's own limit may be higher."
                ),
            )
        )

    return AnalysisResult(
        request=request,
        place=place,
        deal=deal,
        statement=operating_statement(deal),
        inputs=lines,
        flags=flags,
        scenarios=scenarios,
        rates=rates,
        skipped=skipped,
        ranking_scenario=settings.buy_box.ranking_scenario,
    )
