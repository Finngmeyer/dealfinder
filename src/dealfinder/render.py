"""Terminal output for an analysis."""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.console import Console
from rich.table import Table

from dealfinder.analysis import AnalysisResult, AnalyzeRequest, InputLine, base_rate
from dealfinder.market.context import MarketContext
from dealfinder.pipeline.sweep import SweepPlan
from dealfinder.settings import Config
from dealfinder.store.tables import ListingRow, RunRow
from dealfinder.store.usage import BudgetGuard
from dealfinder.text import short_date
from dealfinder.underwrite import ScenarioResult

NOT_AVAILABLE = "n/a"
_STATE_STYLE = {"ok": None, "stale": "yellow", "off": "dim", "unavailable": "yellow"}
_LEVEL_STYLE = {"info": "dim", "warning": "yellow", "red": "bold red"}
_LEVEL_MARK = {"info": "note", "warning": "check", "red": "RED FLAG"}
_KIND_LABEL = {"actual": "actual", "estimated": "estimate", "override": "yours"}


def money(value: float | None, *, cents: bool = False) -> str:
    if value is None:
        return NOT_AVAILABLE
    text = f"${abs(value):,.2f}" if cents else f"${abs(value):,.0f}"
    return f"-{text}" if value < 0 and text.strip("$0,.") else text


def percent(value: float | None, digits: int = 2) -> str:
    return NOT_AVAILABLE if value is None else f"{value * 100:.{digits}f}%"


def ratio(value: float | None, digits: int = 3) -> str:
    return NOT_AVAILABLE if value is None else f"{value:.{digits}f}"


def input_value(line: InputLine) -> str:
    value = line.value
    if value is None:
        return NOT_AVAILABLE
    if isinstance(value, str):
        return value
    match line.unit:
        case "money":
            return money(value)
        case "money_month":
            return f"{money(value)}/mo"
        case "money_year":
            return f"{money(value)}/yr"
        case "percent":
            return percent(value)
        case "number":
            return f"{value:g}"
    return str(value)


def _table(*columns: str, numeric_from: int = 1) -> Table:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, header_style="bold")
    for index, column in enumerate(columns):
        table.add_column(column, justify="right" if index >= numeric_from else "left")
    return table


def _header(result: AnalysisResult, console: Console) -> None:
    deal = result.deal
    units = deal.unit_count
    where = result.place.describe() if result.place else result.request.zip_code
    console.print(
        f"[bold]{money(deal.price)} | {units} {'unit' if units == 1 else 'units'} | {where}[/bold]"
    )
    first = result.scenarios[0].kpis if result.scenarios else None
    parts = [f"{money(deal.price / units)} per door"]
    if first is not None:
        if first.price_per_sqft is not None:
            parts.append(f"{money(first.price_per_sqft)} per sqft")
        parts.append(f"GRM {ratio(first.grm, 2)}")
        parts.append(f"rent-to-price {percent(first.rent_to_price)}")
        if first.rent_upside:
            parts.append(f"rent upside {percent(first.rent_upside, 1)}")
    console.print(" | ".join(parts), style="dim")


def _inputs(result: AnalysisResult, console: Console) -> None:
    table = _table("Input", "Value", "Type", "Source", numeric_from=99)
    table.columns[1].justify = "right"
    for line in result.inputs:
        style = "yellow" if line.flagged else None
        mark = " (!)" if line.flagged else ""
        table.add_row(
            line.label, input_value(line), _KIND_LABEL[line.kind] + mark, line.source, style=style
        )
    console.print(table)


def _statement(result: AnalysisResult, console: Console) -> None:
    statement = result.statement
    table = _table("Year 1, all units rented", "Per year", "Per month")

    def row(label: str, value: float, *, bold: bool = False, skip_zero: bool = False) -> None:
        if skip_zero and value == 0:
            return
        table.add_row(label, money(value), money(value / 12), style="bold" if bold else None)

    row("Gross scheduled rent (GSR)", statement.gsr)
    row("Vacancy", -statement.vacancy_loss)
    row("Effective gross income (EGI)", statement.egi, bold=True)
    row("Property taxes", statement.taxes)
    row("Insurance", statement.insurance)
    row("Flood insurance", statement.flood_insurance, skip_zero=True)
    row("Repairs", statement.repairs)
    row("Capital reserves", statement.reserves)
    row("Management", statement.management)
    row("Utilities", statement.utilities)
    row("HOA dues", statement.hoa, skip_zero=True)
    row("Misc", statement.misc)
    row("Operating expenses", statement.opex, bold=True)
    row("Net operating income (NOI)", statement.noi, bold=True)
    console.print(table)
    if result.scenarios:
        kpis = result.scenarios[0].kpis
        expense_ratio = statement.opex / statement.egi if statement.egi > 0 else None
        console.print(
            f"Cap rate {percent(statement.noi / result.deal.price)} | "
            f"expense ratio {percent(expense_ratio)} | "
            f"rent upside {percent(kpis.rent_upside, 1)}",
            style="dim",
        )


def _scenario_rows(result: AnalysisResult) -> list[tuple[str, list[str]]]:
    scenarios = result.scenarios

    def each(render) -> list[str]:
        return [render(scenario) for scenario in scenarios]

    def rate(s: ScenarioResult) -> str:
        return "cash" if s.terms.is_cash else percent(s.terms.annual_rate)

    def down(s: ScenarioResult) -> str:
        return f"{money(s.loan.down_payment)} ({s.loan.down_payment / s.loan.price:.1%})"

    def mao(s: ScenarioResult) -> str:
        if s.mao.price is None:
            return s.mao.reason or NOT_AVAILABLE
        return money(s.mao.price)

    rows: list[tuple[str, list[str]]] = [
        ("Rate", each(rate)),
        ("Down payment", each(down)),
        ("Loan amount", each(lambda s: money(s.loan.amount))),
        ("P&I per month", each(lambda s: money(s.loan.monthly_pi, cents=True))),
        ("Mortgage insurance per month", each(lambda s: money(s.loan.monthly_mi, cents=True))),
        ("Cash flow per year", each(lambda s: money(s.kpis.cash_flow_year, cents=True))),
        (
            "Cash flow per door per month",
            each(lambda s: money(s.kpis.cash_flow_door_month, cents=True)),
        ),
        ("Cash invested", each(lambda s: money(s.kpis.cash_invested))),
        ("Cash-on-cash return", each(lambda s: percent(s.kpis.coc))),
        ("DSCR (NOI / debt service)", each(lambda s: ratio(s.kpis.dscr))),
        ("Lender DSCR (rent / PITIA)", each(lambda s: ratio(s.kpis.lender_dscr))),
        ("Break-even occupancy", each(lambda s: percent(s.kpis.break_even_occupancy))),
        (
            "Year-1 principal paydown",
            each(lambda s: money(s.kpis.year1_principal_paydown, cents=True)),
        ),
        ("Year-1 total return", each(lambda s: percent(s.kpis.year1_total_return))),
        (
            f"{result.scenarios[0].hold.years[-1].year}-year IRR",
            each(lambda s: percent(s.hold.irr)),
        ),
        ("Equity multiple", each(lambda s: ratio(s.hold.equity_multiple))),
        ("Max allowable offer", each(mao)),
        ("  target that binds", each(lambda s: ", ".join(s.mao.binding) or "-")),
    ]
    if any(s.reserves_required for s in scenarios):
        rows.append(("Reserves the lender wants", each(lambda s: money(s.reserves_required))))
    return rows


def _scenarios(result: AnalysisResult, console: Console) -> None:
    table = _table("", *(s.terms.name for s in result.scenarios))
    for label, cells in _scenario_rows(result):
        table.add_row(label, *cells)
    console.print(table)
    for scenario in result.scenarios:
        name = scenario.terms.name
        info = result.rates[name]
        if not scenario.terms.is_cash:
            style = "yellow" if info.flagged else "dim"
            console.print(f"{name}: rate from {info.source}", style=style)
    if any(s.view == "moved_out" for s in result.scenarios):
        console.print(
            "Owner-occupied loans show the building after you move out: every unit rented, "
            "a property manager paid, same loan.",
            style="dim",
        )
    for name, reason in result.skipped.items():
        console.print(f"{name}: {reason}", style="dim")


def _owner_views(result: AnalysisResult, console: Console) -> None:
    for scenario in result.scenarios:
        hack = scenario.house_hack
        if hack is None:
            continue
        console.print(
            f"\n[bold]{scenario.terms.label}[/bold], living in unit {hack.owner_unit + 1}"
        )
        cheaper = "cheaper" if hack.savings_vs_renting >= 0 else "more"
        console.print(
            f"  Your net housing cost: {money(hack.owner_net_monthly_cost, cents=True)} a month, "
            f"{money(abs(hack.savings_vs_renting), cents=True)} {cheaper} than renting at "
            f"{money(hack.comparison_rent, cents=True)}"
        )
        test = scenario.self_sufficiency
        if test is not None:
            verdict = "passes by" if test.passes else "FAILS by"
            console.print(
                f"  FHA self-sufficiency: payment {money(test.payment, cents=True)} against "
                f"{money(test.allowed_payment, cents=True)} allowed, {verdict} "
                f"{money(abs(test.margin), cents=True)}; highest passing price "
                f"{money(test.max_passing_price)}",
                style=None if test.passes else "bold red",
            )


def _sensitivity(result: AnalysisResult, console: Console) -> None:
    names = [s.terms.name for s in result.scenarios]
    index = names.index(result.ranking_scenario) if result.ranking_scenario in names else 0
    scenario = result.scenarios[index]
    if not scenario.sensitivity:
        return
    table = _table(
        f"Sensitivity, {scenario.terms.name}", "Cash flow / door / mo", "Change", "CoC", "DSCR"
    )
    table.add_row(
        "As underwritten",
        money(scenario.kpis.cash_flow_door_month, cents=True),
        "",
        percent(scenario.kpis.coc),
        ratio(scenario.kpis.dscr),
    )
    for case in scenario.sensitivity:
        table.add_row(
            case.name,
            money(case.cash_flow_door_month, cents=True),
            money(case.change_door_month, cents=True),
            percent(case.coc),
            ratio(case.dscr),
        )
    console.print(table)


def _notes_and_flags(result: AnalysisResult, console: Console) -> None:
    for scenario in result.scenarios:
        for note in scenario.notes:
            console.print(f"{scenario.terms.name}: {note}", style="yellow")
    if result.flags:
        console.print("\n[bold]Flags[/bold]")
    for flag in result.flags:
        console.print(
            f"  {_LEVEL_MARK[flag.level]}: {flag.message}", style=_LEVEL_STYLE[flag.level]
        )


def _sources(market: MarketContext, console: Console) -> None:
    table = _table("Market data", "State", "Detail", numeric_from=99)
    for status in market.sources:
        table.add_row(status.label, status.state, status.detail, style=_STATE_STYLE[status.state])
    console.print(table)


def _closing_lines(attributions: list[str], disclaimer: str, console: Console) -> None:
    console.print()
    for notice in attributions:
        console.print(notice, style="dim")
    console.print(disclaimer, style="italic")


def render_market(market: MarketContext, config: Config, console: Console) -> None:
    """Everything known about one ZIP, and what each figure does to the underwriting."""
    settings = config.settings
    place = market.place
    console.print(f"[bold]{place.describe() if place else market.zip_code}[/bold]")
    if place is not None and place.in_contiguous_us is False:
        console.print(
            f"{place.state} is outside the 48 contiguous states and DC that this tool covers.",
            style="yellow",
        )
    _sources(market, console)

    # Mortgage rate and what each loan would price at today.
    probe = AnalyzeRequest(price=1.0, rents=[0.0], zip_code=market.zip_code)
    base, source, flagged = base_rate(probe, config, market)
    console.print(
        f"[bold]Base mortgage rate[/bold] {percent(base)}, from {source}",
        style="yellow" if flagged else None,
    )
    rates = _table("Loan", "Spread", "Rate")
    for name, preset in settings.financing.presets.items():
        if not preset.enabled or preset.program == "cash":
            continue
        if preset.rate_quote is not None:
            rates.add_row(name, "your quote", percent(preset.rate_quote))
        else:
            rates.add_row(
                name, f"+{preset.rate_spread * 100:.2f} pt", percent(base + preset.rate_spread)
            )
    console.print(rates)

    # Rents
    rents = market.fair_market_rents
    if rents is not None:
        table = _table(rents.describe(), "Gross rent", "Utility allowance", "Net rent")
        for bedrooms in sorted(rents.rents):
            gross = rents.gross_rent(bedrooms, settings.market.fmr_extra_bedroom_pct)
            allowance = config.utility_allowance_for(bedrooms)
            table.add_row(
                "Studio" if bedrooms == 0 else f"{bedrooms} bedroom",
                money(gross),
                money(allowance),
                money(max(0.0, gross - allowance)),
            )
        console.print(table)
        console.print(
            "Gross rent includes utilities. The net figure is used when tenants pay their own "
            "(the allowance is a placeholder).",
            style="dim",
        )
    index = market.rent_index
    if index is not None:
        growth = (
            "no figure a year back"
            if index.yoy_growth is None
            else (f"{index.yoy_growth:+.1%} over 12 months")
        )
        console.print(
            f"[bold]Typical rent[/bold] {money(index.level)} a month in {index.geography} "
            f"{index.region} ({growth}; Zillow ZORI, {index.as_of:%b %Y})"
        )

    # Taxes and vacancy
    housing = market.housing
    unknown = settings.when_unknown
    if housing is not None:
        computed = housing.effective_tax_rate()
        if computed is not None:
            rate, method = computed
            console.print(
                f"[bold]Property tax rate[/bold] {percent(rate)} ({method}; {housing.vintage}, "
                f"{housing.name})"
            )
        rule = settings.underwriting.vacancy
        if housing.rental_vacancy is not None:
            used = min(0.99, max(rule.floor, housing.rental_vacancy + rule.county_add))
            console.print(
                f"[bold]Vacancy[/bold] {percent(used)} (county rental vacancy "
                f"{percent(housing.rental_vacancy, 1)} + {rule.county_add * 100:g} pt, floor "
                f"{percent(rule.floor, 0)})"
            )
    else:
        console.print(
            f"[bold]Property tax rate[/bold] {percent(unknown.tax_rate)} and [bold]vacancy[/bold] "
            f"{percent(unknown.vacancy_rate, 0)} are stand-ins until Census data is connected.",
            style="yellow",
        )

    # Hazard and insurance
    insurance = settings.underwriting.insurance
    state = place.state if place else None
    base_premium = config.insurance_by_state.get(state or "", insurance.per_unit_year)
    risk = market.risk
    if risk is not None:
        high = risk.rating in settings.market.high_risk_ratings
        multiplier = insurance.high_risk_multiplier if high else 1.0
        score = "" if risk.score is None else f", score {risk.score:.0f} of 100"
        detail = f"hazard rating {risk.rating}{score}"
    else:
        multiplier = insurance.high_risk_multiplier if unknown.high_hazard_county else 1.0
        detail = "hazard rating not loaded"
    console.print(
        f"[bold]Insurance[/bold] {money(base_premium * multiplier)} per unit per year "
        f"(placeholder {money(base_premium)} x {multiplier:g}; {detail})"
    )

    # Loan limits
    county = place.county_fips if place else None
    limits = _table(f"Loan limits {config.loan_limits.year}", "1 unit", "2 units", "3", "4")
    for label, kind in (("FHA", "fha"), ("Conforming", "conforming_county")):
        cells = [money(config.loan_limits.limit(kind, units, county)) for units in (1, 2, 3, 4)]
        limits.add_row(label, *cells)
    console.print(limits)
    if not config.loan_limits.lookup("fha", 4, county).county_specific:
        console.print("County not in the limit table: national minimums shown.", style="yellow")

    _closing_lines(market.attributions, "Estimates only. Verify before you rely on them.", console)


def render_plan(plan: SweepPlan, console: Console) -> None:
    """What a sweep would cost, with nothing fetched."""
    console.print(
        f"[bold]Dry run[/bold]: {plan.mode} sweep of {len(plan.rows)} "
        f"{'state' if len(plan.rows) == 1 else 'states'} for {', '.join(plan.types)}. "
        "Nothing was fetched."
    )
    table = _table("State", "Requests", "Basis", numeric_from=99)
    table.columns[1].justify = "right"
    for row in plan.rows:
        table.add_row(row.state, f"{row.calls}{'' if row.known else '+'}", row.note)
    console.print(table)
    at_least = "" if plan.all_known else "at least "
    console.print(f"This run: {at_least}{plan.calls} billed requests.")
    if plan.remaining is None:
        console.print(f"Budget: {plan.budget_used} used this billing period, no cap set.")
    else:
        fits = plan.states_that_fit()
        verdict = (
            "The run fits."
            if fits == len(plan.rows)
            else f"The run would stop after {fits} of {len(plan.rows)} states and resume next "
            "period or when the cap is raised."
        )
        console.print(
            f"Budget: {plan.budget_used} of {plan.budget_cap} used this billing period, "
            f"{plan.remaining} left. {verdict}",
            style=None if fits == len(plan.rows) else "yellow",
        )
    if plan.monthly_calls:
        console.print(
            f"Run {plan.mode} for a month: about {plan.monthly_calls:,.0f} requests"
            f"{'' if plan.all_known else ' or more'}, which would cost:"
        )
        for name, cost in plan.monthly_cost.items():
            console.print(f"  {name:<11} {money(cost, cents=True)} a month")


def render_usage(
    guard: BudgetGuard, config: Config, runs: Sequence[RunRow], console: Console
) -> None:
    budget = config.settings.budgets.rentcast
    used = guard.used()
    cap = "no cap" if guard.cap is None else str(guard.cap)
    console.print(
        f"[bold]RentCast[/bold] ({budget.plan} plan): {used} of {cap} requests used since "
        f"{short_date(guard.period_start().date())}"
        + ("" if guard.cap is None else f", {max(0, guard.cap - used)} left")
    )
    if not runs:
        console.print("No runs yet.", style="dim")
        return
    table = _table("Run", "Mode", "Status", "Started", "Billed", "New listings", numeric_from=4)
    for run in runs:
        stats = run.stats or {}
        new = sum(scope.get("new_listings", 0) for scope in stats.get("scopes", [])) or stats.get(
            "new_listings", 0
        )
        table.add_row(
            str(run.id),
            run.mode,
            run.status.replace("_", " "),
            f"{run.started_at:%b %d %H:%M}",
            str(stats.get("requests", 0)),
            str(new),
        )
    console.print(table)


def render_listings(rows: Sequence[ListingRow], console: Console) -> None:
    if not rows:
        console.print("No listings stored yet. Run a sweep or import a CSV.", style="dim")
        return
    table = _table("Price", "Type", "Units", "Beds", "Baths", "Address", "Source", "Days")
    for index in (1, 5, 6):
        table.columns[index].justify = "left"
    for row in rows:
        place = row.property
        address = ", ".join(
            part for part in (place.address_line1, place.city, place.state, place.zip_code) if part
        )
        table.add_row(
            money(row.price),
            place.property_kind.replace("_", " "),
            "?" if place.unit_count is None else str(place.unit_count),
            "" if place.bedrooms is None else f"{place.bedrooms:g}",
            "" if place.bathrooms is None else f"{place.bathrooms:g}",
            address,
            row.source,
            "" if row.days_on_market is None else str(row.days_on_market),
        )
    console.print(table)


def render_analysis(result: AnalysisResult, console: Console) -> None:
    _header(result, console)
    _inputs(result, console)
    if result.market is not None:
        _sources(result.market, console)
    _statement(result, console)
    if result.scenarios:
        _scenarios(result, console)
        _owner_views(result, console)
        _sensitivity(result, console)
    else:
        console.print("No loan preset applies to this deal.", style="yellow")
        for name, reason in result.skipped.items():
            console.print(f"{name}: {reason}", style="dim")
    _notes_and_flags(result, console)
    _closing_lines(result.attributions, result.disclaimer, console)
