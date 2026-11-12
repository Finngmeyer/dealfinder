"""ZIP code to county and state, from Census data bundled with the package (no network)."""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, ConfigDict

CONTIGUOUS_STATES = frozenset(
    [
        "AL",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
    ]
)

_ZIP = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")


class Place(BaseModel):
    model_config = ConfigDict(frozen=True)

    zip_code: str
    state: str | None
    county_fips: str | None
    county_name: str | None
    approximate: bool  # True when only the 3-digit ZIP prefix matched, so the county is unknown

    @property
    def in_contiguous_us(self) -> bool | None:
        return None if self.state is None else self.state in CONTIGUOUS_STATES

    def describe(self) -> str:
        if self.state is None:
            return f"{self.zip_code} (location unknown)"
        if self.county_name is None:
            return f"{self.zip_code}, {self.state} (county unknown)"
        return f"{self.zip_code} {self.county_name}, {self.state}"


def _rows(name: str) -> list[dict[str, str]]:
    text = resources.files("dealfinder.data").joinpath(name).read_text(encoding="utf-8")
    return list(csv.DictReader(text.splitlines()))


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], dict[str, tuple[str, str]], dict[str, str]]:
    counties = {row["county_fips"]: (row["state"], row["name"]) for row in _rows("counties.csv")}
    zip_to_county = {row["zip"]: row["county_fips"] for row in _rows("zip_county.csv")}
    by_prefix: dict[str, Counter[str]] = defaultdict(Counter)
    for zip_code, fips in zip_to_county.items():
        by_prefix[zip_code[:3]][counties[fips][0]] += 1
    prefix_to_state = {prefix: votes.most_common(1)[0][0] for prefix, votes in by_prefix.items()}
    return zip_to_county, counties, prefix_to_state


def normalize_zip(value: str) -> str | None:
    """Five-digit ZIP from '43215' or '43215-1234'; None when it is not a ZIP."""
    match = _ZIP.match(value)
    return match.group(1) if match else None


def lookup_zip(value: str) -> Place | None:
    """County and state for a ZIP, or None when the text is not a ZIP code.

    ZIPs the Census Bureau does not map (mostly PO-box ZIPs) fall back to the state that owns
    the 3-digit prefix, with ``approximate`` set.
    """
    zip_code = normalize_zip(value)
    if zip_code is None:
        return None
    zip_to_county, counties, prefix_to_state = _tables()
    fips = zip_to_county.get(zip_code)
    if fips is not None:
        state, name = counties[fips]
        return Place(
            zip_code=zip_code, state=state, county_fips=fips, county_name=name, approximate=False
        )
    return Place(
        zip_code=zip_code,
        state=prefix_to_state.get(zip_code[:3]),
        county_fips=None,
        county_name=None,
        approximate=True,
    )
