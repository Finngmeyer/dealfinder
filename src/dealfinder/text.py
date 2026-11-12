"""Small text helpers shared by the analysis and the terminal output."""

from __future__ import annotations

from datetime import date


def short_date(day: date) -> str:
    """'Nov 12, 2026', built by hand because the no-padding day format differs by platform."""
    return f"{day:%b} {day.day}, {day.year}"


def money_list(values: list[float]) -> str:
    return ", ".join(f"${value:,.0f}" for value in values)
