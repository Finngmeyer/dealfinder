"""Optional: have Claude read a description the rules could not settle.

It is asked for the unit count and per-unit facts as JSON, and for each number the exact words
of the description that state it. Nothing is taken on trust: a number is kept only if its quote
appears in the description word for word and the quote actually contains that number. Whatever
fails the check is dropped, so the model can add evidence but cannot invent it.

Off unless ``classify.llm.enabled`` is true and ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from dealfinder.classify import patterns as p
from dealfinder.classify.classifier import Classification, ClassifierInput, Signal, kind_for
from dealfinder.classify.rents import MAX_RENT, MIN_RENT
from dealfinder.classify.unit_mix import UnitDetail, even_mix
from dealfinder.http import SourceUnavailable

SOURCE = "llm"
LLM_METHOD = "rules+llm"  # stored on a classification once Claude has been asked about it
MAX_UNITS = 60

SYSTEM_PROMPT = """You read real-estate listing descriptions and report facts about the building \
that is for sale.

Report only what the description states. Do not estimate, infer from typical buildings, or fill \
gaps. If the description does not state something, use null or an empty list.

unit_count is the number of dwelling units in the building or buildings being sold, as they \
exist today. Ignore units that could be built or added, zoning allowances, neighboring \
properties, and the size of a larger complex when a single unit in it is for sale. Two \
fourplexes sold together are 8 units.

For every number you report, give quote: the exact, contiguous words copied from the description \
that state it. A quote must be a verbatim substring of the description and must contain the \
number, as digits or as a word (a word such as "fourplex" counts for 4). If you cannot quote it, \
do not report it.

units lists facts about individual units only when the description gives them per unit: \
bedrooms, bathrooms, and current monthly rent in dollars. Leave out a unit the description says \
nothing specific about."""

_NULLABLE_NUMBER = {"type": ["number", "null"]}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "unit_count": {"type": ["integer", "null"]},
        "unit_count_quote": {"type": ["string", "null"]},
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unit": {"type": "integer", "description": "1 for the first unit"},
                    "beds": _NULLABLE_NUMBER,
                    "baths": _NULLABLE_NUMBER,
                    "rent": _NULLABLE_NUMBER,
                    "quote": {"type": "string"},
                },
                "required": ["unit", "beds", "baths", "rent", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["unit_count", "unit_count_quote", "units"],
    "additionalProperties": False,
}


class LlmUnit(BaseModel):
    unit: int = Field(ge=1, le=MAX_UNITS)
    beds: float | None = None
    baths: float | None = None
    rent: float | None = None
    quote: str = ""


class LlmExtraction(BaseModel):
    unit_count: int | None = None
    unit_count_quote: str | None = None
    units: list[LlmUnit] = Field(default_factory=list)


Extractor = Callable[[str], LlmExtraction | None]


def _squash(text: str) -> str:
    return " ".join(p.normalize(text).split())


def _mentions(number: float, quote: str) -> bool:
    """Whether a quote states a number: as digits ("4", "1,100"), as a word ("four"), or as a
    building word that means it ("fourplex")."""
    whole = int(number) if float(number).is_integer() else None
    digits = re.findall(r"\d+(?:\.\d+)?", quote.replace(",", ""))
    if any(float(found) == float(number) for found in digits):
        return True
    if whole is None:
        return False
    if any(word in quote.split() for word, value in p.NUMBER_WORDS.items() if value == whole):
        return True
    if whole == 0 and "studio" in quote:
        return True
    return any(word in quote for word, value in p.PLEX_WORDS.items() if value == whole)


def supported(number: float | None, quote: str | None, description: str) -> bool:
    """True when ``quote`` is in the description word for word and contains ``number``."""
    if number is None or not quote:
        return False
    squashed = _squash(quote)
    return bool(squashed) and squashed in _squash(description) and _mentions(number, squashed)


def checked(extraction: LlmExtraction, description: str) -> LlmExtraction:
    """The extraction with everything that fails the quote check removed."""
    count = extraction.unit_count
    count_ok = (
        count is not None
        and 1 <= count <= 500
        and supported(count, extraction.unit_count_quote, description)
    )
    units: list[LlmUnit] = []
    for unit in extraction.units:
        kept = LlmUnit(unit=unit.unit, quote=unit.quote)
        if supported(unit.beds, unit.quote, description):
            kept.beds = unit.beds
        if supported(unit.baths, unit.quote, description):
            kept.baths = unit.baths
        if unit.rent is not None and MIN_RENT <= unit.rent <= MAX_RENT:
            kept.rent = unit.rent if supported(unit.rent, unit.quote, description) else None
        if kept.beds is not None or kept.baths is not None or kept.rent is not None:
            units.append(kept)
    return LlmExtraction(
        unit_count=count if count_ok else None,
        unit_count_quote=extraction.unit_count_quote if count_ok else None,
        units=units,
    )


class ClaudeExtractor:
    """Calls the Anthropic API. ``client`` is an ``anthropic.Anthropic`` instance."""

    def __init__(self, client: Any, *, model: str, effort: str, max_tokens: int) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self.calls = 0

    def __call__(self, description: str) -> LlmExtraction | None:
        """The checked extraction, or None when the model declined or ran out of room.

        Raises SourceUnavailable when the API cannot be used at all (bad key, no connection),
        so the caller can switch the feature off for the rest of the run.
        """
        import anthropic  # imported here so the SDK stays an optional dependency

        self.calls += 1
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": description}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
            )
        except anthropic.AuthenticationError as error:
            raise SourceUnavailable(SOURCE, "ANTHROPIC_API_KEY was not accepted") from error
        except anthropic.PermissionDeniedError as error:
            raise SourceUnavailable(SOURCE, "the key may not use this model") from error
        except anthropic.NotFoundError as error:
            raise SourceUnavailable(SOURCE, f"model '{self._model}' was not found") from error
        except anthropic.RateLimitError as error:
            raise SourceUnavailable(SOURCE, "rate limited; try again later") from error
        except anthropic.APIStatusError as error:
            raise SourceUnavailable(SOURCE, f"the API returned HTTP {error.status_code}") from error
        except anthropic.APIConnectionError as error:
            raise SourceUnavailable(SOURCE, "could not reach the Anthropic API") from error

        if response.stop_reason != "end_turn":  # refusal, or out of tokens mid-answer
            return None
        # Thinking blocks can come first, so the answer is found by type, not position.
        text = next((block.text for block in response.content if block.type == "text"), None)
        if not text:
            return None
        try:
            return checked(LlmExtraction.model_validate(json.loads(text)), description)
        except (ValueError, ValidationError):
            return None


def apply_extraction(
    result: Classification, extraction: LlmExtraction, entry: ClassifierInput
) -> Classification:
    """Fold a checked extraction into a rule-based classification.

    The model fills gaps; it does not overrule. A count from the rules at medium or high
    confidence stands, and a disagreement is flagged for a person to look at.
    """
    updated = result.model_copy(deep=True)
    updated.method = LLM_METHOD
    count = extraction.unit_count
    if count is not None:
        settled = updated.unit_count is not None and updated.meets("medium")
        if settled and count != updated.unit_count:
            updated.flags.append("llm_disagrees")
        elif not settled and updated.property_kind not in ("land", "sfr", "condo", "townhouse"):
            updated.unit_count = count
            updated.confidence = "medium"
            updated.property_kind = kind_for(count, entry.source_kind)
            updated.units = even_mix(count, entry.bedrooms, entry.bathrooms, entry.building_sqft)
            updated.mix_confidence = "low"
        updated.signals.append(
            Signal(
                name="llm",
                strength="medium",
                unit_count=count,
                evidence=extraction.unit_count_quote or "",
            )
        )
    layouts = 0
    for unit in extraction.units:
        position = unit.unit - 1
        if position >= len(updated.units):
            continue
        target: UnitDetail = updated.units[position]
        if unit.rent is not None and target.in_place_rent is None and not target.vacant:
            target.in_place_rent = unit.rent
            target.rent_quote = unit.quote
        # An even split is a guess; a layout the description states for this unit beats it.
        if updated.mix_confidence == "low" and unit.beds is not None:
            target.beds = unit.beds
            if unit.baths is not None:
                target.baths = unit.baths
            target.sqft = None  # the even split of the building's area no longer applies
            layouts += 1
    if updated.units and layouts == len(updated.units):
        updated.mix_confidence = "high"
    return updated
