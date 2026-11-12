"""Command line: ``analyze`` underwrites one deal, ``market`` shows the data for a ZIP code."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from sqlalchemy import Engine, select

from dealfinder.adapters.rentcast import RentCastSource
from dealfinder.analysis import AnalysisError, AnalyzeRequest, analyze
from dealfinder.geo import normalize_zip
from dealfinder.http import Fetcher
from dealfinder.market.context import MarketContext, load_market
from dealfinder.pipeline.importer import CsvImportError, import_csv
from dealfinder.pipeline.sweep import Mode, SweepError, plan_sweep, run_sweep, sweep_states
from dealfinder.render import (
    render_analysis,
    render_listings,
    render_market,
    render_plan,
    render_usage,
)
from dealfinder.settings import Config, Environment, SettingsError, load_config
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
    )
    return source, guard


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
    if report.mapping_created:
        console.print("\nColumns were matched like this. Edit the mapping file if any are wrong:")
        for field, column in report.mapping.columns.items():
            console.print(f"  {field}: {column}", markup=False)


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
) -> None:
    """List stored listings, cheapest first. Ranking by cash flow arrives with the dashboard."""
    _, _, engine = _load()
    query = select(ListingRow).join(PropertyRow).order_by(ListingRow.price.asc().nulls_last())
    if status != "all":
        query = query.where(ListingRow.status == status)
    if state:
        query = query.where(PropertyRow.state == state.upper())
    with session_scope(engine) as session:
        render_listings(session.scalars(query.limit(limit)).all(), Console())


if __name__ == "__main__":
    app()
