"""Zillow Observed Rent Index (ZORI): a typical asking rent and its growth over a year.

Used as a cross-check on rent estimates and to show rent growth. It is one figure per area for
all unit sizes, so it is never used as a unit's rent. The ZIP file covers roughly a quarter of
ZIP codes; the county file fills in for the rest.

Data: Zillow Research public CSVs.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "zori"
BASE_URL = "https://files.zillowstatic.com/research/public_csvs/zori"
ZIP_URL = f"{BASE_URL}/Zip_zori_uc_sfrcondomfr_sm_month.csv"
COUNTY_URL = f"{BASE_URL}/County_zori_uc_sfrcondomfr_sm_month.csv"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RentIndex(BaseModel):
    geography: Literal["zip", "county"]
    region: str
    level: float  # dollars per month
    as_of: date
    yoy_growth: float | None  # None when there is no value twelve months back
    stale: bool = False

    def describe(self) -> str:
        return f"Zillow ZORI, {self.geography} {self.region}, {self.as_of:%b %Y}"


def _check_header(body: bytes) -> None:
    first_line = body[:4096].decode("utf-8-sig", "replace").splitlines()[0]
    if "RegionName" not in first_line or not any(_DATE.match(c) for c in first_line.split(",")):
        raise SourceUnavailable(SOURCE, "the file is not a ZORI table")


def _series(header: list[str], row: list[str]) -> tuple[float, date, float | None] | None:
    """Latest value, its month, and growth against twelve months earlier."""
    points = [
        (date.fromisoformat(name), float(cell))
        for name, cell in zip(header, row, strict=False)
        if _DATE.match(name) and cell.strip()
    ]
    if not points:
        return None
    latest_day, latest = points[-1]
    year_ago = next(
        (
            value
            for day, value in points
            if (day.year, day.month) == (latest_day.year - 1, latest_day.month)
        ),
        None,
    )
    return latest, latest_day, (latest / year_ago - 1) if year_ago else None


def find_zip(text: str, zip_code: str) -> RentIndex | None:
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    column = header.index("RegionName")
    for row in reader:
        if row[column].strip().zfill(5) == zip_code:
            series = _series(header, row)
            if series is None:
                return None
            level, as_of, growth = series
            return RentIndex(
                geography="zip", region=zip_code, level=level, as_of=as_of, yoy_growth=growth
            )
    return None


def find_county(text: str, county_fips: str) -> RentIndex | None:
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    name, state = header.index("RegionName"), header.index("State")
    state_code, county_code = header.index("StateCodeFIPS"), header.index("MunicipalCodeFIPS")
    for row in reader:
        if row[state_code].strip().zfill(2) + row[county_code].strip().zfill(3) == county_fips:
            series = _series(header, row)
            if series is None:
                return None
            level, as_of, growth = series
            return RentIndex(
                geography="county",
                region=f"{row[name]}, {row[state]}",
                level=level,
                as_of=as_of,
                yoy_growth=growth,
            )
    return None


def rent_index(
    fetcher: Fetcher,
    *,
    zip_code: str,
    county_fips: str | None,
    max_age: timedelta = timedelta(days=30),
) -> RentIndex:
    fetched = fetcher.get(SOURCE, ZIP_URL, max_age=max_age, validate=_check_header)
    found = find_zip(fetched.text(), zip_code)
    if found is None and county_fips is not None:
        fetched = fetcher.get(SOURCE, COUNTY_URL, max_age=max_age, validate=_check_header)
        found = find_county(fetched.text(), county_fips)
    if found is None:
        raise SourceUnavailable(SOURCE, "Zillow has no rent index for this ZIP or its county")
    return found.model_copy(update={"stale": fetched.stale})
