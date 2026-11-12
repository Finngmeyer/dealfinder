"""30-year fixed mortgage rate from FRED (series MORTGAGE30US, Freddie Mac's weekly survey).

Without a key the public CSV download is used. With FRED_API_KEY the JSON API is used.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "fred"
SERIES = "MORTGAGE30US"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_URL = "https://api.stlouisfed.org/fred/series/observations"


class MortgageRate(BaseModel):
    rate: float  # fraction: 0.0711 is 7.11%
    as_of: date
    source: str
    stale: bool = False


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None  # FRED writes "." for a week with no value


def parse_csv(text: str) -> tuple[date, float]:
    """Latest dated value in the CSV download: a header row, then ``date,value`` rows."""
    latest: tuple[date, float] | None = None
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        value = _number(row[1])
        if value is None:
            continue
        try:
            day = date.fromisoformat(row[0])
        except ValueError:
            continue
        if latest is None or day > latest[0]:
            latest = (day, value)
    if latest is None:
        raise SourceUnavailable(SOURCE, "the rate file had no usable rows")
    return latest


def parse_api(body: bytes) -> tuple[date, float]:
    try:
        observations = json.loads(body)["observations"]
        dated = [
            (date.fromisoformat(item["date"]), _number(item["value"])) for item in observations
        ]
    except (ValueError, KeyError, TypeError) as error:
        raise SourceUnavailable(SOURCE, "the API response was not in the expected form") from error
    usable = [(day, value) for day, value in dated if value is not None]
    if not usable:
        raise SourceUnavailable(SOURCE, "the API returned no usable observations")
    return max(usable)


def mortgage_rate(
    fetcher: Fetcher, *, api_key: str | None = None, max_age: timedelta = timedelta(days=1)
) -> MortgageRate:
    if api_key:
        fetched = fetcher.get(
            SOURCE,
            API_URL,
            params={"series_id": SERIES, "file_type": "json", "sort_order": "desc", "limit": "10"},
            secret_params={"api_key": api_key},
            max_age=max_age,
            validate=parse_api,
        )
        day, percent = parse_api(fetched.body)
        origin = "FRED API"
    else:
        fetched = fetcher.get(
            SOURCE,
            CSV_URL,
            params={"id": SERIES},
            max_age=max_age,
            validate=lambda body: parse_csv(body.decode("utf-8-sig", "replace")),
        )
        day, percent = parse_csv(fetched.text())
        origin = "FRED"
    return MortgageRate(
        rate=percent / 100, as_of=day, source=f"{origin} {SERIES}", stale=fetched.stale
    )
