"""Your own listing files: MLS exports from an agent, LoopNet downloads, wholesaler lists.

A column mapping says which column holds which field. The first time a file layout is seen a
mapping is guessed from the headers and saved under ``config/csv_mappings/<name>.yaml``, where
it can be corrected by hand; later imports with the same name reuse it.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from dealfinder.models import CallEstimate, Geo, Listing, ListingStatus, PropertyKind
from dealfinder.settings import PropertyType
from dealfinder.store.dedupe import normalize_address

# Canonical field -> header spellings seen in the wild, lower-case with punctuation removed.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "source_listing_id": (
        "mls #",
        "mls number",
        "mls id",
        "mls no",
        "listing id",
        "listing number",
        "list number",
        "listing #",
        "id",
    ),
    "address_line1": (
        "address",
        "street address",
        "property address",
        "full address",
        "street",
        "address line 1",
        "site address",
    ),
    "address_line2": ("unit", "unit number", "address line 2", "apt"),
    "city": ("city", "town", "municipality"),
    "state": ("state", "st", "state or province"),
    "zip_code": ("zip", "zip code", "zipcode", "postal code", "postal"),
    "price": ("price", "list price", "asking price", "current price", "listing price", "ask"),
    "unit_count": (
        "units",
        "# units",
        "number of units",
        "total units",
        "unit count",
        "# of units",
        "num units",
        "no of units",
        "number of units total",
    ),
    "property_type_raw": (
        "property type",
        "type",
        "property sub type",
        "property subtype",
        "style",
        "sub type",
    ),
    "bedrooms": ("beds", "bedrooms", "total bedrooms", "br", "bedrooms total", "bed"),
    "bathrooms": ("baths", "bathrooms", "total bathrooms", "ba", "bathrooms total", "bath"),
    "building_sqft": (
        "sqft",
        "square feet",
        "living area",
        "building sqft",
        "sq ft",
        "total sqft",
        "gross building area",
        "building size",
        "living area sqft",
    ),
    "lot_sqft": ("lot size", "lot sqft", "lot square feet", "lot size sqft"),
    "year_built": ("year built", "yr built", "built"),
    "description": (
        "remarks",
        "public remarks",
        "description",
        "comments",
        "listing remarks",
        "marketing remarks",
        "property description",
        "notes",
    ),
    "tax_annual": (
        "taxes",
        "annual taxes",
        "tax amount",
        "property taxes",
        "tax annual amount",
        "annual tax amount",
    ),
    "hoa_fee_month": ("hoa", "hoa fee", "association fee", "hoa dues", "monthly hoa"),
    "listed_date": ("list date", "listing date", "listed", "date listed", "on market date"),
    "days_on_market": ("dom", "days on market", "cdom", "cumulative days on market"),
    "status": ("status", "listing status", "standard status"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng", "long"),
    "mls_name": ("mls", "mls name", "source mls"),
    "agent_name": ("agent", "listing agent", "agent name", "list agent", "list agent full name"),
    "agent_phone": ("agent phone", "listing agent phone", "list agent phone"),
    "office_name": ("office", "listing office", "brokerage", "list office name"),
    "url": ("url", "link", "listing url", "web link"),
    "listing_type": ("sale type", "listing type", "special listing conditions"),
}
_NUMERIC = frozenset(
    {"price", "bedrooms", "bathrooms", "building_sqft", "lot_sqft", "tax_annual", "hoa_fee_month",
     "latitude", "longitude"}
)  # fmt: skip
_WHOLE = frozenset({"unit_count", "year_built", "days_on_market"})
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y", "%d-%b-%Y")
_UNITS_TO_KIND: dict[int, PropertyKind] = {1: "sfr", 2: "duplex", 3: "triplex", 4: "fourplex"}
_TYPE_WORDS: tuple[tuple[str, PropertyKind], ...] = (
    ("fourplex", "fourplex"),
    ("four plex", "fourplex"),
    ("4 plex", "fourplex"),
    ("quadplex", "fourplex"),
    ("quadruplex", "fourplex"),
    ("triplex", "triplex"),
    ("duplex", "duplex"),
    ("apartment", "multifamily_5plus"),
    ("5+", "multifamily_5plus"),
    ("multi", "multifamily_2_4"),
    ("2 4", "multifamily_2_4"),
    ("condo", "condo"),
    ("town", "townhouse"),
    ("single", "sfr"),
    ("sfr", "sfr"),
    ("detached", "sfr"),
    ("manufactured", "manufactured"),
    ("mobile", "manufactured"),
    ("land", "land"),
    ("lot", "land"),
)
_STATUS_WORDS: tuple[tuple[str, ListingStatus], ...] = (
    ("active under", "pending"),
    ("pending", "pending"),
    ("contingent", "pending"),
    ("under contract", "pending"),
    ("sold", "sold"),
    ("closed", "sold"),
    ("active", "active"),
    ("for sale", "active"),
    ("coming soon", "active"),
    ("withdrawn", "off_market"),
    ("expired", "off_market"),
    ("cancel", "off_market"),
    ("off market", "off_market"),
)


class ColumnMapping(BaseModel):
    """Which column of a file holds which field."""

    model_config = ConfigDict(extra="forbid")

    source: str = "csv"  # stored with every listing from this layout, as "csv:<source>"
    columns: dict[str, str]  # canonical field -> column header in the file
    defaults: dict[str, str] = Field(default_factory=dict)  # used when a cell is blank

    def unmapped(self, headers: Sequence[str]) -> list[str]:
        used = set(self.columns.values())
        return [header for header in headers if header not in used]


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#+ ]+", " ", text.lower())).strip()


def suggest_mapping(headers: Sequence[str], source: str) -> ColumnMapping:
    """Guess a mapping from the headers. Each column is used for at most one field."""
    plain = {header: _plain(header) for header in headers}
    columns: dict[str, str] = {}
    taken: set[str] = set()
    for field, names in SYNONYMS.items():
        for name in names:
            match = next((h for h, p in plain.items() if p == name and h not in taken), None)
            if match is not None:
                columns[field] = match
                taken.add(match)
                break
    return ColumnMapping(source=source, columns=columns)


def mapping_path(config_dir: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"'{name}' is not a usable mapping name: use letters, digits, _ . -")
    return config_dir / "csv_mappings" / f"{name}.yaml"


def load_mapping(config_dir: Path, name: str) -> ColumnMapping | None:
    path = mapping_path(config_dir, name)
    if not path.is_file():
        return None
    return ColumnMapping.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def save_mapping(config_dir: Path, name: str, mapping: ColumnMapping) -> Path:
    path = mapping_path(config_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Column mapping for CSV imports. Left side: the field. Right side: the column header\n"
        "# in your file. Fix anything guessed wrong; add a line under defaults for a value the\n"
        "# file leaves out, for example  state: OH\n"
    )
    path.write_text(header + yaml.safe_dump(mapping.model_dump(), sort_keys=False), "utf-8")
    return path


def read_headers(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return next(csv.reader(handle), [])


def _number(text: str) -> float | None:
    cleaned = re.sub(r"[,$\s]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date(text: str) -> date | None:
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip()[:20], pattern).date()
        except ValueError:
            continue
    return None


def _kind(type_text: str | None, unit_count: int | None) -> PropertyKind:
    """What the row is. A stated unit count beats the wording of the type column."""
    if unit_count is not None and unit_count >= 1:
        return _UNITS_TO_KIND.get(unit_count, "multifamily_5plus")
    plain = _plain(type_text or "")
    return next((kind for word, kind in _TYPE_WORDS if word in plain), "other")


def _status(text: str | None) -> ListingStatus:
    plain = _plain(text or "")
    if not plain:
        return "active"  # a list of things for sale, unless it says otherwise
    return next((status for word, status in _STATUS_WORDS if word in plain), "unknown")


def row_to_listing(row: dict[str, str], mapping: ColumnMapping) -> Listing:
    """Translate one CSV row. Raises ValueError with a reason when the row cannot be used."""
    values: dict[str, Any] = {}
    for field in SYNONYMS:
        column = mapping.columns.get(field)
        text = (row.get(column) or "").strip() if column else ""
        text = text or mapping.defaults.get(field, "")
        if not text:
            continue
        if field in _NUMERIC:
            values[field] = _number(text)
        elif field in _WHOLE:
            number = _number(text)
            values[field] = None if number is None else int(number)
        elif field == "listed_date":
            values[field] = _date(text)
        else:
            values[field] = text

    line1 = values.get("address_line1")
    if not line1:
        raise ValueError("no street address")
    state = values.get("state")
    if state:
        values["state"] = state.upper()[:2]
    zip_code = values.get("zip_code")
    if zip_code:
        values["zip_code"] = zip_code.split("-")[0].zfill(5)[:5]
    key = normalize_address(line1, values.get("city"), values.get("state"), values.get("zip_code"))
    if key is None:
        raise ValueError("no ZIP code or city and state to place the address")

    listing_id = values.pop("source_listing_id", None)
    if not listing_id:
        listing_id = "addr-" + hashlib.sha256(key.encode()).hexdigest()[:16]
    type_text = values.get("property_type_raw")
    return Listing(
        source=f"csv:{mapping.source}",
        source_listing_id=str(listing_id),
        status=_status(values.pop("status", None)),
        property_kind=_kind(type_text, values.get("unit_count")),
        raw=dict(row),
        **values,
    )


def read_listings(path: Path, mapping: ColumnMapping) -> Iterator[tuple[int, Listing | str]]:
    """(row number, listing) for each usable row, or (row number, reason) for one that is not.
    Row numbers match a spreadsheet: the header is row 1."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=2):
            if not any((value or "").strip() for value in row.values()):
                continue  # blank line
            try:
                yield number, row_to_listing(row, mapping)
            except ValueError as error:
                yield number, str(error)


class CsvSource:
    """A CSV file as a listing source. Nothing is billed and nothing is fetched."""

    env_vars: tuple[str, ...] = ()
    is_scraper = False

    def __init__(self, path: Path, mapping: ColumnMapping) -> None:
        self.name = f"csv:{mapping.source}"
        self._path = path
        self._mapping = mapping

    def estimate_calls(self, geo: Geo, types: Sequence[PropertyType]) -> CallEstimate:
        return CallEstimate(calls=0, known=True, note="a local file")

    def search(
        self, geo: Geo, types: Sequence[PropertyType], since: date | None
    ) -> Iterator[dict[str, Any]]:
        with self._path.open(newline="", encoding="utf-8-sig") as handle:
            yield from csv.DictReader(handle)

    def to_listing(self, raw: dict[str, Any]) -> Listing:
        return row_to_listing(raw, self._mapping)
