"""Assemble one deal from what is known, fill the gaps, and run every loan preset.

Gaps are filled in this order: the user's own figure, market data for the ZIP when a
``MarketContext`` is supplied, then the pessimistic stand-in from settings, which raises a flag.
Each input records where it came from and whether it is an actual figure, an estimate, or the
user's own override.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dealfinder.geo import Place, lookup_zip
from dealfinder.market.context import MarketContext
from dealfinder.settings import Config, Preset
from dealfinder.text import money_list, short_date
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
PaidBy = Literal["owner", "tenant", "unknown"]


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
    """What the user knows about a deal. Anything left as None is filled from market data or
    settings."""

    model_config = ConfigDict(extra="forbid")

    price: float = Field(gt=0)
    rents: list[float] = Field(min_length=1)  # current monthly rent, one per unit
    zip_code: str
    market_rents: list[float] | None = None
    beds: list[float] | None = None  # bedrooms per unit, used to look up market rents
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
        units = len(self.rents)
        if any(rent < 0 for rent in self.rents):
            raise ValueError("rents cannot be negative")
        if self.market_rents is not None:
            if len(self.market_rents) != units:
                raise ValueError(
                    f"{len(self.market_rents)} market rents were given for {units} units"
                )
            if any(rent < 0 for rent in self.market_rents):
                raise ValueError("market rents cannot be negative")
        if self.beds is not None:
            if len(self.beds) != units:
                raise ValueError(f"{len(self.beds)} bedroom counts were given for {units} units")
            if any(count < 0 for count in self.beds):
                raise ValueError("bedroom counts cannot be negative")
        if self.owner_unit is not None and self.owner_unit > units:
            raise ValueError(f"owner unit {self.owner_unit} is not one of {units} units")
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
    market: MarketContext | None = None
    attributions: list[str] = Field(default_factory=list)
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


def _who_pays_utilities(request: AnalyzeRequest, config: Config) -> tuple[PaidBy, bool]:
    """(who pays, whether the user said so)."""
    if request.tenants_pay_utilities is None:
        return config.settings.underwriting.utilities.paid_by, False
    return ("tenant" if request.tenants_pay_utilities else "owner"), True


def _fmr_rents(
    request: AnalyzeRequest, config: Config, market: MarketContext | None, paid_by: PaidBy
) -> tuple[list[float], str] | None:
    """Market rent per unit from HUD Fair Market Rents, when they and bedroom counts are known.

    FMRs are gross rents that include tenant-paid utilities, so a utility allowance comes off
    unless the owner is known to pay the utilities.
    """
    rents = market.fair_market_rents if market else None
    if rents is None or request.beds is None:
        return None
    extra = config.settings.market.fmr_extra_bedroom_pct
    net_of_utilities = paid_by != "owner" and bool(config.utility_allowance)
    estimates = [
        max(
            0.0,
            rents.gross_rent(beds, extra)
            - (config.utility_allowance_for(beds) if net_of_utilities else 0.0),
        )
        for beds in request.beds
    ]
    suffix = ", less a placeholder utility allowance" if net_of_utilities else ""
    return estimates, f"{rents.describe()}{suffix}"


def _units(
    request: AnalyzeRequest,
    config: Config,
    market: MarketContext | None,
    paid_by: PaidBy,
    out: _Assembly,
) -> tuple[Unit, ...]:
    out.line(
        "rents",
        "Current rents",
        sum(request.rents),
        "money_month",
        "actual",
        f"entered: {money_list(request.rents)}",
    )
    market_rents = request.market_rents
    if market_rents is not None:
        out.line(
            "market_rents",
            "Market rents",
            sum(market_rents),
            "money_month",
            "estimated",
            f"entered: {money_list(market_rents)}",
        )
    else:
        estimated = _fmr_rents(request, config, market, paid_by)
        if estimated is not None:
            market_rents, source = estimated
            stale = bool(market and market.fair_market_rents and market.fair_market_rents.stale)
            out.line(
                "market_rents",
                "Market rents",
                sum(market_rents),
                "money_month",
                "estimated",
                f"{source}: {money_list(market_rents)}",
                flagged=stale,
            )
        else:
            have_rents = market is not None and market.fair_market_rents is not None
            out.flag(
                "market_rent_unknown",
                "info",
                "No market rents were given"
                + (
                    " and no bedroom counts to look them up with"
                    if have_rents and request.beds is None
                    else ""
                )
                + ", so the pro forma equals current rents.",
            )

    beds = request.beds
    units = tuple(
        Unit(
            in_place_rent=rent,
            market_rent=None if market_rents is None else market_rents[index],
            beds=None if beds is None else beds[index],
        )
        for index, rent in enumerate(request.rents)
    )
    above = [str(i + 1) for i, unit in enumerate(units) if unit.current_rent < request.rents[i]]
    if above:
        out.flag(
            "rent_above_market",
            "info",
            f"In-place rent is above market for unit {', '.join(above)}; cash flow uses the "
            "lower market figure.",
        )
    return units


def _age_band(request: AnalyzeRequest, config: Config, out: _Assembly) -> tuple[float, float]:
    """Repairs and reserves shares, chosen by year built."""
    known = request.year_built is not None
    if request.year_built is None:
        year_built = config.settings.when_unknown.year_built
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
    band = config.settings.underwriting.age_band(year_built)
    name = "newest band" if band.built_before is None else f"built before {band.built_before}"
    for key, label, value in (
        ("repairs_pct", "Repairs", band.repairs_pct),
        ("reserves_pct", "Capital reserves", band.reserves_pct),
    ):
        out.line(
            key, label, value, "percent", "estimated", f"share of GSR, {name}", flagged=not known
        )
    return band.repairs_pct, band.reserves_pct


def _vacancy(
    request: AnalyzeRequest, config: Config, market: MarketContext | None, out: _Assembly
) -> float:
    if request.vacancy_rate is not None:
        out.line("vacancy_rate", "Vacancy", request.vacancy_rate, "percent", "override", "entered")
        return request.vacancy_rate
    housing = market.housing if market else None
    rule = config.settings.underwriting.vacancy
    if housing is not None and housing.rental_vacancy is not None:
        vacancy = min(0.99, max(rule.floor, housing.rental_vacancy + rule.county_add))
        out.line(
            "vacancy_rate",
            "Vacancy",
            vacancy,
            "percent",
            "estimated",
            f"{housing.vintage}: county rental vacancy {housing.rental_vacancy:.1%} "
            f"+ {rule.county_add * 100:g} pt, floor {rule.floor:.0%}",
            flagged=housing.stale,
        )
        return vacancy
    vacancy = config.settings.when_unknown.vacancy_rate
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
    return vacancy


def _utilities(
    request: AnalyzeRequest, config: Config, paid_by: PaidBy, said: bool, out: _Assembly
) -> float:
    label = "Owner-paid utilities"
    if request.utilities_per_unit_month is not None:
        out.line(
            "utilities",
            label,
            request.utilities_per_unit_month,
            "money_month",
            "override",
            "entered, per unit",
        )
        return request.utilities_per_unit_month
    kind: Kind = "override" if said else "estimated"
    origin = "entered" if said else "from settings"
    if paid_by == "tenant":
        out.line("utilities", label, 0.0, "money_month", kind, f"tenants pay, {origin}")
        return 0.0
    cost = config.settings.underwriting.utilities.owner_cost_per_unit_month
    unknown = paid_by == "unknown"
    out.line(
        "utilities",
        label,
        cost,
        "money_month",
        kind,
        ("unknown who pays: owner charged" if unknown else f"owner pays, {origin}") + ", per unit",
        flagged=unknown,
    )
    if unknown:
        out.flag(
            "utilities_unknown",
            "warning",
            f"Who pays utilities is unknown, so the owner is charged ${cost:,.0f} per unit per "
            "month.",
        )
    return cost


def _insurance(
    request: AnalyzeRequest,
    config: Config,
    market: MarketContext | None,
    place: Place | None,
    out: _Assembly,
) -> float:
    if request.insurance_per_unit_year is not None:
        out.line(
            "insurance",
            "Insurance",
            request.insurance_per_unit_year,
            "money_year",
            "override",
            "entered, per unit",
        )
        return request.insurance_per_unit_year

    rule = config.settings.underwriting.insurance
    state = place.state if place else None
    base = config.insurance_by_state.get(state or "", rule.per_unit_year)
    source = f"placeholder for {state}" if state else "placeholder, state unknown"
    risk = market.risk if market else None
    if risk is not None:
        high = risk.rating in config.settings.market.high_risk_ratings
        multiplier = rule.high_risk_multiplier if high else 1.0
        source += (f" x {multiplier:g}" if high else "") + f", hazard rating {risk.rating}"
        if high:
            out.flag(
                "high_hazard",
                "warning",
                f"FEMA rates this county's natural-hazard risk {risk.rating}, so insurance is "
                f"raised {multiplier:g}x.",
            )
    elif config.settings.when_unknown.high_hazard_county:
        multiplier = rule.high_risk_multiplier
        source += f" x {multiplier:g}, county hazard rating not loaded"
    else:
        multiplier = 1.0
    insurance = base * multiplier
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
    return insurance


def _flood(request: AnalyzeRequest, config: Config, unit_count: int, out: _Assembly) -> float:
    placeholder = config.settings.underwriting.flood_insurance_per_unit_year * unit_count
    if request.flood_insurance_year is not None:
        out.line(
            "flood_insurance",
            "Flood insurance",
            request.flood_insurance_year,
            "money_year",
            "override",
            "entered",
        )
        return request.flood_insurance_year
    if request.in_flood_zone:
        out.line(
            "flood_insurance",
            "Flood insurance",
            placeholder,
            "money_year",
            "estimated",
            "placeholder: in a flood zone",
            flagged=True,
        )
        out.flag(
            "flood_zone",
            "red",
            f"In a flood zone: flood insurance added at a placeholder ${placeholder:,.0f} a year.",
        )
        return placeholder
    if request.in_flood_zone is None and config.settings.when_unknown.in_flood_zone:
        out.line(
            "flood_insurance",
            "Flood insurance",
            placeholder,
            "money_year",
            "estimated",
            "flood zone not checked: charged anyway",
            flagged=True,
        )
        out.flag(
            "flood_unknown_charged",
            "warning",
            f"Flood zone not checked, so a placeholder ${placeholder:,.0f} a year is charged.",
        )
        return placeholder
    if request.in_flood_zone is None:
        out.flag(
            "flood_not_checked", "info", "Flood zone not checked; no flood insurance is included."
        )
    return 0.0


def _tax_rate(
    request: AnalyzeRequest, config: Config, market: MarketContext | None, out: _Assembly
) -> float:
    label = "Property tax rate"
    if request.tax_rate is not None:
        out.line("tax_rate", label, request.tax_rate, "percent", "override", "entered")
        return request.tax_rate
    housing = market.housing if market else None
    computed = housing.effective_tax_rate() if housing is not None else None
    if housing is not None and computed is not None:
        rate, method = computed
        out.line(
            "tax_rate",
            label,
            rate,
            "percent",
            "estimated",
            f"{housing.vintage}, {housing.name}: {method}",
            flagged=housing.stale,
        )
        if housing.top_coded:
            out.flag(
                "tax_rate_top_coded",
                "info",
                "The Census caps this county's median tax figure, so the rate uses total taxes "
                "over total home value.",
            )
        return rate
    rate = config.settings.when_unknown.tax_rate
    out.line(
        "tax_rate",
        label,
        rate,
        "percent",
        "estimated",
        "county rate not loaded: pessimistic stand-in",
        flagged=True,
    )
    out.flag(
        "tax_rate_unknown",
        "warning",
        f"The county tax rate is not loaded, so taxes use the stand-in of {rate:.2%} of price.",
    )
    return rate


def _market_notes(
    units: tuple[Unit, ...], config: Config, market: MarketContext | None, out: _Assembly
) -> None:
    """Flags about the market data itself: a rent sanity check, sources that are missing or
    served from an expired cache."""
    if market is None:
        return
    index = market.rent_index
    if index is not None:
        average = sum(unit.pro_forma_rent for unit in units) / len(units)
        if average > index.level * (1 + config.settings.market.rent_check_tolerance):
            out.flag(
                "rent_above_index",
                "warning",
                f"Rents average ${average:,.0f} a unit, {average / index.level - 1:.0%} above "
                f"Zillow's typical rent of ${index.level:,.0f} for this {index.geography}. "
                "Check them against real comparables.",
            )
    missing = market.missing()
    if missing:
        out.flag(
            "market_data_missing",
            "info",
            "Not connected: " + "; ".join(f"{s.label} ({s.detail})" for s in missing) + ".",
        )
    stale = [status.label for status in market.sources if status.state == "stale"]
    if stale:
        out.flag(
            "market_data_stale",
            "warning",
            f"Using an older cached copy because the refresh failed: {', '.join(stale)}.",
        )


def assemble_deal(
    request: AnalyzeRequest, config: Config, market: MarketContext | None = None
) -> tuple[DealInputs, list[InputLine], list[Flag], Place | None]:
    """Build engine inputs plus the list of where each one came from."""
    underwriting = config.settings.underwriting
    out = _Assembly()

    out.line("price", "Price", request.price, "money", "actual", "entered")
    place = _locate(request, out)
    paid_by, said = _who_pays_utilities(request, config)
    units = _units(request, config, market, paid_by, out)
    unit_count = len(units)
    repairs_pct, reserves_pct = _age_band(request, config, out)
    vacancy = _vacancy(request, config, market, out)

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

    utilities = _utilities(request, config, paid_by, said, out)
    insurance = _insurance(request, config, market, place, out)
    flood = _flood(request, config, unit_count, out)
    tax_rate = _tax_rate(request, config, market, out)
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
    closing_pct = underwriting.closing_cost_pct if closing is None else closing
    out.line(
        "closing_cost_pct",
        "Closing costs",
        closing_pct,
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
        repairs_pct=repairs_pct,
        reserves_pct=reserves_pct,
        utilities_per_unit_month=utilities,
        insurance_per_unit_year=insurance,
        flood_insurance_year=flood,
        tax_rate=tax_rate,
        tax_floor=request.tax_bill or 0.0,
        hoa_month=request.hoa_month,
        misc_per_unit_year=underwriting.misc_per_unit_year,
        closing_cost_pct=closing_pct,
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
    _market_notes(units, config, market, out)
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


def base_rate(
    request: AnalyzeRequest, config: Config, market: MarketContext | None
) -> tuple[float, str, bool]:
    """(rate, where it came from, whether it should be flagged).

    In order: the rate entered with the request, the manual rate in settings, the live 30-year
    average, then the stand-in.
    """
    settings = config.settings
    if request.base_rate is not None:
        return request.base_rate, "base rate entered", False
    if settings.financing.base_rate_manual is not None:
        return settings.financing.base_rate_manual, "base rate from settings", False
    live = market.mortgage_rate if market else None
    if live is not None:
        return live.rate, f"{live.source}, week of {short_date(live.as_of)}", live.stale
    return settings.when_unknown.base_rate, "stand-in base rate", True


def resolve_terms(
    request: AnalyzeRequest,
    config: Config,
    unit_count: int,
    county_fips: str | None = None,
    market: MarketContext | None = None,
) -> tuple[list[LoanTerms], dict[str, RateInfo], dict[str, str], set[str]]:
    """Pick the presets to run and decide each one's rate and loan limit.

    A loan's rate is, in order: the quote entered with the request, the preset's own quote from
    settings, or the base rate plus the preset's spread.

    The last item returned names the presets whose limit is a national minimum because the
    county is unknown or missing from the table.
    """
    presets = config.settings.financing.presets
    requested = request.scenarios
    if requested:
        unknown_names = [name for name in requested if name not in presets]
        if unknown_names:
            raise AnalysisError(
                f"Unknown scenario {', '.join(unknown_names)}. Choose from: {', '.join(presets)}."
            )
    base, base_source, base_flagged = base_rate(request, config, market)

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


def analyze(
    request: AnalyzeRequest, config: Config, market: MarketContext | None = None
) -> AnalysisResult:
    """Underwrite one deal under every applicable loan preset.

    ``market`` is the market data for the deal's ZIP. Without it every gap falls back to the
    stand-ins in settings, which is what happens offline with an empty cache.
    """
    deal, lines, flags, place = assemble_deal(request, config, market)
    terms_list, rates, skipped, national_limit = resolve_terms(
        request, config, deal.unit_count, place.county_fips if place else None, market
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

    live_rate = market.mortgage_rate if market else None
    if any(info.flagged for info in rates.values()) and live_rate is None:
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
        market=market,
        attributions=market.attributions if market else [],
    )
