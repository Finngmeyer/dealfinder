"""Store what a listing source returns. Used for the optional scraper sources, which have no
pages or checkpoints: each search is fetched whole and its listings are stored as they come.

Nothing is ever marked off the market here: a scrape that returns fewer listings proves
nothing about the ones it did not return.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine

from dealfinder.http import SourceUnavailable
from dealfinder.models import Geo, ListingSource
from dealfinder.settings import Config, PropertyType
from dealfinder.store import repo
from dealfinder.store.db import session_scope
from dealfinder.store.usage import BudgetExceeded

_BATCH = 100


class IngestReport(BaseModel):
    source: str
    seen: int = 0
    new_listings: int = 0
    updated: int = 0
    new_properties: int = 0
    price_changes: int = 0
    skipped: int = 0  # records that could not be read as a listing
    stopped: str = ""

    def text(self) -> str:
        lines = [
            f"{self.source}: {self.seen:,} listings seen; new {self.new_listings:,}, updated "
            f"{self.updated:,}, new properties {self.new_properties:,}, price changes "
            f"{self.price_changes:,}."
        ]
        if self.skipped:
            lines.append(f"{self.skipped:,} records could not be read and were skipped.")
        if self.stopped:
            lines.append(f"Stopped early: {self.stopped}")
        return "\n".join(lines)


def ingest(
    engine: Engine,
    config: Config,
    source: ListingSource,
    states: Sequence[str],
    types: Sequence[PropertyType],
    since: date | None = None,
) -> IngestReport:
    """Search ``source`` for each state and store what comes back. A source problem stops the
    run and is reported; what was stored before it stays."""
    rule = repo.DedupeRule(
        radius_m=config.settings.dedupe.radius_m,
        price_tolerance=config.settings.dedupe.price_tolerance,
    )
    report = IngestReport(source=source.name)
    batch: list[dict] = []

    def flush() -> None:
        if not batch:
            return
        with session_scope(engine) as session:
            for raw in batch:
                try:
                    listing = source.to_listing(raw)
                except (ValueError, ValidationError, KeyError, TypeError):
                    report.skipped += 1
                    continue
                stored = repo.upsert_listing(session, listing, rule=rule)
                report.seen += 1
                report.new_listings += stored.created
                report.updated += not stored.created
                report.new_properties += stored.new_property
                report.price_changes += stored.price_changed
        batch.clear()

    try:
        for state in states:
            for raw in source.search(Geo(state=state), types, since):
                batch.append(raw)
                if len(batch) >= _BATCH:
                    flush()
    except BudgetExceeded as stop:
        report.stopped = str(stop)
    except SourceUnavailable as problem:
        report.stopped = f"{problem.reason}."
    flush()
    return report
