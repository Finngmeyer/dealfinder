"""Deal score, 0 to 100, with the points behind it.

Pure: numbers in, a score out. The weights, the value that earns no points and the value that
earns full points all come from ``scoring`` in settings. Anything unknown earns nothing.

    score = (sum of the parts - penalties) x confidence multiplier, held between 0 and 100
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from dealfinder.settings import Scale, Scoring

Confidence = Literal["high", "medium", "low"]


class ScoreInputs(BaseModel):
    cash_flow_door_month: float
    coc: float | None
    dscr: float | None  # None with no loan
    has_debt: bool
    cap_rate: float
    price_per_door: float
    county_cap_rate: float | None = None  # median of the county's underwritten listings
    county_price_per_door: float | None = None
    county_name: str = "county"
    days_on_market: int | None = None
    price_cut_pct: float | None = None  # total cut from the first asking price
    tags: Sequence[str] = ()
    listing_type: str | None = None
    rent_upside: float | None = None  # market rent over rent in place, minus one
    in_flood_zone: bool = False
    flood_checked: bool = True
    high_hazard: bool = False
    hazard_checked: bool = True
    confidence: Confidence = "low"


class ScorePart(BaseModel):
    key: str
    label: str
    points: float
    out_of: float
    detail: str


class Score(BaseModel):
    total: float
    grade: str
    parts: list[ScorePart] = Field(default_factory=list)
    penalties: list[ScorePart] = Field(default_factory=list)
    before_multiplier: float
    multiplier: float
    confidence: Confidence


def scaled(value: float | None, scale: Scale) -> float:
    """Points for a value on a straight line from none at ``zero_at`` to all at ``full_at``."""
    if value is None:
        return 0.0
    span = scale.full_at - scale.zero_at
    share = (value - scale.zero_at) / span if span else float(value >= scale.full_at)
    return round(scale.weight * min(1.0, max(0.0, share)), 6)  # no float dust


def grade_for(total: float, grades: dict[str, float]) -> str:
    """The best grade whose floor the score reaches; F below them all."""
    for name, floor in sorted(grades.items(), key=lambda item: -item[1]):
        if total >= floor:
            return name
    return "F"


def _motivation(inputs: ScoreInputs, rules: Scoring) -> ScorePart:
    rule = rules.seller_motivation
    points = 0.0
    notes: list[str] = []
    if inputs.days_on_market is not None:
        share = min(1.0, inputs.days_on_market / rule.days_on_market.full_at)
        points += rule.days_on_market.points * share
        notes.append(f"{inputs.days_on_market} days on market")
    if inputs.price_cut_pct and inputs.price_cut_pct > 0:
        share = min(1.0, inputs.price_cut_pct / rule.price_cut.full_at)
        points += rule.price_cut.points * share
        notes.append(f"price cut {inputs.price_cut_pct:.1%}")
    signals = [tag.replace("_", " ") for tag in rule.tags if tag in inputs.tags]
    if inputs.listing_type in rule.listing_types:
        signals.append(str(inputs.listing_type).lower())
    if signals:
        points += rule.tag_points * len(signals)
        notes.append(", ".join(signals))
    return ScorePart(
        key="seller_motivation",
        label="Seller motivation",
        points=round(min(rule.weight, points), 6),
        out_of=rule.weight,
        detail="; ".join(notes) or "no signs of a motivated seller",
    )


def score(inputs: ScoreInputs, rules: Scoring) -> Score:
    parts = [
        ScorePart(
            key="cash_flow_per_door",
            label="Cash flow per door",
            points=scaled(inputs.cash_flow_door_month, rules.cash_flow_per_door),
            out_of=rules.cash_flow_per_door.weight,
            detail=f"${inputs.cash_flow_door_month:,.0f} a month; "
            f"full marks at ${rules.cash_flow_per_door.full_at:,.0f}",
        ),
        ScorePart(
            key="coc",
            label="Cash-on-cash return",
            points=scaled(inputs.coc, rules.coc),
            out_of=rules.coc.weight,
            detail=("not defined" if inputs.coc is None else f"{inputs.coc:.1%}")
            + f"; full marks at {rules.coc.full_at:.0%}",
        ),
    ]
    if inputs.dscr is None and not inputs.has_debt:
        parts.append(
            ScorePart(
                key="dscr",
                label="Debt coverage",
                points=rules.dscr.weight,
                out_of=rules.dscr.weight,
                detail="no loan, so nothing to cover",
            )
        )
    else:
        parts.append(
            ScorePart(
                key="dscr",
                label="Debt coverage",
                points=scaled(inputs.dscr, rules.dscr),
                out_of=rules.dscr.weight,
                detail=("not defined" if inputs.dscr is None else f"DSCR {inputs.dscr:.2f}")
                + f"; full marks at {rules.dscr.full_at:.2f}",
            )
        )

    place = inputs.county_name
    if inputs.county_cap_rate is None:
        cap_points, cap_detail = 0.0, f"too few listings in {place} to compare"
    else:
        spread = inputs.cap_rate - inputs.county_cap_rate
        cap_points = scaled(spread, rules.cap_rate_vs_county)
        cap_detail = (
            f"{inputs.cap_rate:.1%} against a {place} median of {inputs.county_cap_rate:.1%}"
        )
    parts.append(
        ScorePart(
            key="cap_rate_vs_county",
            label="Cap rate against the county",
            points=cap_points,
            out_of=rules.cap_rate_vs_county.weight,
            detail=cap_detail,
        )
    )
    if inputs.county_price_per_door is None or inputs.county_price_per_door <= 0:
        door_points, door_detail = 0.0, f"too few listings in {place} to compare"
    else:
        discount = 1 - inputs.price_per_door / inputs.county_price_per_door
        door_points = scaled(discount, rules.price_per_door_vs_county)
        door_detail = (
            f"${inputs.price_per_door:,.0f} a door against a {place} median of "
            f"${inputs.county_price_per_door:,.0f}"
        )
    parts.append(
        ScorePart(
            key="price_per_door_vs_county",
            label="Price per door against the county",
            points=door_points,
            out_of=rules.price_per_door_vs_county.weight,
            detail=door_detail,
        )
    )
    parts.append(_motivation(inputs, rules))
    parts.append(
        ScorePart(
            key="rent_upside",
            label="Rent upside",
            points=scaled(inputs.rent_upside, rules.rent_upside),
            out_of=rules.rent_upside.weight,
            detail="needs both rents in place and a market estimate to compare"
            if inputs.rent_upside is None
            else f"market rents are {inputs.rent_upside:+.0%} against rents in place",
        )
    )

    penalties: list[ScorePart] = []
    if inputs.in_flood_zone:
        penalties.append(
            ScorePart(
                key="flood_zone",
                label="Flood zone",
                points=-rules.penalties.flood_zone,
                out_of=0,
                detail="in a FEMA flood zone"
                if inputs.flood_checked
                else "flood zone not checked: counted as in one",
            )
        )
    if inputs.high_hazard:
        penalties.append(
            ScorePart(
                key="high_hazard",
                label="Natural-hazard risk",
                points=-rules.penalties.high_hazard,
                out_of=0,
                detail="FEMA rates the county's hazard risk high"
                if inputs.hazard_checked
                else "county hazard rating not loaded: counted as high",
            )
        )

    before = sum(part.points for part in parts) + sum(part.points for part in penalties)
    multiplier = rules.confidence_multiplier[inputs.confidence]
    total = round(min(100.0, max(0.0, before * multiplier)), 1)
    return Score(
        total=total,
        grade=grade_for(total, rules.grades),
        parts=parts,
        penalties=penalties,
        before_multiplier=round(before, 2),
        multiplier=multiplier,
        confidence=inputs.confidence,
    )
