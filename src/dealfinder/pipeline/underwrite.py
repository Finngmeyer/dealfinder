"""The two-stage funnel: underwrite, enrich the ones worth it, score and rank.

Stage 1 runs on every active listing of a wanted property type, with free data only, under the
ranking scenario. A listing survives when its cash flow per door or its rent-to-price ratio
clears the bar in ``funnel.stage1``.

Stage 2 runs on the survivors that sit inside the buy box's states and price, best first. It
spends what is scarce: RentCast requests for the property record (unit count, tax bill) and a
rent estimate per unit layout, a FEMA flood lookup, a Census geocode when the listing has no
coordinates, and optionally Claude reading an unclear description. Then the listing is
underwritten again under every loan preset.

Last, every result is scored against the medians of its county and stored.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from statistics import median
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, selectinload

from dealfinder.adapters.rentcast import RentCastSource
from dealfinder.analysis import (
    AnalysisError,
    AnalysisResult,
    AnalyzeRequest,
    Provenance,
    analyze,
)
from dealfinder.classify.classifier import kind_matches
from dealfinder.classify.llm import Extractor
from dealfinder.geo import CONTIGUOUS_STATES
from dealfinder.http import Fetcher, SourceUnavailable
from dealfinder.market import flood, geocode
from dealfinder.market.context import MarketContext, load_market
from dealfinder.pipeline.classify import RECORD_KEY, apply_record, read_with_claude
from dealfinder.score import ScoreInputs, score
from dealfinder.settings import Config, Environment, Stage1
from dealfinder.store import dedupe
from dealfinder.store.db import session_scope
from dealfinder.store.overrides import LINE_KEYS, get_overrides
from dealfinder.store.tables import (
    ClassificationRow,
    ListingRow,
    PropertyRow,
    ScreenRow,
    UnderwritingRow,
    UnitRow,
    utcnow,
)
from dealfinder.store.usage import BudgetExceeded
from dealfinder.underwrite import ScenarioResult
from dealfinder.underwrite.mao import failing_targets

_BATCH = 200


class Unscreenable(Exception):
    """A listing that cannot be underwritten, with the reason in plain words."""


def source_label(source: str) -> str:
    if source == "rentcast":
        return "RentCast listing"
    if source.startswith("csv:"):
        return f"your import '{source[4:]}'"
    return source


class MarketCache:
    """Market data by ZIP, loaded once per run."""

    def __init__(self, config: Config, environment: Environment, fetcher: Fetcher) -> None:
        self._config, self._environment, self._fetcher = config, environment, fetcher
        self._by_zip: dict[str, MarketContext] = {}

    def get(self, zip_code: str | None) -> MarketContext | None:
        if not zip_code:
            return None
        if zip_code not in self._by_zip:
            self._by_zip[zip_code] = load_market(
                zip_code, self._config, self._environment, self._fetcher
            )
        return self._by_zip[zip_code]


# ---- from stored rows to a request ---------------------------------------------------------------


def wanted_states(config: Config) -> frozenset[str]:
    box = config.settings.buy_box.states
    return CONTIGUOUS_STATES if box == "all" else frozenset(state.upper() for state in box)


def candidates(session: Session, config: Config) -> list[tuple[int, str]]:
    """(listing id, type match) for the listings to underwrite: active, of a wanted property
    type or possibly one, and one listing per property (the one seen most recently)."""
    box = config.settings.buy_box
    rows = session.execute(
        select(
            ListingRow.id,
            ListingRow.property_id,
            PropertyRow.property_kind,
            PropertyRow.unit_confidence,
        )
        .join(PropertyRow)
        .where(ListingRow.status == "active", PropertyRow.hidden.is_(False))
        .order_by(ListingRow.last_seen_at.desc(), ListingRow.id)
    )
    chosen: dict[int, tuple[int, str]] = {}
    for listing_id, property_id, kind, confidence in rows:
        match = kind_matches(kind, confidence, box.property_types, box.min_unit_confidence)
        if match != "no" and property_id not in chosen:
            chosen[property_id] = (listing_id, match)
    return sorted(chosen.values())


def build_request(
    listing: ListingRow,
    place: PropertyRow,
    units: Sequence[UnitRow],
    tags: Sequence[str],
    config: Config,
    overrides: dict[str, Any],
    scenarios: list[str],
) -> AnalyzeRequest:
    """What is known about a stored listing, as an analysis request, with where each figure
    came from. The user's own figures replace anything here."""
    settings = config.settings
    source = source_label(listing.source)
    price = overrides.get("price", listing.price)
    if price is None or price <= 0:
        raise Unscreenable("The listing has no price.")
    if not units:
        raise Unscreenable(
            "The unit count is not known. Confirm it with `dealfinder resolve-units`, or set it "
            "by importing a list that states it."
        )

    notes: dict[str, Provenance] = {"price": Provenance(source=f"list price, {source}")}
    fields: dict[str, Any] = {
        "price": listing.price,
        "rents": [None if unit.vacant else unit.in_place_rent for unit in units],
        "zip_code": place.zip_code or "",
        "year_built": place.year_built,
        "building_sqft": place.building_sqft or None,
        "tax_bill": listing.tax_annual,
        "hoa_month": listing.hoa_fee_month or 0.0,
        "in_flood_zone": place.in_flood_zone,
        "scenarios": scenarios,
    }
    if any(rent is not None for rent in fields["rents"]):
        notes["rents"] = Provenance(source="stated in the listing")
    if all(unit.beds is not None for unit in units):
        fields["beds"] = [unit.beds for unit in units]
    if all(unit.market_rent is not None for unit in units):
        fields["market_rents"] = [unit.market_rent for unit in units]
        origins = sorted({unit.market_rent_source or "rent estimate" for unit in units})
        notes["market_rents"] = Provenance(source="; ".join(origins), kind="estimated")
    if place.year_built is not None:
        notes["year_built"] = Provenance(source=source)
    if listing.tax_annual is not None:
        record = (listing.raw_json or {}).get(RECORD_KEY) or {}
        from_record = record.get("tax_annual") == listing.tax_annual and record.get("tax_year")
        origin = f"county record via RentCast, {record['tax_year']}" if from_record else source
        notes["tax_bill"] = Provenance(source=f"{origin}; taxes never go below it")
    if listing.hoa_fee_month:
        notes["hoa"] = Provenance(source=source)
    if place.in_flood_zone:
        notes["flood_insurance"] = Provenance(
            source=f"placeholder: FEMA flood zone {place.flood_zone or 'unnamed'}",
            kind="estimated",
            flagged=True,
        )

    level = settings.classify.rehab_level(list(tags))
    per_unit = settings.underwriting.rehab_per_unit.get(level, 0.0)
    if level != "none" and per_unit > 0:
        causes = [tag for tag in settings.classify.rehab_by_flag.get(level, []) if tag in tags]
        fields["rehab"] = per_unit * len(units)
        notes["rehab"] = Provenance(
            source=f"{level} rehab at ${per_unit:,.0f} a unit, from the listing's tags "
            f"({', '.join(tag.replace('_', ' ') for tag in causes)})",
            kind="estimated",
            flagged=True,
        )

    for name, value in overrides.items():
        fields[name] = value
        notes[LINE_KEYS.get(name, name)] = Provenance(source="your figure", kind="override")
    try:
        return AnalyzeRequest(**fields, provenance=notes)
    except ValidationError as error:
        problem = error.errors()[0]
        where = ".".join(str(part) for part in problem["loc"]) or "the request"
        raise Unscreenable(f"A figure is not usable ({where}): {problem['msg']}.") from error


def _money(amount: float) -> str:
    return f"-${-amount:,.0f}" if amount < 0 else f"${amount:,.0f}"


def stage1_verdict(cash_flow_door: float, rent_to_price: float, rule: Stage1) -> tuple[bool, str]:
    """(kept, why). A listing is kept when either bar is cleared; a bar set to null is off,
    and with both off everything is kept."""
    floor, ratio = rule.min_cash_flow_per_door_month, rule.min_rent_to_price
    facts = f"cash flow {_money(cash_flow_door)} a door, rent {rent_to_price:.2%} of price"
    if floor is None and ratio is None:
        return True, f"Kept: {facts} (stage 1 has no bars set)."
    by_cash = floor is not None and cash_flow_door >= floor
    by_rent = ratio is not None and rent_to_price >= ratio
    bars = " or ".join(
        text
        for text in (
            None if floor is None else f"{_money(floor)} a door",
            None if ratio is None else f"{ratio:.2%} of price",
        )
        if text
    )
    return (
        by_cash or by_rent,
        f"{'Kept' if by_cash or by_rent else 'Dropped'}: {facts}; needs {bars}.",
    )


def price_cut(listing: ListingRow) -> float | None:
    """Total cut from the first asking price seen, as a share; None when it has not fallen."""
    first = next((event.price for event in listing.price_events if event.price), None)
    if not first or not listing.price or listing.price >= first:
        return None
    return 1 - listing.price / first


def buy_box_misses(
    scenario: ScenarioResult,
    listing: ListingRow,
    place: PropertyRow,
    match: str,
    config: Config,
) -> list[str]:
    """Why a deal is outside the buy box; empty when it is inside."""
    box = config.settings.buy_box
    misses: list[str] = []
    if match != "yes":
        misses.append("unit count not confirmed")
    if place.state and place.state not in wanted_states(config):
        misses.append(f"{place.state} is not one of the buy box's states")
    if box.max_price is not None and (listing.price or 0) > box.max_price:
        misses.append(f"price above ${box.max_price:,.0f}")
    misses.extend(
        f"misses the {name} target" for name in failing_targets(scenario.kpis, box.targets)
    )
    return misses


def score_inputs(
    result: AnalysisResult,
    scenario: ScenarioResult,
    listing: ListingRow,
    tags: Sequence[str],
    confidence: str | None,
    config: Config,
) -> ScoreInputs:
    """Everything the score needs except the county medians, which are only known once every
    listing has been underwritten."""
    settings = config.settings
    kpis = scenario.kpis
    risk = result.market.risk if result.market else None
    flood_known = result.request.in_flood_zone is not None
    has_rents = any(rent is not None for rent in result.request.rents)
    has_market = any(unit.market_rent is not None for unit in result.deal.units)
    county = result.place.county_name if result.place and result.place.county_name else None
    return ScoreInputs(
        cash_flow_door_month=kpis.cash_flow_door_month,
        coc=kpis.coc,
        dscr=kpis.dscr,
        has_debt=kpis.annual_debt_service > 0,
        cap_rate=kpis.cap_rate,
        price_per_door=kpis.price_per_door,
        county_name=county or "the county",
        days_on_market=listing.days_on_market,
        price_cut_pct=price_cut(listing),
        tags=list(tags),
        listing_type=listing.listing_type,
        rent_upside=kpis.rent_upside if has_rents and has_market else None,
        in_flood_zone=bool(result.request.in_flood_zone)
        if flood_known
        else settings.when_unknown.in_flood_zone,
        flood_checked=flood_known,
        high_hazard=(risk.rating in settings.market.high_risk_ratings)
        if risk is not None
        else settings.when_unknown.high_hazard_county,
        hazard_checked=risk is not None,
        confidence=confidence if confidence in ("high", "medium", "low") else "low",  # type: ignore[arg-type]
    )


# ---- storing -------------------------------------------------------------------------------------


@dataclass
class _Outcome:
    """What one pass over a listing produced, kept light enough to hold for every listing."""

    listing_id: int
    county: str | None = None
    cap_rate: float | None = None
    price_per_door: float | None = None
    cash_flow_door: float | None = None
    passed: bool | None = None
    eligible: bool = False  # for stage 2: passed stage 1 and inside the buy box's area and price
    problem: str | None = None


def _screen_row(session: Session, listing: ListingRow) -> ScreenRow:
    row = session.scalars(select(ScreenRow).where(ScreenRow.listing_id == listing.id)).first()
    if row is None:
        row = ScreenRow(listing_id=listing.id, property_id=listing.property_id)
        session.add(row)
    return row


def _store_problem(session: Session, listing: ListingRow, match: str, problem: str) -> None:
    row = _screen_row(session, listing)
    row.stage, row.passed_stage1, row.note = 0, None, problem
    row.type_match, row.confidence, row.price = (
        match,
        listing.property.unit_confidence,
        listing.price,
    )
    row.request, row.inputs, row.flags, row.details = {}, [], [], {}
    row.computed_at = utcnow()
    session.execute(delete(UnderwritingRow).where(UnderwritingRow.listing_id == listing.id))


def _store_result(
    session: Session,
    listing: ListingRow,
    match: str,
    result: AnalysisResult,
    tags: Sequence[str],
    config: Config,
    *,
    stage: int,
    passed: bool,
    note: str,
    enrichment: dict[str, Any] | None = None,
) -> None:
    place = listing.property
    row = _screen_row(session, listing)
    row.stage, row.passed_stage1, row.note = stage, passed, note
    row.type_match, row.confidence, row.price = match, place.unit_confidence, result.request.price
    row.request = result.request.model_dump(mode="json")
    row.inputs = [line.model_dump(mode="json") for line in result.inputs]
    row.flags = [flag.model_dump(mode="json") for flag in result.flags]
    row.details = {
        "statement": result.statement.model_dump(mode="json"),
        "rates": {name: info.model_dump(mode="json") for name, info in result.rates.items()},
        "skipped": result.skipped,
        "attributions": result.attributions,
        "market_sources": [
            status.model_dump(mode="json")
            for status in (result.market.sources if result.market else [])
        ],
        "tags": list(tags),
    }
    if enrichment is not None:
        row.enrichment = enrichment
    row.computed_at = utcnow()

    existing = {
        stored.scenario: stored
        for stored in session.scalars(
            select(UnderwritingRow).where(UnderwritingRow.listing_id == listing.id)
        )
    }
    for scenario in result.scenarios:
        name = scenario.terms.name
        stored = existing.pop(name, None)
        if stored is None:
            stored = UnderwritingRow(listing_id=listing.id, property_id=place.id, scenario=name)
            session.add(stored)
        kpis = scenario.kpis
        misses = buy_box_misses(scenario, listing, place, match, config)
        stored.stage = stage
        stored.engine_version = scenario.engine_version
        stored.results = scenario.model_dump(mode="json")
        stored.cash_flow_door_month = kpis.cash_flow_door_month
        stored.coc, stored.dscr, stored.cap_rate = kpis.coc, kpis.dscr, kpis.cap_rate
        stored.mao = scenario.mao.price
        stored.in_buy_box, stored.buy_box_misses = not misses, misses
        stored.score, stored.grade = None, None
        stored.score_parts = {
            "inputs": score_inputs(
                result, scenario, listing, tags, place.unit_confidence, config
            ).model_dump(mode="json")
        }
        stored.computed_at = utcnow()
    for leftover in existing.values():  # scenarios from an earlier run that no longer apply
        session.delete(leftover)


def _tags(session: Session, property_id: int) -> list[str]:
    flags = session.scalar(
        select(ClassificationRow.flags).where(ClassificationRow.property_id == property_id)
    )
    return [str(flag) for flag in flags or []]


def _units(session: Session, property_id: int) -> list[UnitRow]:
    return list(
        session.scalars(
            select(UnitRow).where(UnitRow.property_id == property_id).order_by(UnitRow.position)
        )
    )


def _underwrite(
    session: Session,
    listing: ListingRow,
    match: str,
    config: Config,
    markets: MarketCache,
    scenarios: list[str],
) -> tuple[AnalysisResult, list[str]]:
    """Underwrite one stored listing. Raises Unscreenable with the reason when it cannot be."""
    place = listing.property
    tags = _tags(session, place.id)
    request = build_request(
        listing,
        place,
        _units(session, place.id),
        tags,
        config,
        get_overrides(session, place.id),
        scenarios,
    )
    market = markets.get(place.zip_code)
    if request.market_rents is None and any(rent is None for rent in request.rents):
        # Some unit has no stated rent, so a free estimate is needed: HUD rents by bedroom.
        if market is None or market.fair_market_rents is None:
            status = next(
                (s for s in (market.sources if market else []) if s.key == "hud_fmr"), None
            )
            why = status.detail if status else "the listing has no ZIP code"
            raise Unscreenable(
                f"No rent is stated for every unit, and HUD rents are not available ({why})."
            )
        if request.beds is None:
            raise Unscreenable(
                "No rent is stated for every unit, and bedrooms per unit are not known, so no "
                "rent can be estimated."
            )
    try:
        result = analyze(request, config, market)
    except AnalysisError as error:
        raise Unscreenable(str(error)) from error
    ranking = config.settings.buy_box.ranking_scenario
    if not any(scenario.terms.name == ranking for scenario in result.scenarios):
        reason = result.skipped.get(ranking, "not available")
        raise Unscreenable(f"The ranking loan '{ranking}' is {reason}.")
    return result, tags


# ---- stage 2 -------------------------------------------------------------------------------------


@dataclass
class _Spend:
    """What stage 2 may still spend in this run, and why it stopped if it did."""

    rent_requests_left: int
    llm_calls_left: int
    rentcast_stopped: str = ""
    llm_stopped: str = ""
    records: int = 0
    rent_requests: int = 0
    flood_lookups: int = 0
    geocodes: int = 0
    llm_calls: int = 0


def _address(place: PropertyRow) -> str:
    return ", ".join(
        part for part in (place.address_line1, place.city, place.state, place.zip_code) if part
    )


def enrich(
    session: Session,
    listing: ListingRow,
    config: Config,
    fetcher: Fetcher,
    rentcast: RentCastSource | None,
    extractor: Extractor | None,
    spend: _Spend,
) -> dict[str, str]:
    """Stage 2 for one listing. Returns a note per step saying what happened; nothing here
    raises for a source problem."""
    rule = config.settings.funnel.stage2
    place = listing.property
    notes: dict[str, str] = {}
    rentcast_ready = rentcast is not None and not spend.rentcast_stopped

    # 1. Property record: the unit count when unconfirmed, and the last tax bill.
    needs_record = (
        listing.source == "rentcast"
        and RECORD_KEY not in (listing.raw_json or {})
        and (place.unit_confidence in (None, "low") or listing.tax_annual is None)
    )
    if needs_record and rentcast is None:
        notes["record"] = "not looked up: RENTCAST_API_KEY is not set"
    elif needs_record and rentcast_ready:
        assert rentcast is not None
        session.commit()  # the request counter writes on its own connection
        try:
            record = rentcast.property_record(listing.source_listing_id)
        except BudgetExceeded as stop:
            spend.rentcast_stopped = str(stop)
            notes["record"] = f"not looked up: {stop}"
        except SourceUnavailable as problem:
            if problem.status == 404:
                apply_record(session, listing, None, config)
                notes["record"] = "RentCast has no property record"
            else:
                notes["record"] = f"not looked up: {problem.reason}"
        else:
            spend.records += 1
            apply_record(session, listing, record, config)
            notes["record"] = "property record found" + (
                f": {record.unit_count} units" if record.unit_count else "; no unit count in it"
            )
    elif needs_record:
        notes["record"] = f"not looked up: {spend.rentcast_stopped}"

    # 2. Claude reads the description when the rules are unsure of it.
    if rule.llm and extractor is not None and not spend.llm_stopped:
        if spend.llm_calls_left <= 0:
            notes["llm"] = "not read: classify.llm.max_calls_per_run reached"
        else:
            try:
                if read_with_claude(session, place, config, extractor):
                    spend.llm_calls += 1
                    spend.llm_calls_left -= 1
                    notes["llm"] = "description read by Claude"
            except SourceUnavailable as problem:
                spend.llm_stopped = problem.reason
                notes["llm"] = f"not read: {problem.reason}"

    # The record or Claude may have shown this is not a wanted kind after all: stop spending.
    session.flush()
    session.refresh(place)
    box = config.settings.buy_box
    if (
        kind_matches(
            place.property_kind, place.unit_confidence, box.property_types, box.min_unit_confidence
        )
        == "no"
    ):
        return notes

    # 3. Coordinates, for the map and the flood lookup.
    if place.latitude is None or place.longitude is None:
        try:
            found = geocode.geocode(
                fetcher,
                street=place.address_line1,
                city=place.city,
                state=place.state,
                zip_code=place.zip_code,
            )
        except SourceUnavailable as problem:
            notes["geocode"] = f"not located: {problem.reason}"
        else:
            if found is None:
                notes["geocode"] = "the Census geocoder could not place this address"
            else:
                place.latitude, place.longitude = found.latitude, found.longitude
                place.geo_cell = dedupe.geo_cell(found.latitude, found.longitude)
                spend.geocodes += 1
                notes["geocode"] = "located by the Census geocoder"

    # 4. Flood zone.
    if rule.flood_zones and place.flood_checked_at is None:
        try:
            zone = flood.flood_zone(
                fetcher,
                latitude=place.latitude,
                longitude=place.longitude,
                max_age=timedelta(days=rule.flood_cache_days),
            )
        except SourceUnavailable as problem:
            notes["flood"] = f"not checked: {problem.reason}"
        else:
            place.flood_zone, place.in_flood_zone = zone.zone, zone.in_flood_zone
            place.flood_checked_at = utcnow()
            spend.flood_lookups += 1
            notes["flood"] = zone.describe()

    # 5. A rent estimate for each unit layout that has none.
    session.flush()
    units = _units(session, place.id)
    missing = [unit for unit in units if unit.market_rent is None]
    if rule.rent_estimates and missing:
        if any(unit.beds is None for unit in units):
            notes["rents"] = "not estimated: bedrooms per unit are not known"
        elif rentcast is None:
            notes["rents"] = "not estimated: RENTCAST_API_KEY is not set"
        elif not _address(place) or not place.address_line1:
            notes["rents"] = "not estimated: no street address"
        else:
            layouts: dict[tuple[float | None, float | None, float | None], list[UnitRow]] = {}
            for unit in missing:
                layouts.setdefault((unit.beds, unit.baths, unit.sqft), []).append(unit)
            for (beds, baths, sqft), same in layouts.items():
                if spend.rentcast_stopped:
                    notes["rents"] = f"not estimated: {spend.rentcast_stopped}"
                    break
                if spend.rent_requests_left <= 0:
                    notes["rents"] = (
                        "not estimated: funnel.stage2.max_rent_requests_per_run reached"
                    )
                    break
                billed_before = rentcast.requests_billed
                session.commit()  # the request counter writes on its own connection
                try:
                    estimate = rentcast.rent_estimate(
                        address=_address(place),
                        kind=place.property_kind,
                        bedrooms=beds or 0.0,
                        bathrooms=baths,
                        sqft=sqft,
                        comp_count=rule.rent_comp_count,
                        cache_days=rule.rent_cache_days,
                    )
                except BudgetExceeded as stop:
                    spend.rentcast_stopped = str(stop)
                    notes["rents"] = f"not estimated: {stop}"
                    break
                except SourceUnavailable as problem:
                    notes["rents"] = f"not estimated: {problem.reason}"
                    break
                billed = rentcast.requests_billed - billed_before
                spend.rent_requests += billed
                spend.rent_requests_left -= billed
                for unit in same:
                    unit.market_rent = estimate.rent
                    unit.market_rent_source = estimate.describe()
                    unit.rent_low, unit.rent_high = estimate.low, estimate.high
                    unit.comps = [comp.model_dump(mode="json") for comp in estimate.comps]
            else:
                count = len(layouts)
                notes["rents"] = (
                    f"rent estimated for {count} unit layout{'s' if count != 1 else ''}"
                )
    session.flush()
    return notes


# ---- the run -------------------------------------------------------------------------------------


def _process(
    session: Session,
    listing: ListingRow,
    match: str,
    scenarios: list[str],
    stage: int,
    config: Config,
    markets: MarketCache,
    **extra: Any,
) -> _Outcome:
    """Underwrite one listing and store the result, or the reason it could not be."""
    settings = config.settings
    ranking = settings.buy_box.ranking_scenario
    try:
        result, tags = _underwrite(session, listing, match, config, markets, scenarios)
    except Unscreenable as problem:
        _store_problem(session, listing, match, str(problem))
        return _Outcome(listing_id=listing.id, problem=str(problem))
    kpis = next(s for s in result.scenarios if s.terms.name == ranking).kpis
    passed, note = stage1_verdict(
        kpis.cash_flow_door_month, kpis.rent_to_price, settings.funnel.stage1
    )
    _store_result(
        session, listing, match, result, tags, config, stage=stage, passed=passed, note=note,
        **extra,
    )  # fmt: skip
    place = listing.property
    max_price = settings.buy_box.max_price
    return _Outcome(
        listing_id=listing.id,
        county=place.county_fips,
        cap_rate=kpis.cap_rate,
        price_per_door=kpis.price_per_door,
        cash_flow_door=kpis.cash_flow_door_month,
        passed=passed,
        eligible=passed
        and (place.state is None or place.state in wanted_states(config))
        and (max_price is None or (listing.price or 0) <= max_price),
    )


def _rescore(row: UnderwritingRow, config: Config, cap_median: Any, door_median: Any) -> str:
    """Score one stored result against its county's medians. Returns the grade."""
    inputs = ScoreInputs(
        **{
            **row.score_parts["inputs"],
            "county_cap_rate": cap_median,
            "county_price_per_door": door_median,
        }
    )
    result = score(inputs, config.settings.scoring)
    row.score, row.grade = result.total, result.grade
    row.score_parts = {
        "inputs": inputs.model_dump(mode="json"),
        **result.model_dump(mode="json", exclude={"total", "grade"}),
    }
    return result.grade


def underwrite_listing(
    engine: Engine,
    config: Config,
    environment: Environment,
    fetcher: Fetcher,
    listing_id: int,
    *,
    markets: MarketCache | None = None,
) -> str | None:
    """Underwrite one stored listing again, under every loan preset, after its figures
    changed. Nothing is fetched beyond market data, and the county medians from the last
    full run are reused for the score. Returns the reason if it cannot be underwritten."""
    settings = config.settings
    ranking = settings.buy_box.ranking_scenario
    presets = settings.financing.presets
    every = [name for name, preset in presets.items() if preset.enabled or name == ranking]
    box = settings.buy_box
    with session_scope(engine) as session:
        listing = session.get(ListingRow, listing_id)
        if listing is None:
            return "The listing is no longer stored."
        place = listing.property
        match = kind_matches(
            place.property_kind, place.unit_confidence, box.property_types, box.min_unit_confidence
        )
        before = session.scalars(
            select(UnderwritingRow).where(UnderwritingRow.listing_id == listing_id)
        ).first()
        medians = (None, None)
        if before is not None and "inputs" in (before.score_parts or {}):
            stored = before.score_parts["inputs"]
            medians = (stored.get("county_cap_rate"), stored.get("county_price_per_door"))
        screen = session.scalars(
            select(ScreenRow).where(ScreenRow.listing_id == listing_id)
        ).first()
        stage = max(1, screen.stage) if screen is not None else 1
        outcome = _process(
            session,
            listing,
            match,
            every,
            stage,
            config,
            markets or MarketCache(config, environment, fetcher),
        )
        session.flush()
        for row in session.scalars(
            select(UnderwritingRow).where(UnderwritingRow.listing_id == listing_id)
        ):
            _rescore(row, config, *medians)
        return outcome.problem


class FunnelReport(BaseModel):
    candidates: int = 0
    underwritten: int = 0
    problems: dict[str, int] = Field(default_factory=dict)  # reason -> listings
    kept: int = 0
    dropped: int = 0
    enriched: int = 0
    records: int = 0
    rent_requests: int = 0
    flood_lookups: int = 0
    geocodes: int = 0
    llm_calls: int = 0
    in_buy_box: int = 0
    grades: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def text(self) -> str:
        lines = [
            f"Stage 1: {self.candidates:,} listings of the wanted types; "
            f"{self.underwritten:,} underwritten, {sum(self.problems.values()):,} could not be.",
        ]
        lines.extend(f"  {count:,} x {reason}" for reason, count in self.problems.items())
        lines.append(f"  Kept {self.kept:,}, dropped {self.dropped:,}.")
        lines.append(
            f"Stage 2: {self.enriched:,} listings enriched. RentCast requests billed: "
            f"{self.records} property records, {self.rent_requests} rent estimates. "
            f"Flood lookups {self.flood_lookups}, geocodes {self.geocodes}, "
            f"descriptions read by Claude {self.llm_calls}."
        )
        lines.extend(f"  {note}" for note in self.notes)
        grades = ", ".join(f"{count} {grade}" for grade, count in sorted(self.grades.items()))
        lines.append(f"In the buy box: {self.in_buy_box:,}. Grades: {grades or 'none'}.")
        return "\n".join(lines)


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def run_funnel(
    engine: Engine,
    config: Config,
    environment: Environment,
    fetcher: Fetcher,
    *,
    rentcast: RentCastSource | None = None,
    extractor: Extractor | None = None,
    stage2: bool = True,
    markets: MarketCache | None = None,
) -> FunnelReport:
    """Run the whole funnel over the stored listings and store the results.

    ``rentcast`` and ``extractor`` are the paid steps of stage 2; without them those steps are
    skipped and say so. ``markets`` supplies market data by ZIP; by default it is loaded from
    the free sources.
    """
    settings = config.settings
    ranking = settings.buy_box.ranking_scenario
    presets = settings.financing.presets
    if ranking not in presets:
        raise AnalysisError(
            f"buy_box.ranking_scenario '{ranking}' is not a loan preset. "
            f"Choose from: {', '.join(presets)}."
        )
    every = [name for name, preset in presets.items() if preset.enabled or name == ranking]
    markets = markets or MarketCache(config, environment, fetcher)
    report = FunnelReport()
    problems: Counter[str] = Counter()
    outcomes: dict[int, _Outcome] = {}

    with session_scope(engine) as session:
        chosen = candidates(session, config)
        keep = [listing_id for listing_id, _ in chosen]
        # Listings that are no longer candidates lose their stored results.
        for table in (UnderwritingRow, ScreenRow):
            session.execute(delete(table).where(table.listing_id.not_in(keep)))
    report.candidates = len(chosen)
    matches = dict(chosen)

    def one(session: Session, listing: ListingRow, scenarios: list[str], stage: int, **extra: Any):
        return _process(
            session, listing, matches[listing.id], scenarios, stage, config, markets, **extra
        )

    load = (selectinload(ListingRow.property), selectinload(ListingRow.price_events))
    for batch in _chunks(keep, _BATCH):
        with session_scope(engine) as session:
            rows = session.scalars(
                select(ListingRow).where(ListingRow.id.in_(batch)).options(*load)
            ).all()
            for listing in rows:
                outcomes[listing.id] = one(session, listing, [ranking], 1)

    # ---- stage 2: the survivors, best cash flow first
    survivors = sorted(
        (outcome for outcome in outcomes.values() if outcome.eligible),
        key=lambda outcome: -(outcome.cash_flow_door or 0.0),
    )
    spend = _Spend(
        rent_requests_left=settings.funnel.stage2.max_rent_requests_per_run,
        llm_calls_left=settings.classify.llm.max_calls_per_run,
    )
    if stage2:
        for survivor in survivors:
            with session_scope(engine) as session:
                listing = session.get_one(ListingRow, survivor.listing_id, options=load)
                notes = enrich(session, listing, config, fetcher, rentcast, extractor, spend)
                session.refresh(listing.property)
                # The record or Claude may have changed what this property is.
                box = settings.buy_box
                matches[listing.id] = kind_matches(
                    listing.property.property_kind,
                    listing.property.unit_confidence,
                    box.property_types,
                    box.min_unit_confidence,
                )
                if matches[listing.id] == "no":
                    _store_problem(
                        session,
                        listing,
                        "no",
                        f"Not a wanted type after a closer look: "
                        f"{listing.property.property_kind.replace('_', ' ')}.",
                    )
                    outcomes[listing.id] = _Outcome(listing_id=listing.id, problem="not wanted")
                    continue
                outcomes[listing.id] = one(session, listing, every, 2, enrichment=notes)
                report.enriched += 1
        report.records, report.rent_requests = spend.records, spend.rent_requests
        report.flood_lookups, report.geocodes = spend.flood_lookups, spend.geocodes
        report.llm_calls = spend.llm_calls
        if spend.rentcast_stopped:
            report.notes.append(f"RentCast stopped early: {spend.rentcast_stopped}")
        if spend.llm_stopped:
            report.notes.append(f"Claude stopped early: {spend.llm_stopped}")
        if survivors and rentcast is None:
            report.notes.append(
                "RentCast is not connected (RENTCAST_API_KEY), so no rent estimates or "
                "property records were fetched."
            )
    else:
        report.notes.append("Stage 2 was skipped, as asked.")

    for outcome in outcomes.values():
        if outcome.problem is not None:
            problems[outcome.problem] += 1
        else:
            report.underwritten += 1
            report.kept += bool(outcome.passed)
            report.dropped += not outcome.passed
    report.problems = dict(problems.most_common())

    # ---- scores, against each county's medians
    by_county: dict[str, list[_Outcome]] = defaultdict(list)
    for outcome in outcomes.values():
        if outcome.county and outcome.cap_rate is not None:
            by_county[outcome.county].append(outcome)
    medians = {
        county: (
            median(o.cap_rate for o in group if o.cap_rate is not None),
            median(o.price_per_door for o in group if o.price_per_door is not None),
        )
        for county, group in by_county.items()
        if len(group) >= settings.scoring.county_sample_min
    }
    counties = {outcome.listing_id: outcome.county for outcome in outcomes.values()}
    grades: Counter[str] = Counter()
    for batch in _chunks(keep, _BATCH):
        with session_scope(engine) as session:
            for row in session.scalars(
                select(UnderwritingRow).where(UnderwritingRow.listing_id.in_(batch))
            ):
                grade = _rescore(
                    row, config, *medians.get(counties.get(row.listing_id) or "", (None, None))
                )
                if row.scenario == ranking:
                    grades[grade] += 1
                    report.in_buy_box += bool(row.in_buy_box)
    report.grades = dict(grades)
    return report
