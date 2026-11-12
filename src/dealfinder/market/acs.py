"""County property-tax rate and rental vacancy from the Census ACS 5-year estimates.

  * effective tax rate = median real estate taxes paid (B25103_001E) / median home value
    (B25077_001E)
  * rental vacancy rate = DP04_0005E

The Census API needs a key. Built from the published API format and the variable metadata,
which is readable without a key; not yet exercised against live data.

Median taxes are top-coded at "$10,000 or more" and median value at "$2,000,000 or more". In
those counties the median ratio would understate the rate, so the aggregate ratio (B25090_001E
/ B25082_001E) is used instead and the result says so.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "acs"
BASE_URL = "https://api.census.gov/data"
DETAIL_VARIABLES = ("B25103_001E", "B25077_001E", "B25090_001E", "B25082_001E")
PROFILE_VARIABLES = ("DP04_0005E",)
TAXES_TOP_CODE = 10_001
VALUE_TOP_CODE = 2_000_001


class CountyHousing(BaseModel):
    year: int  # last year of the 5-year window
    county_fips: str
    name: str
    median_taxes: float | None
    median_value: float | None
    aggregate_taxes: float | None
    aggregate_value: float | None
    rental_vacancy: float | None  # fraction
    stale: bool = False

    @property
    def vintage(self) -> str:
        return f"ACS {self.year - 4}-{self.year}"

    @property
    def top_coded(self) -> bool:
        return (self.median_taxes is not None and self.median_taxes >= TAXES_TOP_CODE) or (
            self.median_value is not None and self.median_value >= VALUE_TOP_CODE
        )

    def effective_tax_rate(self) -> tuple[float, str] | None:
        """(rate, how it was computed), or None when the county has no usable figures."""
        if not self.top_coded and self.median_taxes and self.median_value:
            return self.median_taxes / self.median_value, "median taxes / median value"
        if self.aggregate_taxes and self.aggregate_value:
            return self.aggregate_taxes / self.aggregate_value, "aggregate taxes / aggregate value"
        return None


def _value(cell: Any) -> float | None:
    """A numeric cell, or None for blanks and the large negative codes the ACS uses for
    "not available"."""
    try:
        number = float(cell)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_table(body: bytes, variables: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Census API tables are a list of rows with the header first. Returns county code ->
    {column: cell} for one state."""
    try:
        rows = json.loads(body)
        header = rows[0]
        columns = {name: header.index(name) for name in (*variables, "NAME", "state", "county")}
    except (ValueError, IndexError, TypeError) as error:
        raise SourceUnavailable(
            SOURCE, "the response was not a data table; check CENSUS_API_KEY"
        ) from error
    return {
        row[columns["state"]] + row[columns["county"]]: {
            name: row[index] for name, index in columns.items()
        }
        for row in rows[1:]
    }


def _state_table(
    fetcher: Fetcher,
    dataset: str,
    variables: tuple[str, ...],
    *,
    year: int,
    state_fips: str,
    key: str,
    max_age: timedelta,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """One request covers every county in the state, so a state is fetched once and cached."""
    fetched = fetcher.get(
        SOURCE,
        f"{BASE_URL}/{year}/{dataset}",
        params={
            "get": ",".join(("NAME", *variables)),
            "for": "county:*",
            "in": f"state:{state_fips}",
        },
        secret_params={"key": key},
        max_age=max_age,
        validate=lambda body: parse_table(body, variables),
    )
    return parse_table(fetched.body, variables), fetched.stale


def county_housing(
    fetcher: Fetcher,
    *,
    key: str | None,
    county_fips: str | None,
    year: int,
    max_age: timedelta = timedelta(days=365),
) -> CountyHousing:
    if not key:
        raise SourceUnavailable(SOURCE, "CENSUS_API_KEY is not set", missing_key=True)
    if county_fips is None:
        raise SourceUnavailable(SOURCE, "the county is unknown")
    state_fips = county_fips[:2]
    common = {"year": year, "state_fips": state_fips, "key": key, "max_age": max_age}
    detail, detail_stale = _state_table(fetcher, "acs/acs5", DETAIL_VARIABLES, **common)
    profile, profile_stale = _state_table(fetcher, "acs/acs5/profile", PROFILE_VARIABLES, **common)
    row = detail.get(county_fips)
    if row is None:
        raise SourceUnavailable(SOURCE, f"county {county_fips} is not in the {year} ACS tables")
    vacancy = _value(profile.get(county_fips, {}).get("DP04_0005E"))
    return CountyHousing(
        year=year,
        county_fips=county_fips,
        name=str(row["NAME"]),
        median_taxes=_value(row["B25103_001E"]),
        median_value=_value(row["B25077_001E"]),
        aggregate_taxes=_value(row["B25090_001E"]),
        aggregate_value=_value(row["B25082_001E"]),
        rental_vacancy=None if vacancy is None else vacancy / 100,
        stale=detail_stale or profile_stale,
    )
