"""Small text helpers shared by the analysis and the terminal output."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date


def short_date(day: date) -> str:
    """'Nov 12, 2026', built by hand because the no-padding day format differs by platform."""
    return f"{day:%b} {day.day}, {day.year}"


def money_list(values: Sequence[float | None]) -> str:
    """'$1,100, $1,100, unknown' for a list of amounts, some of which may be missing."""
    return ", ".join("unknown" if value is None else f"${value:,.0f}" for value in values)
