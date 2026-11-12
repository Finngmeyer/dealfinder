"""Read stored results back as flat rows: for the report command, the dashboard and exports."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dealfinder.settings import Config
from dealfinder.store.tables import (
    ListingRow,
    PropertyRow,
    ScreenRow,
    UnderwritingRow,
    UnitRow,
)


class DealRow(BaseModel):
    """One underwritten listing under one loan scenario."""

    listing_id: int
    property_id: int
    scenario: str
    stage: int  # 1: free data only, 2: enriched
    address: str
    city: str | None
    state: str | None
    zip_code: str | None
    county_fips: str | None
    latitude: float | None
    longitude: float | None
    kind: str
    units: int | None
    unit_confidence: str | None
    type_match: str  # yes | unverified
    price: float | None
    price_per_door: float | None
    cash_flow_door_month: float | None
    coc: float | None
    dscr: float | None
    cap_rate: float | None
    mao: float | None
    score: float | None
    grade: str | None
    in_buy_box: bool
    buy_box_misses: list[str]
    passed_stage1: bool | None
    rents_stated: int  # units whose rent the listing states (actual figures)
    rents_estimated: int  # units priced from a market estimate
    year_built: int | None
    days_on_market: int | None
    listed_date: date | None
    source: str
    url: str | None
    flags: int  # warnings and red flags raised while underwriting
    in_flood_zone: bool | None
    starred: bool
    computed_at: datetime

    @property
    def full_address(self) -> str:
        return ", ".join(
            part for part in (self.address, self.city, self.state, self.zip_code) if part
        )


class ProblemRow(BaseModel):
    """A listing of a wanted type that could not be underwritten, and why."""

    listing_id: int
    property_id: int
    address: str
    city: str | None
    state: str | None
    price: float | None
    kind: str
    units: int | None
    unit_confidence: str | None
    source: str
    problem: str


def deal_rows(session: Session, config: Config, scenario: str | None = None) -> list[DealRow]:
    """Every stored result for one scenario (default: the ranking scenario), best cash flow
    per door first."""
    name = scenario or config.settings.buy_box.ranking_scenario
    stated = (
        select(UnitRow.property_id, func.count(UnitRow.in_place_rent).label("stated"))
        .where(UnitRow.vacant.is_(False))
        .group_by(UnitRow.property_id)
        .subquery()
    )
    query = (
        select(UnderwritingRow, ScreenRow, ListingRow, PropertyRow, stated.c.stated)
        .join(ScreenRow, ScreenRow.listing_id == UnderwritingRow.listing_id)
        .join(ListingRow, ListingRow.id == UnderwritingRow.listing_id)
        .join(PropertyRow, PropertyRow.id == UnderwritingRow.property_id)
        .outerjoin(stated, stated.c.property_id == PropertyRow.id)
        .where(UnderwritingRow.scenario == name)
        .order_by(UnderwritingRow.cash_flow_door_month.desc().nulls_last(), UnderwritingRow.id)
    )
    rows: list[DealRow] = []
    for result, screen, listing, place, stated_count in session.execute(query):
        units = place.unit_count
        price = screen.price if screen.price is not None else listing.price
        rows.append(
            DealRow(
                listing_id=listing.id,
                property_id=place.id,
                scenario=result.scenario,
                stage=result.stage,
                address=place.address_line1 or "(no street address)",
                city=place.city,
                state=place.state,
                zip_code=place.zip_code,
                county_fips=place.county_fips,
                latitude=place.latitude,
                longitude=place.longitude,
                kind=place.property_kind,
                units=units,
                unit_confidence=place.unit_confidence,
                type_match=screen.type_match,
                price=price,
                price_per_door=price / units if price and units else None,
                cash_flow_door_month=result.cash_flow_door_month,
                coc=result.coc,
                dscr=result.dscr,
                cap_rate=result.cap_rate,
                mao=result.mao,
                score=result.score,
                grade=result.grade,
                in_buy_box=bool(result.in_buy_box),
                buy_box_misses=[str(miss) for miss in result.buy_box_misses or []],
                passed_stage1=screen.passed_stage1,
                rents_stated=int(stated_count or 0),
                rents_estimated=max(0, (units or 0) - int(stated_count or 0)),
                year_built=place.year_built,
                days_on_market=listing.days_on_market,
                listed_date=listing.listed_date,
                source=listing.source,
                url=listing.url,
                flags=sum(1 for flag in screen.flags or [] if flag.get("level") != "info"),
                in_flood_zone=place.in_flood_zone,
                starred=place.starred,
                computed_at=result.computed_at,
            )
        )
    return rows


def problem_rows(session: Session) -> list[ProblemRow]:
    query = (
        select(ScreenRow, ListingRow, PropertyRow)
        .join(ListingRow, ListingRow.id == ScreenRow.listing_id)
        .join(PropertyRow, PropertyRow.id == ScreenRow.property_id)
        .where(ScreenRow.stage == 0)
        .order_by(ListingRow.price.asc().nulls_last(), ListingRow.id)
    )
    return [
        ProblemRow(
            listing_id=listing.id,
            property_id=place.id,
            address=place.address_line1 or "(no street address)",
            city=place.city,
            state=place.state,
            price=listing.price,
            kind=place.property_kind,
            units=place.unit_count,
            unit_confidence=place.unit_confidence,
            source=listing.source,
            problem=screen.note or "",
        )
        for screen, listing, place in session.execute(query)
    ]


def scenario_names(session: Session) -> list[str]:
    """Scenarios that have stored results."""
    return sorted(session.scalars(select(UnderwritingRow.scenario).distinct()))


def deal_detail(session: Session, listing_id: int) -> dict[str, Any] | None:
    """Everything stored about one listing: the screening record, every scenario's results,
    the units, and the listing itself."""
    screen = session.scalars(select(ScreenRow).where(ScreenRow.listing_id == listing_id)).first()
    listing = session.get(ListingRow, listing_id)
    if screen is None or listing is None:
        return None
    place = listing.property
    scenarios = session.scalars(
        select(UnderwritingRow)
        .where(UnderwritingRow.listing_id == listing_id)
        .order_by(UnderwritingRow.id)
    ).all()
    units = session.scalars(
        select(UnitRow).where(UnitRow.property_id == place.id).order_by(UnitRow.position)
    ).all()
    return {
        "listing": listing,
        "property": place,
        "screen": screen,
        "scenarios": {row.scenario: row for row in scenarios},
        "units": list(units),
    }
