"""Command line: ``analyze`` underwrites one deal, ``market`` shows the data for a ZIP code."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, get_args

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from sqlalchemy import Engine, select

from dealfinder import scheduler
from dealfinder.adapters.rentcast import RentCastSource
from dealfinder.alerts import run_alerts
from dealfinder.analysis import AnalysisError, AnalyzeRequest, analyze
from dealfinder.classify.classifier import ClassifierInput, classify, kind_matches
from dealfinder.classify.evaluate import append_label_rows, evaluate, load_labeled
from dealfinder.classify.llm import ClaudeExtractor, Extractor
from dealfinder.export import to_csv, to_xlsx
from dealfinder.geo import normalize_zip
from dealfinder.http import Fetcher
from dealfinder.market.context import MarketContext, load_market
from dealfinder.pipeline.classify import (
    classify_all,
    label_rows,
    resolve_units,
    unresolved_listings,
)
from dealfinder.pipeline.importer import CsvImportError, import_csv
from dealfinder.pipeline.report import deal_rows, problem_rows
from dealfinder.pipeline.run import run_all, write_log
from dealfinder.pipeline.sweep import Mode, SweepError, plan_sweep, run_sweep, sweep_states
from dealfinder.pipeline.underwrite import run_funnel
from dealfinder.render import (
    render_analysis,
    render_classification,
    render_listings,
    render_market,
    render_plan,
    render_problems,
    render_report,
    render_usage,
)
from dealfinder.settings import Config, Environment, PropertyType, SettingsError, load_config
from dealfinder.store import repo
from dealfinder.store.db import open_database, session_scope
from dealfinder.store.tables import ListingRow, PropertyRow, RunRow
from dealfinder.store.usage import BudgetGuard

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find and underwrite small multifamily deals. Percent options take percent: 7.25 = 7.25%.",
)


@app.callback()
def main() -> None:
    """Find and underwrite small multifamily deals."""


def market_for(
    zip_code: str, config: Config, *, offline: bool = False, refresh: bool = False
) -> MarketContext:
    """Market data for a ZIP from the cache and, unless offline, the network."""
    environment = Environment()
    fetcher = Fetcher(
        environment.cache_dir,
        offline=offline or environment.dealfinder_offline,
        refresh=refresh,
    )
    try:
        return load_market(zip_code, config, environment, fetcher)
    finally:
        fetcher.close()


def parse_rents(text: str) -> list[float]:
    """'1350,1350,1200' or '1350x4' or a mix such as '1350x2,1200,950'."""
    rents: list[float] = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        amount, _, count = token.lower().partition("x")
        try:
            rents.extend([float(amount.lstrip("$"))] * (int(count) if count else 1))
        except ValueError as error:
            raise typer.BadParameter(
                f"'{token}' is not a rent. Use 1350,1350,1200 or 1350x4."
            ) from error
    if not rents:
        raise typer.BadParameter("Give at least one rent.")
    return rents


def _fraction(percent_value: float | None) -> float | None:
    return None if percent_value is None else percent_value / 100


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=2)


@app.command("analyze")
def analyze_command(
    price: Annotated[float, typer.Option(help="Purchase price in dollars.")],
    rents: Annotated[
        str, typer.Option(help="Current monthly rent per unit: 1350,1350,1200 or 1350x4.")
    ],
    zip_code: Annotated[str, typer.Option("--zip", help="Five-digit ZIP code.")],
    market_rents: Annotated[
        str | None, typer.Option(help="Estimated market rent per unit, same format as --rents.")
    ] = None,
    year_built: Annotated[
        int | None, typer.Option(help="Sets the repairs and reserves band.")
    ] = None,
    sqft: Annotated[float | None, typer.Option(help="Building square feet.")] = None,
    tax_rate: Annotated[
        float | None, typer.Option(help="Property tax as a percent of price, e.g. 1.2.")
    ] = None,
    tax_bill: Annotated[
        float | None, typer.Option(help="Last annual tax bill. Taxes never go below it.")
    ] = None,
    vacancy: Annotated[float | None, typer.Option(help="Vacancy percent, e.g. 6.")] = None,
    management: Annotated[
        str | None, typer.Option(help="self or manager. Defaults to the setting.")
    ] = None,
    insurance: Annotated[
        float | None, typer.Option(help="Hazard insurance per unit per year.")
    ] = None,
    utilities: Annotated[
        float | None, typer.Option(help="Owner-paid utilities per unit per month.")
    ] = None,
    tenants_pay_utilities: Annotated[
        bool | None,
        typer.Option("--tenants-pay-utilities/--owner-pays-utilities", help="Who pays utilities."),
    ] = None,
    hoa: Annotated[float, typer.Option(help="HOA dues per month.")] = 0.0,
    flood_zone: Annotated[
        bool | None,
        typer.Option("--flood-zone/--no-flood-zone", help="Whether it sits in a FEMA flood zone."),
    ] = None,
    flood_insurance: Annotated[
        float | None, typer.Option(help="Flood insurance per year, if you have a figure.")
    ] = None,
    rehab: Annotated[float | None, typer.Option(help="Rehab budget in dollars.")] = None,
    credits: Annotated[float, typer.Option(help="Seller credits in dollars.")] = 0.0,
    closing: Annotated[float | None, typer.Option(help="Closing costs, percent of price.")] = None,
    base_rate: Annotated[
        float | None, typer.Option(help="Base mortgage rate percent; each loan adds its spread.")
    ] = None,
    rate: Annotated[
        float | None,
        typer.Option(help="Your quoted rate percent, used as-is for every loan shown."),
    ] = None,
    scenario: Annotated[
        list[str] | None,
        typer.Option(help="Loan preset to show. Repeat for several. Default: all enabled."),
    ] = None,
    beds: Annotated[
        str | None,
        typer.Option(help="Bedrooms per unit, e.g. 2,2,1,1 or 2x4. Used to look up market rents."),
    ] = None,
    owner_unit: Annotated[
        int | None,
        typer.Option(help="Unit you would live in (1 = first). Default: the highest rent."),
    ] = None,
    my_rent: Annotated[
        float | None,
        typer.Option(help="What you pay in rent today, to compare a house hack against."),
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use cached market data only; make no requests.")
    ] = False,
    no_market: Annotated[
        bool,
        typer.Option("--no-market", help="Ignore market data; use entered values and stand-ins."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Underwrite one deal from its price, rents and ZIP code."""
    if management is not None and management not in ("self", "manager"):
        raise typer.BadParameter("Use self or manager.", param_hint="--management")
    try:
        config = load_config()
        request = AnalyzeRequest(
            price=price,
            rents=parse_rents(rents),
            zip_code=zip_code,
            market_rents=parse_rents(market_rents) if market_rents else None,
            beds=parse_rents(beds) if beds else None,
            year_built=year_built,
            building_sqft=sqft,
            tax_rate=_fraction(tax_rate),
            tax_bill=tax_bill,
            vacancy_rate=_fraction(vacancy),
            management=management,  # type: ignore[arg-type]
            insurance_per_unit_year=insurance,
            utilities_per_unit_month=utilities,
            tenants_pay_utilities=tenants_pay_utilities,
            hoa_month=hoa,
            in_flood_zone=flood_zone,
            flood_insurance_year=flood_insurance,
            rehab=rehab,
            credits=credits,
            closing_cost_pct=_fraction(closing),
            base_rate=_fraction(base_rate),
            rate_quote=_fraction(rate),
            scenarios=scenario or None,
            owner_unit=owner_unit,
            comparison_rent=my_rent,
        )
        market = None if no_market else market_for(request.zip_code, config, offline=offline)
        result = analyze(request, config, market)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or 'input'}: {issue['msg']}"
            for issue in error.errors()
        )
        raise _fail(f"Invalid input: {problems}") from error
    except (AnalysisError, SettingsError) as error:
        raise _fail(str(error)) from error

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        render_analysis(result, Console())


@app.command("market")
def market_command(
    zip_code: Annotated[str, typer.Option("--zip", help="Five-digit ZIP code.")],
    offline: Annotated[
        bool, typer.Option("--offline", help="Use cached data only; make no requests.")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Download everything again, ignoring the cache.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Show rents, taxes, vacancy, rates, hazard rating and loan limits for a ZIP code."""
    if normalize_zip(zip_code) is None:
        raise typer.BadParameter(f"'{zip_code}' is not a 5-digit ZIP code.", param_hint="--zip")
    try:
        config = load_config()
    except SettingsError as error:
        raise _fail(str(error)) from error
    market = market_for(zip_code, config, offline=offline, refresh=refresh)
    if json_output:
        typer.echo(market.model_dump_json(indent=2))
    else:
        render_market(market, config, Console())


@app.command("settings")
def settings_command() -> None:
    """Show the settings in effect, the files they came from, and which keys are set."""
    try:
        config = load_config()
    except SettingsError as error:
        raise _fail(str(error)) from error
    console = Console()
    console.print("[bold]Config files in use[/bold]")
    for path in config.files:
        console.print(f"  {path}")
    console.print(
        f"  Personal overrides go in {config.config_dir / 'settings.local.yaml'}", style="dim"
    )
    console.print("\n[bold]Keys[/bold] (from .env; values are never shown)")
    for name, present in Environment().keys_set().items():
        console.print(f"  {name}: {'set' if present else 'not set'}")
    console.print("\n[bold]Settings in effect[/bold]")
    console.print(
        yaml.safe_dump(config.settings.model_dump(mode="json"), sort_keys=False), markup=False
    )


# ---- listings: sweeps, imports, usage --------------------------------------------------------


def _load() -> tuple[Config, Environment, Engine]:
    """Config, environment and a database with its schema up to date."""
    try:
        config = load_config()
    except SettingsError as error:
        raise _fail(str(error)) from error
    environment = Environment()
    return config, environment, open_database(environment.dealfinder_db_url)


def _rentcast(
    config: Config, environment: Environment, engine: Engine, fetcher: Fetcher
) -> tuple[RentCastSource, BudgetGuard]:
    budget = config.settings.budgets.rentcast
    guard = BudgetGuard(
        engine, "rentcast", cap=budget.monthly_request_cap, cycle_day=budget.billing_cycle_day
    )

    def last_count(scope: str) -> int | None:
        with session_scope(engine) as session:
            return repo.inventory_total(session, "rentcast", scope)

    key = environment.rentcast_api_key
    source = RentCastSource(
        fetcher,
        api_key=key.get_secret_value() if key else None,
        guard=guard,
        sweep=config.settings.sweep,
        max_price=config.settings.buy_box.max_price,
        inventory=last_count,
        record_cache_days=config.settings.classify.property_record_cache_days,
    )
    return source, guard


def _property_types(text: str) -> list[str]:
    """Property types from a comma-separated option, checked against the ones that exist."""
    known = get_args(PropertyType)
    chosen = [part.strip().lower() for part in text.split(",") if part.strip()]
    unknown = [name for name in chosen if name not in known]
    if unknown or not chosen:
        raise _fail(f"Unknown property type {', '.join(unknown)}. Choose from: {', '.join(known)}.")
    return chosen


def _extractor(
    config: Config, environment: Environment, console: Console, *, wanted: bool | None = None
) -> Extractor | None:
    """The step that has Claude read descriptions, or None with the reason printed when off.

    ``wanted`` None follows ``classify.llm.enabled`` in settings.
    """
    llm = config.settings.classify.llm
    if not (llm.enabled if wanted is None else wanted):
        return None
    if not environment.anthropic_api_key:
        console.print(
            "Claude is not reading descriptions: ANTHROPIC_API_KEY is not set.", style="yellow"
        )
        return None
    try:
        import anthropic
    except ImportError:
        console.print(
            "Claude is not reading descriptions: the Anthropic SDK is not installed "
            "(uv sync --extra llm).",
            style="yellow",
        )
        return None
    client = anthropic.Anthropic(api_key=environment.anthropic_api_key.get_secret_value())
    return ClaudeExtractor(client, model=llm.model, effort=llm.effort, max_tokens=llm.max_tokens)


def _sweep(mode: Mode, states: str | None, dry_run: bool, fresh: bool) -> None:
    config, environment, engine = _load()
    console = Console()
    if not config.settings.sources["rentcast"].enabled:
        raise _fail("RentCast is switched off in settings (sources.rentcast.enabled).")
    try:
        chosen = sweep_states(config, states.split(",") if states else None)
    except SweepError as error:
        raise _fail(str(error)) from error
    types = config.settings.buy_box.property_types
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        source, guard = _rentcast(config, environment, engine, fetcher)
        plan = plan_sweep(source, guard, config, mode=mode, states=chosen, types=types)
        if dry_run:
            render_plan(plan, console)
            return
        if not environment.rentcast_api_key:
            raise _fail(
                "RentCast is off: RENTCAST_API_KEY is not set. Add it to .env, or use "
                "--dry-run to see what a sweep would cost."
            )
        report = run_sweep(
            engine, config, source, guard, mode=mode, states=chosen, types=types, fresh=fresh
        )
    finally:
        fetcher.close()
    style = {"completed": None, "stopped_budget": "yellow", "failed": "red"}[report.status]
    console.print(report.text(), style=style, markup=False)
    console.print(classify_all(engine, config).text(), markup=False)
    if report.status == "failed":
        raise typer.Exit(code=1)


@app.command("sweep")
def sweep_command(
    mode: Annotated[str, typer.Option(help="weekly: every active listing. daily: new ones only.")],
    states: Annotated[
        str | None, typer.Option(help="Comma-separated states, e.g. OH,IN. Default: the buy box.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show expected requests and cost; fetch nothing.")
    ] = False,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Start a new run instead of resuming an unfinished one.")
    ] = False,
) -> None:
    """Fetch listings from RentCast, state by state, within the request budget."""
    if mode not in ("daily", "weekly"):
        raise typer.BadParameter("Use daily or weekly.", param_hint="--mode")
    _sweep(mode, states, dry_run, fresh)  # type: ignore[arg-type]


@app.command("probe")
def probe_command(
    states: Annotated[
        str | None, typer.Option(help="Comma-separated states. Default: the buy box.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show expected requests; fetch nothing.")
    ] = False,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Start a new run instead of resuming an unfinished one.")
    ] = False,
) -> None:
    """Count each state's listings with one request per state, keeping what comes back."""
    _sweep("probe", states, dry_run, fresh)


@app.command("import-csv")
def import_csv_command(
    path: Annotated[Path, typer.Argument(help="CSV file of listings.")],
    mapping: Annotated[
        str,
        typer.Option(
            help="Name for this file layout. Guessed and saved the first time it is used."
        ),
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check the file and mapping; store nothing.")
    ] = False,
) -> None:
    """Import your own list: an agent's MLS export, a LoopNet download, a wholesaler's sheet."""
    config, _, engine = _load()
    if not config.settings.sources["csv"].enabled:
        raise _fail("CSV import is switched off in settings (sources.csv.enabled).")
    try:
        report = import_csv(engine, config, path, mapping, dry_run=dry_run)
    except CsvImportError as error:
        raise _fail(str(error)) from error
    console = Console()
    console.print(report.text(), markup=False)
    if not dry_run:
        console.print(classify_all(engine, config).text(), markup=False)
    if report.mapping_created:
        console.print("\nColumns were matched like this. Edit the mapping file if any are wrong:")
        for field, column in report.mapping.columns.items():
            console.print(f"  {field}: {column}", markup=False)


@app.command("classify")
def classify_command(
    description: Annotated[str, typer.Argument(help="Listing description to read.")],
    units: Annotated[int | None, typer.Option(help="Unit count the source states, if any.")] = None,
    beds: Annotated[float | None, typer.Option(help="Total bedrooms in the building.")] = None,
    baths: Annotated[float | None, typer.Option(help="Total bathrooms in the building.")] = None,
    kind: Annotated[
        str, typer.Option(help="What the source calls it, e.g. multifamily_2_4, sfr, condo.")
    ] = "multifamily_2_4",
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Show how one description is read: unit count, confidence, evidence, tags and rents."""
    try:
        config = load_config()
        entry = ClassifierInput(
            source_kind=kind,  # type: ignore[arg-type]
            stated_units=units,
            description=description,
            bedrooms=beds,
            bathrooms=baths,
        )
    except SettingsError as error:
        raise _fail(str(error)) from error
    except ValidationError as error:
        raise _fail(f"'{kind}' is not a property kind.") from error
    result = classify(entry, config.keywords)
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        render_classification(result, Console())


@app.command("classify-eval")
def classify_eval_command(
    path: Annotated[Path, typer.Argument(help="CSV with 'description' and 'units' columns.")],
) -> None:
    """Score the classifier against descriptions you have labeled by hand."""
    try:
        config = load_config()
        rows = load_labeled(path)
    except (SettingsError, ValueError, OSError) as error:
        raise _fail(str(error)) from error
    Console().print(evaluate(rows, config.keywords).text(), markup=False)


@app.command("label-sheet")
def label_sheet_command(
    path: Annotated[
        Path | None,
        typer.Option(help="Where to write it. Default: data/labeled/descriptions.csv."),
    ] = None,
) -> None:
    """Write stored descriptions to a sheet for checking the classifier against real listings.

    Rows whose source stated a unit count arrive already labeled. Fill in the 'units' column
    for the rest (a number, land, or unknown), then run classify-eval on the file.
    """
    _, environment, engine = _load()
    target = path or environment.dealfinder_data_dir / "labeled" / "descriptions.csv"
    rows = label_rows(engine)
    if not rows:
        raise _fail("No stored listing has a description yet. Import an MLS export first.")
    added, already = append_label_rows(target, rows)
    blank = sum(1 for row in rows if not row["units"])
    console = Console()
    console.print(
        f"Added {added} descriptions to {target} ({already} were already there).", markup=False
    )
    console.print(
        f"{len(rows) - blank} carry the unit count the source stated; {blank} need the 'units' "
        "column filled in by hand (a number, land, or unknown).",
        markup=False,
    )
    console.print(f"Then: dealfinder classify-eval {target}", markup=False)


@app.command("classify-all")
def classify_all_command(
    llm: Annotated[
        bool | None,
        typer.Option(
            "--llm/--no-llm",
            help="Have Claude read descriptions the rules are unsure of. Default: the setting.",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Read every property again, changed or not.")
    ] = False,
) -> None:
    """Classify stored properties from their listings: kind, unit count, units and rents.

    Imports and sweeps do this on their own with the rules; run it by hand after editing
    config/keywords.yaml or to have Claude read the uncertain ones.
    """
    config, environment, engine = _load()
    console = Console()
    extractor = _extractor(config, environment, console, wanted=llm)
    report = classify_all(engine, config, extractor=extractor, force=force)
    console.print(report.text(), markup=False)


@app.command("resolve-units")
def resolve_units_command(
    limit: Annotated[
        int | None,
        typer.Option(help="Most listings to look up. Default: as many as the budget allows."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show how many lookups are pending; fetch nothing.")
    ] = False,
) -> None:
    """Look up RentCast property records for listings whose unit count is unconfirmed.

    Each lookup is one billed request and can bring back the unit count and the last tax bill.
    """
    config, environment, engine = _load()
    console = Console()
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        source, guard = _rentcast(config, environment, engine, fetcher)
        if dry_run:
            with session_scope(engine) as session:
                pending = len(unresolved_listings(session, config))
            wanted = pending if limit is None else min(pending, limit)
            left = "no cap" if guard.cap is None else f"{guard.remaining()} left of {guard.cap}"
            console.print(
                f"{pending} listings have no confirmed unit count. Looking up {wanted} would "
                f"take up to {wanted} billed requests. Budget: {left}."
            )
            return
        if not environment.rentcast_api_key:
            raise _fail("RentCast is off: RENTCAST_API_KEY is not set. Add it to .env.")
        report = resolve_units(engine, config, source, limit=limit)
    finally:
        fetcher.close()
    console.print(report.text(), style="yellow" if report.stopped else None, markup=False)


@app.command("underwrite")
def underwrite_command(
    stage1_only: Annotated[
        bool,
        typer.Option("--stage1-only", help="Free data only: no RentCast, flood or Claude calls."),
    ] = False,
) -> None:
    """Underwrite every stored listing of the wanted types, enrich the best, and score them.

    Stage 1 uses free data. Stage 2 spends RentCast requests on the survivors, best first,
    inside the request budget.
    """
    config, environment, engine = _load()
    console = Console()
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        source, _ = _rentcast(config, environment, engine, fetcher)
        rentcast = source if environment.rentcast_api_key else None
        if not config.settings.sources["rentcast"].enabled:
            rentcast = None
        extractor = None if stage1_only else _extractor(config, environment, console)
        report = run_funnel(
            engine,
            config,
            environment,
            fetcher,
            rentcast=rentcast,
            extractor=extractor,
            stage2=not stage1_only,
        )
    except AnalysisError as error:
        raise _fail(str(error)) from error
    finally:
        fetcher.close()
    console.print(report.text(), markup=False)
    console.print("See them with: dealfinder report --top 20", style="dim")


@app.command("report")
def report_command(
    top: Annotated[int, typer.Option(help="How many deals to show.")] = 20,
    scenario: Annotated[
        str | None, typer.Option(help="Loan preset to rank by. Default: the ranking scenario.")
    ] = None,
    state: Annotated[str | None, typer.Option(help="Two-letter state.")] = None,
    buy_box: Annotated[
        bool, typer.Option("--buy-box", help="Only deals inside the buy box.")
    ] = False,
    unverified: Annotated[
        bool, typer.Option("--unverified", help="Show deals whose unit count is unconfirmed.")
    ] = False,
    problems: Annotated[
        bool, typer.Option("--problems", help="Show listings that could not be underwritten.")
    ] = False,
    csv: Annotated[
        Path | None, typer.Option(help="Also write every deal in the list to this CSV file.")
    ] = None,
    xlsx: Annotated[
        Path | None, typer.Option(help="Also write every deal in the list to this Excel file.")
    ] = None,
) -> None:
    """Rank the underwritten deals by cash flow per door."""
    config, _, engine = _load()
    console = Console()
    with session_scope(engine) as session:
        if problems:
            render_problems(problem_rows(session), console)
            return
        name = scenario or config.settings.buy_box.ranking_scenario
        if name not in config.settings.financing.presets:
            known = ", ".join(config.settings.financing.presets)
            raise _fail(f"Unknown scenario {name}. Choose from: {known}.")
        rows = deal_rows(session, config, name)
        waiting = len(problem_rows(session))
    if state:
        rows = [row for row in rows if row.state == state.upper()]
    wanted = "unverified" if unverified else "yes"
    shown = [row for row in rows if row.type_match == wanted and (row.in_buy_box or not buy_box)]
    render_report(shown[:top], name, console, total=len(shown))
    for path, build in ((csv, to_csv), (xlsx, to_xlsx)):
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(build(shown))
            except OSError as error:
                raise _fail(f"Could not write {path}: {error.strerror or error}") from error
            console.print(f"Wrote {len(shown)} deals to {path}", markup=False)
    others = sum(1 for row in rows if row.type_match != wanted)
    if others or waiting:
        console.print(
            f"Also stored: {others} "
            f"{'confirmed' if unverified else 'with an unconfirmed unit count (--unverified)'}, "
            f"{waiting} that could not be underwritten (--problems).",
            style="dim",
        )


@app.command("run")
def run_command(
    mode: Annotated[
        str, typer.Option(help="daily: new listings only. weekly: every active listing.")
    ] = "daily",
    states: Annotated[
        str | None, typer.Option(help="Comma-separated states. Default: the buy box.")
    ] = None,
    no_sweep: Annotated[
        bool, typer.Option("--no-sweep", help="Do not fetch listings; use what is stored.")
    ] = False,
    no_alerts: Annotated[bool, typer.Option("--no-alerts", help="Do not send alerts.")] = False,
) -> None:
    """Everything in one go: fetch, classify, underwrite, alert. The scheduler runs this."""
    if mode not in ("daily", "weekly"):
        raise typer.BadParameter("Use daily or weekly.", param_hint="--mode")
    config, environment, engine = _load()
    console = Console()
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        source, guard = _rentcast(config, environment, engine, fetcher)
        connected = bool(environment.rentcast_api_key)
        report = run_all(
            engine,
            config,
            environment,
            fetcher,
            mode=mode,  # type: ignore[arg-type]
            states=states.split(",") if states else None,
            rentcast=source if connected else None,
            guard=guard if connected else None,
            extractor=_extractor(config, environment, console),
            sweep=not no_sweep,
            alerts=not no_alerts,
        )
    finally:
        fetcher.close()
    console.print(report.text(), markup=False)
    log = write_log(report, environment.dealfinder_data_dir / "logs")
    console.print(f"Logged to {log}", style="dim", markup=False)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("schedule")
def schedule_command(
    install: Annotated[
        bool, typer.Option("--install", help="Add the schedule to cron or Task Scheduler.")
    ] = False,
    remove: Annotated[bool, typer.Option("--remove", help="Take the schedule out again.")] = False,
) -> None:
    """Show the schedule for `dealfinder run`, or install or remove it.

    Times come from `schedule` in settings. Without an option this only shows what would be
    added; nothing on your computer changes.
    """
    import sys

    config, environment, _ = _load()
    if install and remove:
        raise _fail("Choose one of --install and --remove.")
    console = Console()
    project = Path.cwd()
    logs = (environment.dealfinder_data_dir / "logs").resolve()
    items = scheduler.entries(config.settings.schedule, sys.executable)
    try:
        if remove:
            console.print(scheduler.remove(), markup=False)
            return
        for line in scheduler.describe(items):
            console.print(line, markup=False)
        if install:
            console.print(scheduler.install(items, project, logs), markup=False)
            return
    except (RuntimeError, OSError) as error:
        raise _fail(str(error)) from error
    console.print("\nThis is what --install would add:", markup=False)
    if sys.platform.startswith("win"):
        for command in scheduler.windows_commands(items, project, logs):
            console.print(" ".join(command), markup=False, soft_wrap=True)
    else:
        for line in scheduler.crontab_lines(items, project, logs):
            console.print(line, markup=False, soft_wrap=True)
    console.print(
        "\nA computer that is asleep at these times misses the run; the next one catches up.",
        style="dim",
    )


@app.command("alerts")
def alerts_command(
    send: Annotated[
        bool, typer.Option("--send", help="Send the digest. Without it, only show it.")
    ] = False,
) -> None:
    """Show what there is to alert on, or send it: one digest, never the same alert twice.

    New deals inside the buy box graded A or B, deals that qualify after a price cut, and
    price cuts on starred properties.
    """
    config, environment, engine = _load()
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        report = run_alerts(engine, config, environment, fetcher, send=send)
    finally:
        fetcher.close()
    Console().print(report.text(), markup=False)


@app.command("ui")
def ui_command(
    port: Annotated[int, typer.Option(help="Port for the dashboard.")] = 8501,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser window.")
    ] = False,
) -> None:
    """Open the dashboard: ranked deals, map, one deal in full, markets and settings."""
    import subprocess
    import sys
    from importlib.util import find_spec

    if find_spec("streamlit") is None:
        raise _fail("The dashboard needs Streamlit. Install it with: uv sync")
    _load()  # fail early, in plain words, on a bad config or database
    script = Path(__file__).parent / "ui" / "app.py"
    command = [
        sys.executable, "-m", "streamlit", "run", str(script),
        "--server.port", str(port),
        "--browser.gatherUsageStats", "false",
        "--server.headless", "true" if no_browser else "false",
    ]  # fmt: skip
    raise typer.Exit(code=subprocess.call(command))


@app.command("usage")
def usage_command() -> None:
    """Show what has been spent of the RentCast request budget, and recent runs."""
    config, environment, engine = _load()
    fetcher = Fetcher(environment.cache_dir, offline=True)
    try:
        _, guard = _rentcast(config, environment, engine, fetcher)
    finally:
        fetcher.close()
    with session_scope(engine) as session:
        runs = session.scalars(select(RunRow).order_by(RunRow.id.desc()).limit(10)).all()
        render_usage(guard, config, runs, Console())


@app.command("listings")
def listings_command(
    state: Annotated[str | None, typer.Option(help="Two-letter state.")] = None,
    status: Annotated[
        str, typer.Option(help="active, pending, off_market, sold or all.")
    ] = "active",
    limit: Annotated[int, typer.Option(help="How many to show.")] = 25,
    types: Annotated[
        str | None,
        typer.Option(help="Property types, comma-separated, or 'all'. Default: the buy box's."),
    ] = None,
    unverified: Annotated[
        bool,
        typer.Option(
            "--unverified",
            help="Show listings that might be a wanted type but lack a confirmed unit count.",
        ),
    ] = False,
) -> None:
    """List stored listings of the wanted property types, cheapest first.

    Ranking by cash flow arrives with the dashboard.
    """
    config, _, engine = _load()
    box = config.settings.buy_box
    wanted: list[str] | None = list(box.property_types)
    if types is not None:
        wanted = None if types.strip().lower() == "all" else _property_types(types)
    if unverified and wanted is None:
        raise _fail("--unverified needs property types to check against, not --types all.")
    query = select(ListingRow).join(PropertyRow).order_by(ListingRow.price.asc().nulls_last())
    if status != "all":
        query = query.where(ListingRow.status == status)
    if state:
        query = query.where(PropertyRow.state == state.upper())
    console = Console()
    with session_scope(engine) as session:
        rows = session.scalars(query).all()
        if wanted is None:
            render_listings(rows[:limit], console)
            return
        groups: dict[str, list[ListingRow]] = {"yes": [], "unverified": [], "no": []}
        for row in rows:
            place = row.property
            match = kind_matches(
                place.property_kind, place.unit_confidence, wanted, box.min_unit_confidence
            )
            groups[match].append(row)
        shown = groups["unverified" if unverified else "yes"]
        if shown:
            render_listings(shown[:limit], console)
        console.print(
            f"{', '.join(wanted)}: {len(groups['yes'])} confirmed, "
            f"{len(groups['unverified'])} with an unconfirmed unit count, "
            f"{len(groups['no'])} of other kinds.",
            markup=False,
        )
        if not rows:
            console.print("Nothing stored yet. Run a sweep or import a CSV.", style="dim")
        elif unverified and shown:
            console.print(
                "Confirm unit counts with: dealfinder resolve-units (one RentCast request each).",
                style="dim",
            )
        elif not unverified and groups["unverified"]:
            console.print(
                "See the unconfirmed ones with --unverified, everything with --types all.",
                style="dim",
            )


if __name__ == "__main__":
    app()
