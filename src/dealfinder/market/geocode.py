"""Coordinates for a street address, from the Census Bureau's geocoder.

Free and keyless. Used for listings that arrive without coordinates (most CSV imports), which
the map and the flood lookup both need. An address the geocoder cannot place is "not found",
remembered like any other answer so it is not asked about again.
"""

from __future__ import annotations

import json
from datetime import timedelta

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "census_geocoder"
URL = "https://geocoding.geo.census.gov/geocoder/locations/address"
BENCHMARK = "Public_AR_Current"


class Coordinates(BaseModel):
    latitude: float
    longitude: float
    matched_address: str | None = None


def parse_match(body: bytes) -> Coordinates | None:
    """The first match, or None when the geocoder found nothing."""
    try:
        data = json.loads(body)
        matches = data["result"]["addressMatches"]
    except (ValueError, KeyError, TypeError) as error:
        raise SourceUnavailable(SOURCE, "the response had an unexpected shape") from error
    if not isinstance(matches, list):
        raise SourceUnavailable(SOURCE, "the response had an unexpected shape")
    for match in matches:
        try:
            point = match["coordinates"]
            return Coordinates(
                latitude=float(point["y"]),
                longitude=float(point["x"]),
                matched_address=match.get("matchedAddress"),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return None


def geocode(
    fetcher: Fetcher,
    *,
    street: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
    max_age: timedelta | None = None,
) -> Coordinates | None:
    """Coordinates for an address, or None when the Census cannot place it."""
    if not street or not (zip_code or (city and state)):
        raise SourceUnavailable(SOURCE, "the address is incomplete")
    params = {"street": street, "benchmark": BENCHMARK, "format": "json"}
    for name, value in (("city", city), ("state", state), ("zip", zip_code)):
        if value:
            params[name] = value
    fetched = fetcher.get(SOURCE, URL, params=params, max_age=max_age, validate=parse_match)
    return parse_match(fetched.body)
