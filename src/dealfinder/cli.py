"""Command line: ``dealfinder analyze`` underwrites one deal with no network access."""

from __future__ import annotations

from typing import Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from dealfinder.analysis import AnalysisError, AnalyzeRequest, analyze
from dealfinder.render import render_analysis
from dealfinder.settings import Environment, SettingsError, load_config

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find and underwrite small multifamily deals. Percent options take percent: 7.25 = 7.25%.",
)


@app.callback()
def main() -> None:
    """Keeps ``analyze`` a named subcommand while it is the only deal command."""


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
    owner_unit: Annotated[
        int | None,
        typer.Option(help="Unit you would live in (1 = first). Default: the highest rent."),
    ] = None,
    my_rent: Annotated[
        float | None,
        typer.Option(help="What you pay in rent today, to compare a house hack against."),
    ] = None,
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
        result = analyze(request, config)
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


if __name__ == "__main__":
    app()
