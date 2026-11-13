"""Sweeps: fetch listings state by state, store them, and record what changed.

  * weekly  every active listing in each state. A state that was fetched completely has its
            missing listings marked off-market.
  * daily   only listings that came on in the last few days. Nothing is marked off-market.
  * probe   the first page of each state, to count the inventory. The page's listings are
            stored, so the request is not wasted.

A run is idempotent and resumable. Each page is committed together with a checkpoint, so a run
that stops (the budget ran out, the connection dropped, the laptop went to sleep) picks up at
the next page when the same command is run again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Engine

from dealfinder.adapters.rentcast import RentCastSource, raw_types
from dealfinder.geo import CONTIGUOUS_STATES
from dealfinder.http import SourceUnavailable
from dealfinder.models import Geo
from dealfinder.settings import Config, PropertyType
from dealfinder.store import repo
from dealfinder.store.db import session_scope
from dealfinder.store.tables import RunRow, utcnow
from dealfinder.store.usage import BudgetExceeded, BudgetGuard

Mode = Literal["daily", "weekly", "probe"]
_RUNS_PER_MONTH = {"daily": 365 / 12, "weekly": 52 / 12, "probe": 0.0}
_COUNTERS = ("seen", "new_listings", "new_properties", "price_changes", "relisted", "off_market")


class SweepError(Exception):
    """The sweep cannot start as asked."""


def sweep_states(config: Config, requested: Sequence[str] | None) -> list[str]:
    """States to sweep: the ones asked for, else the buy box's, else all 48 plus DC."""
    chosen = list(requested or [])
    if not chosen:
        box = config.settings.buy_box.states
        chosen = sorted(CONTIGUOUS_STATES) if box == "all" else list(box)
    states = [state.strip().upper() for state in chosen if state.strip()]
    outside = [state for state in states if state not in CONTIGUOUS_STATES]
    if outside:
        raise SweepError(
            f"{', '.join(outside)} not covered: this tool sweeps the 48 contiguous states and DC."
        )
    return sorted(set(states))


# ---- planning ----------------------------------------------------------------------------------


class PlanRow(BaseModel):
    state: str
    calls: int
    known: bool
    note: str


class SweepPlan(BaseModel):
    """What a sweep is expected to cost, before anything is fetched."""

    mode: Mode
    types: list[str]
    rows: list[PlanRow]
    calls: int
    all_known: bool
    budget_used: int
    budget_cap: int | None
    monthly_calls: float
    monthly_cost: dict[str, float]  # plan name -> dollars a month at this cadence

    @property
    def remaining(self) -> int | None:
        return None if self.budget_cap is None else max(0, self.budget_cap - self.budget_used)

    def states_that_fit(self) -> int:
        """How many states, in order, fit in what is left of the budget."""
        if self.remaining is None:
            return len(self.rows)
        left, count = self.remaining, 0
        for row in self.rows:
            if row.calls > left:
                break
            left -= row.calls
            count += 1
        return count


def plan_sweep(
    source: RentCastSource,
    guard: BudgetGuard,
    config: Config,
    *,
    mode: Mode,
    states: Sequence[str],
    types: Sequence[PropertyType],
) -> SweepPlan:
    rows: list[PlanRow] = []
    for state in states:
        if mode == "weekly":
            estimate = source.estimate_calls(Geo(state=state), types)
            rows.append(
                PlanRow(state=state, calls=estimate.calls, known=estimate.known, note=estimate.note)
            )
        else:
            note = (
                "first page only"
                if mode == "probe"
                else "new listings only, assumed to fit one page"
            )
            rows.append(PlanRow(state=state, calls=1, known=mode == "probe", note=note))
    calls = sum(row.calls for row in rows)
    monthly = calls * _RUNS_PER_MONTH[mode]
    return SweepPlan(
        mode=mode,
        types=list(types),
        rows=rows,
        calls=calls,
        all_known=all(row.known for row in rows),
        budget_used=guard.used(),
        budget_cap=guard.cap,
        monthly_calls=monthly,
        monthly_cost={
            name: plan.cost(round(monthly)) for name, plan in config.settings.rentcast_plans.items()
        },
    )


# ---- running -----------------------------------------------------------------------------------


class ScopeResult(BaseModel):
    """What happened for one state."""

    state: str
    scope: str
    status: Literal["completed", "done_earlier", "stopped_budget", "failed", "not_reached"] = (
        "not_reached"
    )
    requests: int = 0  # billed requests made now
    cached_pages: int = 0  # pages reread from disk for free
    seen: int = 0
    new_listings: int = 0
    new_properties: int = 0
    price_changes: int = 0
    relisted: int = 0
    off_market: int = 0
    total_reported: int | None = None
    complete: bool | None = None  # weekly: whether every listing RentCast counted was fetched
    detail: str = ""


class SweepReport(BaseModel):
    run_id: int
    mode: Mode
    resumed: bool
    status: Literal["completed", "stopped_budget", "failed"]
    scopes: list[ScopeResult]
    requests: int
    budget_used: int
    budget_cap: int | None
    message: str

    def totals(self) -> dict[str, int]:
        return {name: sum(getattr(scope, name) for scope in self.scopes) for name in _COUNTERS}

    def text(self) -> str:
        """The run report as plain text, also stored with the run."""
        totals = self.totals()
        cap = "no cap" if self.budget_cap is None else f"{self.budget_cap}"
        lines = [
            f"Run {self.run_id}: {self.mode} sweep, {self.status.replace('_', ' ')}"
            + (" (resumed)" if self.resumed else ""),
            self.message,
            f"Requests billed now: {self.requests}. This billing period: {self.budget_used} of "
            f"{cap}.",
            f"Listings seen {totals['seen']:,}; new {totals['new_listings']:,}; "
            f"price changes {totals['price_changes']:,}; relisted {totals['relisted']:,}; "
            f"off-market {totals['off_market']:,}.",
        ]
        for scope in self.scopes:
            lines.append(
                f"  {scope.state}: {scope.status.replace('_', ' ')}, {scope.seen:,} seen, "
                f"{scope.new_listings:,} new, {scope.requests} billed, "
                f"{scope.cached_pages} from cache" + (f". {scope.detail}" if scope.detail else "")
            )
        return "\n".join(lines)


def _is_complete(fetched: int, total: int | None, tolerance: float) -> bool:
    """Whether the pages fetched cover what RentCast said was there. Results reshuffle while
    paging, so a small shortfall is allowed. Without a count the last short page is the proof."""
    return total is None or fetched >= total * (1 - tolerance)


def run_sweep(
    engine: Engine,
    config: Config,
    source: RentCastSource,
    guard: BudgetGuard,
    *,
    mode: Mode,
    states: Sequence[str],
    types: Sequence[PropertyType],
    fresh: bool = False,
    now: Callable[[], datetime] = utcnow,
) -> SweepReport:
    """Run or resume a sweep. Never raises for a budget stop or a source failure: both end the
    run early with the reason in the report, and the same command resumes it."""
    settings = config.settings
    rule = repo.DedupeRule(settings.dedupe.radius_m, settings.dedupe.price_tolerance)
    params: dict[str, Any] = {
        "source": source.name,
        "states": list(states),
        "types": sorted(types),
        "scope_filter": source.scope(Geo(state="*"), types),
    }
    with session_scope(engine) as session:
        run = None if fresh else repo.resumable_run(session, mode, params)
        resumed = run is not None
        if run is None:
            run = repo.start_run(session, mode, params, started_at=now())
        run.status = "running"
        run_id = run.id
        started = run.started_at
    guard.run_id = run_id

    # The window is anchored to when the run began, so a resumed run asks the same question.
    since = (
        (started - timedelta(days=settings.sweep.daily_days_old)).date()
        if mode == "daily"
        else None
    )
    types_raw = raw_types(types)
    results = [
        ScopeResult(state=state, scope=source.scope(Geo(state=state), types)) for state in states
    ]
    status: Literal["completed", "stopped_budget", "failed"] = "completed"
    message = "Every state finished."

    for result in results:
        geo = Geo(state=result.state)
        with session_scope(engine) as session:
            mark = repo.checkpoint(session, run_id, source.name, result.scope)
            start_offset, done = mark.next_offset, mark.completed
            result.total_reported = mark.total
        if done:
            result.status = "done_earlier"
            continue
        try:
            pages = source.pages(
                geo,
                types,
                since,
                start_offset=start_offset,
                max_pages=1 if mode == "probe" else None,
            )
            for page in pages:
                with session_scope(engine) as session:
                    for raw in page.items:
                        stored = repo.upsert_listing(
                            session, source.to_listing(raw), rule=rule, run_id=run_id, now=now()
                        )
                        result.seen += 1
                        result.new_listings += stored.created
                        result.new_properties += stored.new_property
                        result.price_changes += stored.price_changed
                        result.relisted += stored.relisted
                    mark = repo.checkpoint(session, run_id, source.name, result.scope)
                    mark.next_offset = page.offset + settings.sweep.page_size
                    mark.fetched += len(page.items)
                    if page.total is not None:
                        mark.total = page.total
                        result.total_reported = page.total
                if page.from_cache:
                    result.cached_pages += 1
                else:
                    result.requests += 1
        except BudgetExceeded as stop:
            result.status = "stopped_budget"
            status, message = "stopped_budget", f"Stopped at {result.state}: {stop}"
            break
        except SourceUnavailable as problem:
            result.status = "failed"
            result.detail = problem.reason
            status = "failed"
            message = f"Stopped at {result.state}: {problem.reason}. Run it again to resume."
            break

        with session_scope(engine) as session:
            mark = repo.checkpoint(session, run_id, source.name, result.scope)
            mark.completed = True
            if mode != "daily" and mark.total is not None:
                repo.record_inventory(session, source.name, result.scope, mark.total)
            if mode == "weekly":
                result.complete = _is_complete(
                    mark.fetched, mark.total, settings.sweep.completeness_tolerance
                )
                if result.complete:
                    result.off_market = repo.mark_off_market(
                        session,
                        source=source.name,
                        state=result.state,
                        raw_types=types_raw,
                        run_id=run_id,
                        after_missed=settings.sweep.missed_sweeps_before_off_market,
                        now=now(),
                    )
                else:
                    result.detail = (
                        f"only {mark.fetched:,} of {mark.total:,} listings came back, so nothing "
                        "was marked off-market"
                    )
        result.status = "completed"

    report = SweepReport(
        run_id=run_id,
        mode=mode,
        resumed=resumed,
        status=status,
        scopes=results,
        requests=sum(result.requests for result in results),
        budget_used=guard.used(),
        budget_cap=guard.cap,
        message=message,
    )
    with session_scope(engine) as session:
        run = session.get_one(RunRow, run_id)
        repo.finish_run(
            session, run, status=status, stats=report.model_dump(mode="json"), report=report.text()
        )
    return report
