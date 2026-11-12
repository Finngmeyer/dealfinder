"""Synthetic records shaped like the real sources' data. Nothing here is real listing data."""

from __future__ import annotations

from typing import Any

from dealfinder.models import Listing


def rentcast_record(number: int, **changes: Any) -> dict[str, Any]:
    """One sale listing as RentCast returns it. ``number`` makes the address unique."""
    record: dict[str, Any] = {
        "id": f"{100 + number}-Sample-St,-Columbus,-OH-43215",
        "formattedAddress": f"{100 + number} Sample St, Columbus, OH 43215",
        "addressLine1": f"{100 + number} Sample St",
        "addressLine2": None,
        "city": "Columbus",
        "state": "OH",
        "stateFips": "39",
        "zipCode": "43215",
        "county": "Franklin",
        "countyFips": "049",
        "latitude": 39.96 + number * 0.001,
        "longitude": -83.0 - number * 0.001,
        "propertyType": "Multi-Family",
        "bedrooms": 8,
        "bathrooms": 4,
        "squareFootage": 3600,
        "lotSize": 6000,
        "yearBuilt": 1978,
        "hoa": {"fee": 0},
        "status": "Active",
        "price": 400_000 + number * 1_000,
        "listingType": "Standard",
        "listedDate": "2026-10-01T00:00:00.000Z",
        "removedDate": None,
        "createdDate": "2026-10-01T00:00:00.000Z",
        "lastSeenDate": "2026-11-12T10:00:00.000Z",
        "daysOnMarket": 42,
        "mlsName": "SampleMLS",
        "mlsNumber": f"S{number:05d}",
        "listingAgent": {"name": "Sam Sample", "phone": "5550100"},
        "listingOffice": {"name": "Sample Realty"},
        "history": {
            "2026-10-01": {"event": "Sale Listing", "price": 400_000 + number * 1_000},
        },
    }
    record.update(changes)
    return record


def listing(number: int = 1, **changes: Any) -> Listing:
    """A canonical listing with a unique address per ``number``."""
    fields: dict[str, Any] = {
        "source": "rentcast",
        "source_listing_id": f"listing-{number}",
        "status": "active",
        "price": 400_000.0,
        "address_line1": f"{100 + number} Sample St",
        "city": "Columbus",
        "state": "OH",
        "zip_code": "43215",
        "county_fips": "39049",
        "latitude": 39.96 + number * 0.01,
        "longitude": -83.0,
        "property_kind": "multifamily_2_4",
        "property_type_raw": "Multi-Family",
        "bedrooms": 8.0,
        "bathrooms": 4.0,
    }
    fields.update(changes)
    return Listing(**fields)


def rentcast_property(number: int, **changes: Any) -> dict[str, Any]:
    """One property record as RentCast returns it, for the listing ``rentcast_record(number)``."""
    record: dict[str, Any] = {
        "id": f"{100 + number}-Sample-St,-Columbus,-OH-43215",
        "formattedAddress": f"{100 + number} Sample St, Columbus, OH 43215",
        "propertyType": "Multi-Family",
        "bedrooms": 8,
        "bathrooms": 4,
        "squareFootage": 3600,
        "yearBuilt": 1978,
        "features": {"unitCount": 4, "heating": True},
        "propertyTaxes": {
            "2024": {"year": 2024, "total": 5510},
            "2025": {"year": 2025, "total": 5760},
        },
        "ownerOccupied": False,
    }
    record.update(changes)
    return record
