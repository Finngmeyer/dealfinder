"""Import a CSV of listings: an agent's MLS export, a LoopNet download, a wholesaler's list."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import Engine

from dealfinder.adapters import csv_import
from dealfinder.models import Listing
from dealfinder.settings import Config
from dealfinder.store import repo
from dealfinder.store.db import session_scope


class CsvImportError(Exception):
    """The file cannot be imported as asked."""


class ImportReport(BaseModel):
    path: str
    mapping_name: str
    mapping_file: str
    mapping_created: bool  # guessed from the headers on this run
    mapping: csv_import.ColumnMapping
    unmapped_columns: list[str]
    dry_run: bool
    run_id: int | None = None
    rows: int = 0
    new_listings: int = 0
    updated_listings: int = 0
    new_properties: int = 0
    price_changes: int = 0
    skipped: list[tuple[int, str]] = Field(default_factory=list)  # (row number, reason)
    with_description: int = 0
    with_unit_count: int = 0

    def text(self) -> str:
        verb = "Would import" if self.dry_run else "Imported"
        lines = [
            f"{verb} {self.rows - len(self.skipped):,} of {self.rows:,} rows from {self.path}",
            f"Mapping '{self.mapping_name}' ({'guessed now' if self.mapping_created else 'saved'}"
            f"): {self.mapping_file}",
        ]
        if not self.dry_run:
            lines.append(
                f"New listings {self.new_listings:,}; updated {self.updated_listings:,}; "
                f"new properties {self.new_properties:,}; price changes {self.price_changes:,}."
            )
        lines.append(
            f"Rows with a description: {self.with_description:,}; with a unit count: "
            f"{self.with_unit_count:,}."
        )
        if self.unmapped_columns:
            lines.append("Columns not used: " + ", ".join(self.unmapped_columns))
        for number, reason in self.skipped[:20]:
            lines.append(f"  row {number} skipped: {reason}")
        if len(self.skipped) > 20:
            lines.append(f"  ... and {len(self.skipped) - 20} more skipped rows")
        return "\n".join(lines)


def import_csv(
    engine: Engine | None,
    config: Config,
    path: Path,
    mapping_name: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Import ``path`` using the mapping ``mapping_name``.

    When no mapping of that name exists one is guessed from the headers and, unless this is a
    dry run, saved for next time. A dry run reads and checks every row but stores nothing.
    """
    if not path.is_file():
        raise CsvImportError(f"No such file: {path}")
    try:
        mapping = csv_import.load_mapping(config.config_dir, mapping_name)
        mapping_file = csv_import.mapping_path(config.config_dir, mapping_name)
    except ValueError as error:
        raise CsvImportError(str(error)) from error
    headers = csv_import.read_headers(path)
    if not headers:
        raise CsvImportError(f"{path} has no header row")
    created = mapping is None
    if mapping is None:
        mapping = csv_import.suggest_mapping(headers, mapping_name)
    if "address_line1" not in mapping.columns:
        if created and not dry_run:
            csv_import.save_mapping(config.config_dir, mapping_name, mapping)
        raise CsvImportError(
            "No address column was recognized among: "
            + ", ".join(headers)
            + f". Add a line  address_line1: <your column>  under columns in {mapping_file}"
        )
    missing = [column for column in mapping.columns.values() if column not in headers]
    if missing:
        raise CsvImportError(
            f"The mapping {mapping_file} names columns this file does not have: "
            + ", ".join(missing)
        )
    if created and not dry_run:
        csv_import.save_mapping(config.config_dir, mapping_name, mapping)

    report = ImportReport(
        path=str(path),
        mapping_name=mapping_name,
        mapping_file=str(mapping_file),
        mapping_created=created,
        mapping=mapping,
        unmapped_columns=mapping.unmapped(headers),
        dry_run=dry_run,
    )
    listings: list[Listing] = []
    for number, outcome in csv_import.read_listings(path, mapping):
        report.rows += 1
        if isinstance(outcome, str):
            report.skipped.append((number, outcome))
            continue
        listings.append(outcome)
        report.with_description += bool(outcome.description)
        report.with_unit_count += outcome.unit_count is not None
    if dry_run or engine is None:
        return report

    rule = repo.DedupeRule(config.settings.dedupe.radius_m, config.settings.dedupe.price_tolerance)
    with session_scope(engine) as session:
        run = repo.start_run(
            session,
            "import",
            {"path": str(path), "mapping": mapping_name, "source": mapping.source},
        )
        for listing in listings:
            stored = repo.upsert_listing(session, listing, rule=rule, run_id=run.id)
            report.new_listings += stored.created
            report.updated_listings += not stored.created
            report.new_properties += stored.new_property
            report.price_changes += stored.price_changed
        report.run_id = run.id
        repo.finish_run(
            session,
            run,
            status="completed",
            stats=report.model_dump(mode="json", exclude={"mapping"}),
            report=report.text(),
        )
    return report
