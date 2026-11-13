"""One run of everything, in order: fetch, classify, underwrite, alert.

This is what the scheduler calls. Each step reports for itself, and a step that cannot run
(no key, budget spent, source down) says so and lets the rest go ahead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import Engine

from dealfinder.adapters.rentcast import RentCastSource
from dealfinder.alerts import run_alerts
from dealfinder.analysis import AnalysisError
from dealfinder.classify.llm import Extractor
from dealfinder.http import Fetcher
from dealfinder.models import ListingSource
from dealfinder.pipeline.classify import classify_all
from dealfinder.pipeline.ingest import ingest
from dealfinder.pipeline.sweep import Mode, SweepError, run_sweep, sweep_states
from dealfinder.pipeline.underwrite import run_funnel
from dealfinder.settings import Config, Environment
from dealfinder.store.usage import BudgetGuard


class Step(BaseModel):
    name: str
    text: str
    ok: bool = True


class RunReport(BaseModel):
    mode: str
    started: datetime
    steps: list[Step] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def text(self) -> str:
        lines = [f"Dealfinder {self.mode} run, {self.started:%Y-%m-%d %H:%M}"]
        for step in self.steps:
            lines += ["", f"== {step.name} ==", step.text]
        return "\n".join(lines)


def run_all(
    engine: Engine,
    config: Config,
    environment: Environment,
    fetcher: Fetcher,
    *,
    mode: Mode,
    states: Sequence[str] | None = None,
    rentcast: RentCastSource | None = None,
    guard: BudgetGuard | None = None,
    extractor: Extractor | None = None,
    scrapers: Sequence[ListingSource] = (),
    sweep: bool = True,
    alerts: bool = True,
    now: datetime | None = None,
) -> RunReport:
    """``rentcast`` and ``guard`` are None when there is no RentCast key; the sweep and the
    paid lookups are then skipped with a note."""
    settings = config.settings
    report = RunReport(mode=mode, started=now or datetime.now())

    if not sweep:
        report.steps.append(Step(name="Listings", text="Sweep skipped, as asked."))
    elif not settings.sources["rentcast"].enabled:
        report.steps.append(
            Step(name="Listings", text="RentCast is switched off in settings; no sweep.")
        )
    elif rentcast is None or guard is None:
        report.steps.append(
            Step(
                name="Listings",
                text="RENTCAST_API_KEY is not set, so no listings were fetched. Lists you "
                "import with `dealfinder import-csv` are still underwritten below.",
            )
        )
    else:
        try:
            chosen = sweep_states(config, list(states) if states else None)
            swept = run_sweep(
                engine,
                config,
                rentcast,
                guard,
                mode=mode,
                states=chosen,
                types=settings.buy_box.property_types,
            )
            report.steps.append(
                Step(name="Listings", text=swept.text(), ok=swept.status != "failed")
            )
        except SweepError as error:
            report.steps.append(Step(name="Listings", text=str(error), ok=False))

    for scraper in scrapers:  # only the ones switched on in settings are passed in
        fetched = ingest(
            engine,
            config,
            scraper,
            sweep_states(config, list(states) if states else None),
            settings.buy_box.property_types,
        )
        report.steps.append(Step(name=f"Scraper: {scraper.name}", text=fetched.text()))

    report.steps.append(Step(name="Classify", text=classify_all(engine, config).text()))

    try:
        funnel = run_funnel(
            engine, config, environment, fetcher, rentcast=rentcast, extractor=extractor
        )
        report.steps.append(Step(name="Underwrite", text=funnel.text()))
    except AnalysisError as error:
        report.steps.append(Step(name="Underwrite", text=str(error), ok=False))

    if not alerts:
        report.steps.append(Step(name="Alerts", text="Alerts skipped, as asked."))
    else:
        sent = run_alerts(engine, config, environment, fetcher, send=True)
        report.steps.append(Step(name="Alerts", text=sent.text()))
    return report


def write_log(report: RunReport, log_dir: Path) -> Path:
    """Append the report to this month's log file and return its path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"runs-{report.started:%Y-%m}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(report.text() + "\n\n")
    return path
