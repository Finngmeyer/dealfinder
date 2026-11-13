"""What the dashboard reads and writes. Everything Streamlit-free lives here so it can be
tested without a browser; the pages only arrange what these functions return."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from statistics import median
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import Engine, select

from dealfinder.geo import county_name
from dealfinder.http import Fetcher
from dealfinder.pipeline.report import DealRow, ProblemRow, deal_rows, problem_rows
from dealfinder.pipeline.underwrite import FunnelReport, run_funnel, underwrite_listing
from dealfinder.settings import (
    LOCAL_SETTINGS_FILE,
    Config,
    Environment,
    deep_merge,
    load_config,
)
from dealfinder.store.db import session_scope
from dealfinder.store.overrides import FIELDS, clear_overrides, get_overrides, set_override
from dealfinder.store.tables import (
    ClassificationRow,
    ListingRow,
    PropertyRow,
    ScreenRow,
    utcnow,
)
from dealfinder.store.usage import BudgetGuard

# Inputs whose being estimated matters most to a reader of the ranked table.
_ESTIMATE_LABELS = {
    "rents": "current rents",
    "market_rents": "market rents",
    "tax_rate": "tax rate",
    "tax_bill": "tax bill",
    "insurance": "insurance",
    "vacancy_rate": "vacancy",
    "utilities": "utilities",
    "repairs_pct": "repairs",
    "reserves_pct": "reserves",
    "management_pct": "management",
    "year_built": "year built",
    "rehab": "rehab",
    "flood_insurance": "flood insurance",
    "closing_cost_pct": "closing costs",
    "misc": "misc",
    "price": "price",
    "hoa": "HOA dues",
}


class Provenance(BaseModel):
    """Which of a deal's inputs are actual and which are estimates, for a tooltip."""

    actual: list[str]
    estimated: list[str]
    flagged: list[str]
    rents_estimated: bool

    def text(self) -> str:
        parts = []
        if self.actual:
            parts.append("Actual: " + ", ".join(self.actual) + ".")
        if self.estimated:
            parts.append("Estimated: " + ", ".join(self.estimated) + ".")
        if self.flagged:
            parts.append("Pessimistic stand-ins: " + ", ".join(self.flagged) + ".")
        return " ".join(parts)


def provenance(
    inputs: Sequence[dict[str, Any]], rents_stated: int, units: int | None
) -> Provenance:
    actual, estimated, flagged = [], [], []
    for line in inputs:
        label = _ESTIMATE_LABELS.get(line.get("key", ""), str(line.get("label", "")).lower())
        if line.get("kind") == "estimated":
            estimated.append(label)
            if line.get("flagged"):
                flagged.append(label)
        else:
            actual.append(label + (" (your figure)" if line.get("kind") == "override" else ""))
    return Provenance(
        actual=actual,
        estimated=estimated,
        flagged=flagged,
        rents_estimated=not units or rents_stated < units,
    )


class Deals(BaseModel):
    rows: list[DealRow]
    problems: list[ProblemRow]
    provenance: dict[int, Provenance]  # by listing id
    scenario: str


def load_deals(engine: Engine, config: Config, scenario: str | None = None) -> Deals:
    name = scenario or config.settings.buy_box.ranking_scenario
    with session_scope(engine) as session:
        rows = deal_rows(session, config, name)
        problems = problem_rows(session)
        inputs = dict(session.execute(select(ScreenRow.listing_id, ScreenRow.inputs)).all())
    return Deals(
        rows=rows,
        problems=problems,
        provenance={
            row.listing_id: provenance(
                inputs.get(row.listing_id) or [], row.rents_stated, row.units
            )
            for row in rows
        },
        scenario=name,
    )


class DealFilter(BaseModel):
    states: list[str] = []
    grades: list[str] = []
    buy_box_only: bool = False
    stage2_only: bool = False
    min_cash_flow: float | None = None
    max_price: float | None = None
    search: str = ""
    sort: str = "Cash flow per door"
    descending: bool = True


SORTS = {
    "Cash flow per door": lambda row: row.cash_flow_door_month,
    "Score": lambda row: row.score,
    "Cash-on-cash return": lambda row: row.coc,
    "DSCR": lambda row: row.dscr,
    "Cap rate": lambda row: row.cap_rate,
    "Price": lambda row: row.price,
    "Price per door": lambda row: row.price_per_door,
    "Days on market": lambda row: row.days_on_market,
}


def apply_filter(rows: Sequence[DealRow], chosen: DealFilter, match: str = "yes") -> list[DealRow]:
    """Rows of one type match ("yes" or "unverified") that pass the filter, sorted."""
    needle = chosen.search.strip().lower()
    kept = [
        row
        for row in rows
        if row.type_match == match
        and (not chosen.states or row.state in chosen.states)
        and (not chosen.grades or row.grade in chosen.grades)
        and (row.in_buy_box or not chosen.buy_box_only)
        and (row.stage == 2 or not chosen.stage2_only)
        and (
            chosen.min_cash_flow is None
            or (
                row.cash_flow_door_month is not None
                and row.cash_flow_door_month >= chosen.min_cash_flow
            )
        )
        and (chosen.max_price is None or (row.price is not None and row.price <= chosen.max_price))
        and (not needle or needle in row.full_address.lower())
    ]
    key = SORTS.get(chosen.sort, SORTS["Cash flow per door"])
    known = [row for row in kept if key(row) is not None]
    unknown = [row for row in kept if key(row) is None]
    return sorted(known, key=key, reverse=chosen.descending) + unknown  # type: ignore[arg-type,return-value]


class CountySummary(BaseModel):
    county_fips: str
    name: str
    listings: int
    in_buy_box: int
    median_cash_flow_door: float
    median_cap_rate: float
    median_price_per_door: float


def county_summaries(rows: Sequence[DealRow]) -> list[CountySummary]:
    """Medians by county over the underwritten deals, biggest sample first."""
    groups: dict[str, list[DealRow]] = defaultdict(list)
    for row in rows:
        if row.county_fips and row.cash_flow_door_month is not None:
            groups[row.county_fips].append(row)
    found = [
        CountySummary(
            county_fips=fips,
            name=county_name(fips) or fips,
            listings=len(group),
            in_buy_box=sum(1 for row in group if row.in_buy_box),
            median_cash_flow_door=median(row.cash_flow_door_month or 0.0 for row in group),
            median_cap_rate=median(row.cap_rate or 0.0 for row in group),
            median_price_per_door=median(row.price_per_door or 0.0 for row in group),
        )
        for fips, group in groups.items()
    ]
    return sorted(found, key=lambda county: (-county.listings, county.name))


# ---- actions -------------------------------------------------------------------------------------


def set_star(engine: Engine, property_id: int, starred: bool) -> None:
    with session_scope(engine) as session:
        place = session.get_one(PropertyRow, property_id)
        place.starred = starred
        place.starred_at = utcnow() if starred else None


def set_hidden(engine: Engine, property_id: int, hidden: bool) -> None:
    with session_scope(engine) as session:
        session.get_one(PropertyRow, property_id).hidden = hidden


def hidden_properties(engine: Engine) -> list[tuple[int, str]]:
    with session_scope(engine) as session:
        rows = session.scalars(select(PropertyRow).where(PropertyRow.hidden.is_(True)))
        return [
            (row.id, ", ".join(p for p in (row.address_line1, row.city, row.state) if p))
            for row in rows
        ]


def parse_amounts(text: str, count: int) -> list[float | None] | None:
    """'1200, 1150, vacant, 1100' -> one amount per unit; None for a blank box.

    Raises ValueError with a message for the person when the text cannot be used.
    """
    if not text.strip():
        return None
    amounts: list[float | None] = []
    for token in text.replace("$", "").split(","):
        word = token.strip().lower()
        if word in ("", "vacant", "unknown", "?", "-"):
            amounts.append(None)
            continue
        try:
            amounts.append(float(word.replace(" ", "")))
        except ValueError as error:
            raise ValueError(f"'{token.strip()}' is not an amount.") from error
    if len(amounts) != count:
        raise ValueError(f"{len(amounts)} amounts were given for {count} units.")
    return amounts


def save_overrides(
    engine: Engine,
    config: Config,
    environment: Environment,
    property_id: int,
    listing_id: int,
    changes: dict[str, Any],
) -> str | None:
    """Store the user's figures (None removes one) and underwrite the listing again.
    Returns a problem message when the listing can no longer be underwritten."""
    with session_scope(engine) as session:
        for name, amount in changes.items():
            if name in FIELDS:
                set_override(session, property_id, name, amount)
    return reunderwrite(engine, config, environment, listing_id)


def remove_overrides(
    engine: Engine, config: Config, environment: Environment, property_id: int, listing_id: int
) -> str | None:
    with session_scope(engine) as session:
        clear_overrides(session, property_id)
    return reunderwrite(engine, config, environment, listing_id)


def overrides_for(engine: Engine, property_id: int) -> dict[str, Any]:
    with session_scope(engine) as session:
        return get_overrides(session, property_id)


def reunderwrite(
    engine: Engine, config: Config, environment: Environment, listing_id: int
) -> str | None:
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        return underwrite_listing(engine, config, environment, fetcher, listing_id)
    finally:
        fetcher.close()


def rerun_free(engine: Engine, config: Config, environment: Environment) -> FunnelReport:
    """Run the funnel with the free steps only: no RentCast requests, no Claude."""
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        return run_funnel(engine, config, environment, fetcher)
    finally:
        fetcher.close()


def listing_extras(engine: Engine, listing_id: int) -> dict[str, Any]:
    """Listing text, price history and the classifier's evidence, as plain data."""
    with session_scope(engine) as session:
        listing = session.get(ListingRow, listing_id)
        if listing is None:
            return {}
        classification = session.scalars(
            select(ClassificationRow).where(ClassificationRow.property_id == listing.property_id)
        ).first()
        return {
            "description": listing.description,
            "source": listing.source,
            "url": listing.url,
            "mls": " ".join(p for p in (listing.mls_name, listing.mls_number) if p) or None,
            "agent": ", ".join(
                p for p in (listing.agent_name, listing.office_name, listing.agent_phone) if p
            )
            or None,
            "listing_type": listing.listing_type,
            "status": listing.status,
            "first_seen": listing.first_seen_at,
            "last_seen": listing.last_seen_at,
            "history": [
                {
                    "When": event.observed_at.date().isoformat(),
                    "Event": event.event.replace("_", " "),
                    "Price": event.price,
                    "Was": event.previous_price,
                }
                for event in listing.price_events
            ],
            "signals": list(classification.signals) if classification else [],
            "tags": [str(tag) for tag in (classification.flags if classification else [])],
            "method": classification.method if classification else None,
        }


def budget_status(engine: Engine, config: Config) -> dict[str, Any]:
    budget = config.settings.budgets.rentcast
    guard = BudgetGuard(
        engine, "rentcast", cap=budget.monthly_request_cap, cycle_day=budget.billing_cycle_day
    )
    plan = config.settings.rentcast_plans.get(budget.plan)
    used = guard.used()
    return {
        "plan": budget.plan,
        "used": used,
        "cap": budget.monthly_request_cap,
        "left": guard.remaining(),
        "cost": plan.cost(used) if plan else None,
    }


# ---- settings ------------------------------------------------------------------------------------


def local_settings_path(config: Config) -> Path:
    return config.config_dir / LOCAL_SETTINGS_FILE


def save_local_settings(config: Config, changes: dict[str, Any]) -> Path:
    """Merge ``changes`` into settings.local.yaml, the personal file that overrides
    settings.yaml. The merged result is validated first, so a bad value is never written."""
    path = local_settings_path(config)
    current: dict[str, Any] = {}
    if path.is_file():
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    merged = deep_merge(current, changes)
    backup = path.read_text(encoding="utf-8") if path.is_file() else None
    path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    try:
        load_config(config.config_dir)
    except Exception:
        if backup is None:
            path.unlink()
        else:
            path.write_text(backup, encoding="utf-8")
        raise
    return path
