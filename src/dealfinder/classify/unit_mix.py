"""The units of a building: what is known about each, or an even split when nothing is."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

MixConfidence = Literal["high", "low"]


class UnitDetail(BaseModel):
    beds: float | None = None
    baths: float | None = None
    sqft: float | None = None
    in_place_rent: float | None = None  # monthly rent stated in the listing: an actual figure
    rent_quote: str | None = None  # the listing's words the rent came from
    vacant: bool = False


def split_whole(total: float | None, units: int) -> list[float | None]:
    """Split a count evenly in whole numbers; any remainder goes to the first units.

    Seven bedrooms over four units gives 2, 2, 2, 1.
    """
    if total is None or units < 1:
        return [None] * max(units, 0)
    base, remainder = divmod(int(total), units)
    return [float(base + (index < remainder)) for index in range(units)]


def split_baths(total: float | None, units: int) -> list[float | None]:
    """Like ``split_whole`` for bathrooms: full baths are split, and a leftover half bath goes
    to the first unit. 4.5 baths over four units gives 1.5, 1, 1, 1."""
    if total is None or units < 1:
        return [None] * max(units, 0)
    halves = round(total * 2)
    shares = split_whole(halves // 2, units)
    if halves % 2:
        shares[0] = (shares[0] or 0.0) + 0.5
    return shares


def even_mix(
    units: int, beds: float | None, baths: float | None, sqft: float | None
) -> list[UnitDetail]:
    """Units with the building's totals split evenly. Used when the listing gives no per-unit
    detail, and always marked low confidence by the caller."""
    return [
        UnitDetail(beds=b, baths=ba, sqft=s)
        for b, ba, s in zip(
            split_whole(beds, units),
            split_baths(baths, units),
            split_whole(sqft, units),
            strict=True,
        )
    ]
