"""Terminal output for an analysis."""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.table import Table

from dealfinder.analysis import AnalysisResult, InputLine
from dealfinder.underwrite import ScenarioResult

NOT_AVAILABLE = "n/a"
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


def render_analysis(result: AnalysisResult, console: Console) -> None:
    _header(result, console)
    _inputs(result, console)
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
    console.print(f"\n{result.disclaimer}", style="italic")
