"""The user's own figures for a property. They replace whatever the listing or the market
data says, on every later run, until they are removed."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dealfinder.store.tables import OverrideRow

# Request field -> what to call it. Only these can be overridden.
FIELDS: dict[str, str] = {
    "price": "Offer price",
    "rents": "Current rent per unit",
    "market_rents": "Market rent per unit",
    "year_built": "Year built",
    "tax_bill": "Last annual tax bill",
    "tax_rate": "Property tax rate",
    "vacancy_rate": "Vacancy",
    "management": "Management (self or manager)",
    "insurance_per_unit_year": "Insurance per unit per year",
    "utilities_per_unit_month": "Owner-paid utilities per unit per month",
    "tenants_pay_utilities": "Tenants pay utilities",
    "hoa_month": "HOA dues per month",
    "in_flood_zone": "In a flood zone",
    "flood_insurance_year": "Flood insurance per year",
    "rehab": "Rehab budget",
    "credits": "Seller credits",
    "closing_cost_pct": "Closing costs",
    "rate_quote": "Interest rate quote",
}
# Where a field shows up among the input lines, when the name differs.
LINE_KEYS = {"hoa_month": "hoa", "in_flood_zone": "flood_insurance"}


def get_overrides(session: Session, property_id: int) -> dict[str, Any]:
    rows = session.scalars(select(OverrideRow).where(OverrideRow.property_id == property_id))
    return {row.field: row.value.get("value") for row in rows if row.field in FIELDS}


def set_override(
    session: Session, property_id: int, field: str, value: Any, note: str | None = None
) -> None:
    """Store the user's figure for one field. None removes it."""
    if field not in FIELDS:
        raise ValueError(f"'{field}' cannot be overridden. Choose from: {', '.join(FIELDS)}.")
    row = session.scalars(
        select(OverrideRow).where(
            OverrideRow.property_id == property_id, OverrideRow.field == field
        )
    ).first()
    if value is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        row = OverrideRow(property_id=property_id, field=field)
        session.add(row)
    row.value = {"value": value}
    row.note = note


def clear_overrides(session: Session, property_id: int) -> None:
    session.execute(delete(OverrideRow).where(OverrideRow.property_id == property_id))
