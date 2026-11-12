"""FEMA National Risk Index: one composite natural-hazard rating per county.

FEMA's download page no longer serves scripted requests, so the data comes from FEMA's public
map service, a few thousand rows fetched in pages and cached. A CSV placed at
``<data dir>/nri_counties.csv`` (columns STCOFIPS, RISK_SCORE, RISK_RATNG) takes precedence,
for anyone who prefers to download the table by hand.
"""

from __future__ import annotations

import csv
import json
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "nri"
SERVICE_URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Counties/FeatureServer/0/query"
)
PAGE_SIZE = 2000
MAX_PAGES = 10
NO_RATING = "Insufficient Data"


class CountyRisk(BaseModel):
    county_fips: str
    score: float | None  # 0-100 percentile, higher is riskier
    rating: str  # "Very Low" ... "Very High"
    stale: bool = False

    def describe(self) -> str:
        return f"FEMA National Risk Index: {self.rating}"


def parse_page(body: bytes) -> list[dict[str, object]]:
    """Rows of one page. The service reports errors inside a 200 response."""
    try:
        data = json.loads(body)
        if "error" in data:
            message = data["error"].get("message", "unknown error")
            raise SourceUnavailable(SOURCE, f"the map service returned an error: {message}")
        return [feature["attributes"] for feature in data["features"]]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise SourceUnavailable(SOURCE, "the response was not in the expected form") from error


def _to_risk(row: dict[str, object]) -> CountyRisk | None:
    fips, rating = row.get("STCOFIPS"), row.get("RISK_RATNG")
    if not fips or not rating:
        return None
    score = row.get("RISK_SCORE")
    try:
        number = float(score) if score not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = None
    return CountyRisk(county_fips=str(fips).zfill(5), score=number, rating=str(rating))


def _from_csv(path: Path) -> dict[str, CountyRisk]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        risks = [_to_risk(dict(row)) for row in csv.DictReader(handle)]
    return {risk.county_fips: risk for risk in risks if risk is not None}


def all_counties(fetcher: Fetcher, max_age: timedelta) -> tuple[dict[str, CountyRisk], bool]:
    risks: dict[str, CountyRisk] = {}
    stale = False
    for page in range(MAX_PAGES):
        fetched = fetcher.get(
            SOURCE,
            SERVICE_URL,
            params={
                "where": "1=1",
                "outFields": "STCOFIPS,RISK_SCORE,RISK_RATNG",
                "returnGeometry": "false",
                "orderByFields": "STCOFIPS",
                "resultOffset": str(page * PAGE_SIZE),
                "resultRecordCount": str(PAGE_SIZE),
                "f": "json",
            },
            max_age=max_age,
            validate=parse_page,
        )
        stale = stale or fetched.stale
        rows = parse_page(fetched.body)
        for row in rows:
            risk = _to_risk(row)
            if risk is not None:
                risks[risk.county_fips] = risk
        if len(rows) < PAGE_SIZE:
            break
    if not risks:
        raise SourceUnavailable(SOURCE, "the map service returned no counties")
    return risks, stale


def county_risk(
    fetcher: Fetcher,
    *,
    county_fips: str | None,
    manual_csv: Path | None = None,
    max_age: timedelta = timedelta(days=180),
) -> CountyRisk:
    if county_fips is None:
        raise SourceUnavailable(SOURCE, "the county is unknown")
    if manual_csv is not None and manual_csv.is_file():
        risks, stale = _from_csv(manual_csv), False
    else:
        risks, stale = all_counties(fetcher, max_age)
    risk = risks.get(county_fips)
    if risk is None or risk.rating == NO_RATING:
        raise SourceUnavailable(SOURCE, "FEMA has no rating for this county")
    return risk.model_copy(update={"stale": stale})
