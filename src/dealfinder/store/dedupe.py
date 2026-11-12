"""Recognize the same property across sources.

Two listings are the same property when their normalized addresses match, or, failing that,
when they sit within a few tens of meters of each other and their prices are close. Distance
alone is not enough: neighboring row houses are that close.
"""

from __future__ import annotations

import math
import re

_SUFFIXES = {
    "ALLEY": "ALY",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "CIRCLE": "CIR",
    "COURT": "CT",
    "COVE": "CV",
    "CROSSING": "XING",
    "DRIVE": "DR",
    "EXPRESSWAY": "EXPY",
    "EXTENSION": "EXT",
    "FREEWAY": "FWY",
    "HEIGHTS": "HTS",
    "HIGHWAY": "HWY",
    "LANE": "LN",
    "PARKWAY": "PKWY",
    "PLACE": "PL",
    "POINT": "PT",
    "ROAD": "RD",
    "SQUARE": "SQ",
    "STREET": "ST",
    "TERRACE": "TER",
    "TRAIL": "TRL",
    "TURNPIKE": "TPKE",
}
_DIRECTIONS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}
_UNIT_WORDS = frozenset(
    {"APT", "APARTMENT", "UNIT", "UNITS", "STE", "SUITE", "BLDG", "BUILDING", "FL", "FLOOR", "RM"}
)
_NOT_ALPHANUMERIC = re.compile(r"[^A-Z0-9#/\- ]+")
_EARTH_RADIUS_M = 6_371_000.0
_CELL_DEGREES = 0.001  # about 111 m north-south, less east-west


def _street_tokens(line: str) -> tuple[list[str], list[str]]:
    """(street tokens, unit tokens) from one address line."""
    text = _NOT_ALPHANUMERIC.sub(" ", line.upper().replace(".", "").replace(",", " "))
    tokens = text.replace("#", " # ").split()
    street: list[str] = []
    unit: list[str] = []
    for index, token in enumerate(tokens):
        if token == "#" or (token in _UNIT_WORDS and index > 0):
            unit = [t for t in tokens[index + 1 :] if t != "#"]
            break
        street.append(_DIRECTIONS.get(token, _SUFFIXES.get(token, token)))
    return street, unit


def normalize_address(
    line1: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
    *,
    line2: str | None = None,
    keep_unit: bool = False,
) -> str | None:
    """A key that is equal for two spellings of one address, or None without a street line.

    '123 North Main Street, Apt 2' and '123 N Main St #2' both give '123 N MAIN ST|43215'.
    The unit is dropped unless ``keep_unit`` is set, because a multifamily building is one
    property however a source writes its unit range; condos keep it.
    """
    if not line1 or not line1.strip():
        return None
    street, unit = _street_tokens(line1)
    if not street:
        return None
    if keep_unit:
        if line2 and line2.strip():
            _, unit_from_line2 = _street_tokens("X " + line2)
            unit = unit_from_line2 or line2.upper().split()
        if unit:
            street = [*street, "UNIT", *unit]
    zip5 = (zip_code or "").strip()[:5]
    if len(zip5) == 5 and zip5.isdigit():
        place = zip5
    elif city and state:
        place = f"{city.strip().upper()},{state.strip().upper()}"
    else:
        return None
    return f"{' '.join(street)}|{place}"


def geo_cell(latitude: float, longitude: float) -> str:
    """The grid square a point falls in. Points within one square of each other are neighbors."""
    return f"{round(latitude / _CELL_DEGREES)}:{round(longitude / _CELL_DEGREES)}"


def neighbor_cells(latitude: float, longitude: float) -> list[str]:
    """The point's own grid square and the eight around it."""
    row, column = round(latitude / _CELL_DEGREES), round(longitude / _CELL_DEGREES)
    return [f"{row + dy}:{column + dx}" for dy in (-1, 0, 1) for dx in (-1, 0, 1)]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    half_dphi = (phi2 - phi1) / 2
    half_dlambda = math.radians(lon2 - lon1) / 2
    a = math.sin(half_dphi) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(half_dlambda) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def prices_close(first: float | None, second: float | None, tolerance: float) -> bool:
    """True when both prices are known and within ``tolerance`` of each other."""
    if not first or not second:
        return False
    return abs(first - second) <= tolerance * max(first, second)
