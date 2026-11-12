"""The optional step where Claude reads a description.

The API is never called: the SDK is pointed at a local stand-in transport. What matters most
here is the quote check, which keeps a model from adding facts the description does not state.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from dealfinder.classify.classifier import ClassifierInput, classify
from dealfinder.classify.llm import (
    LLM_METHOD,
    SCHEMA,
    ClaudeExtractor,
    LlmExtraction,
    LlmUnit,
    apply_extraction,
    checked,
    supported,
)
from dealfinder.http import SourceUnavailable

TEXT = (
    "Brick building with four apartments near the park. The first unit rents for $1,100 a "
    "month and has two bedrooms. Upstairs unit is a studio."
)


class TestQuoteCheck:
    @pytest.mark.parametrize(
        ("number", "quote"),
        [
            (4, "four apartments"),
            (4, "Brick building with FOUR apartments"),  # case does not matter
            (1100, "rents for $1,100 a month"),
            (2, "has two bedrooms"),
            (0, "Upstairs unit is a studio"),
        ],
    )
    def test_a_number_its_quote_states_is_kept(self, number, quote):
        assert supported(number, quote, TEXT)

    @pytest.mark.parametrize(
        ("number", "quote"),
        [
            (4, "four units"),  # not in the description
            (5, "four apartments"),  # the quote is real but says a different number
            (1100, "near the park"),  # real words, no number
            (4, ""),
            (4, None),
            (None, "four apartments"),
            (1.5, "has two bedrooms"),
        ],
    )
    def test_anything_else_is_dropped(self, number, quote):
        assert not supported(number, quote, TEXT)

    def test_a_building_word_counts_for_its_number(self):
        assert supported(4, "Solid fourplex", "Solid fourplex with parking")
        assert supported(3, "triple decker", "Classic triple decker")
        assert not supported(4, "Solid triplex", "Solid triplex with parking")

    def test_spacing_and_punctuation_variants_still_match(self):
        description = "Rents:  $950\nper month for the  4\u2013plex"
        assert supported(950, "Rents: $950 per month", description)

    def test_checked_keeps_only_what_survives(self):
        extraction = LlmExtraction(
            unit_count=4,
            unit_count_quote="four apartments",
            units=[
                LlmUnit(unit=1, beds=2, baths=1, rent=1100, quote="rents for $1,100 a month"),
                LlmUnit(unit=2, beds=1, baths=1, rent=900, quote="a quote that is not there"),
                LlmUnit(unit=3, rent=40_000, quote="rents for $1,100 a month"),  # not a rent
            ],
        )
        result = checked(extraction, TEXT)
        assert result.unit_count == 4
        assert [(unit.unit, unit.beds, unit.baths, unit.rent) for unit in result.units] == [
            (1, None, None, 1100.0)  # the quote states the rent but neither beds nor baths
        ]

    def test_an_unsupported_count_is_removed(self):
        result = checked(LlmExtraction(unit_count=6, unit_count_quote="four apartments"), TEXT)
        assert result.unit_count is None and result.unit_count_quote is None


def message(payload: dict | str, stop_reason: str = "end_turn", thinking: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    content = [{"type": "text", "text": text}]
    if thinking:
        content.insert(0, {"type": "thinking", "thinking": "counting", "signature": "sig"})
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def extractor_for(handler) -> ClaudeExtractor:
    client = anthropic.Anthropic(
        api_key="sk-test-not-a-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    return ClaudeExtractor(client, model="claude-haiku-5-5", effort="low", max_tokens=4000)


ANSWER = {"unit_count": 4, "unit_count_quote": "four apartments", "units": []}


class TestClaudeExtractor:
    def test_the_request_and_the_checked_answer(self):
        seen: list[dict] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(json.loads(request.content))
            return httpx2.Response(200, json=message(ANSWER))

        extractor = extractor_for(handler)
        result = extractor(TEXT)
        assert result is not None and result.unit_count == 4 and extractor.calls == 1
        body = seen[0]
        assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 4000
        assert body["messages"] == [{"role": "user", "content": TEXT}]
        assert body["output_config"] == {
            "effort": "low",
            "format": {"type": "json_schema", "schema": SCHEMA},
        }
        assert "verbatim" in body["system"]

    def test_the_answer_is_found_after_a_thinking_block(self):
        extractor = extractor_for(
            lambda request: httpx2.Response(200, json=message(ANSWER, thinking=True))
        )
        assert extractor(TEXT).unit_count == 4

    def test_an_invented_count_does_not_survive(self):
        invented = {"unit_count": 6, "unit_count_quote": "six apartments", "units": []}
        extractor = extractor_for(lambda request: httpx2.Response(200, json=message(invented)))
        assert extractor(TEXT).unit_count is None

    @pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
    def test_a_refusal_or_a_cut_off_answer_is_no_answer(self, stop_reason):
        extractor = extractor_for(
            lambda request: httpx2.Response(200, json=message(ANSWER, stop_reason))
        )
        assert extractor(TEXT) is None

    @pytest.mark.parametrize("text", ["not json", '{"unit_count": "many"}', "[]"])
    def test_an_answer_of_the_wrong_shape_is_no_answer(self, text):
        extractor = extractor_for(lambda request: httpx2.Response(200, json=message(text)))
        assert extractor(TEXT) is None

    @pytest.mark.parametrize(
        ("status", "kind", "reason"),
        [
            (401, "authentication_error", "ANTHROPIC_API_KEY was not accepted"),
            (403, "permission_error", "may not use this model"),
            (404, "not_found_error", "was not found"),
            (429, "rate_limit_error", "rate limited"),
            (529, "overloaded_error", "HTTP 529"),
        ],
    )
    def test_api_errors_become_source_unavailable(self, status, kind, reason):
        error = {"type": "error", "error": {"type": kind, "message": "no"}}
        extractor = extractor_for(lambda request: httpx2.Response(status, json=error))
        with pytest.raises(SourceUnavailable, match=reason) as caught:
            extractor(TEXT)
        assert caught.value.source == "llm"

    def test_no_connection(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("down", request=request)

        with pytest.raises(SourceUnavailable, match="could not reach"):
            extractor_for(handler)(TEXT)


class TestApplyExtraction:
    def base(self, config, description: str, **fields):
        entry = ClassifierInput(source_kind="multifamily_2_4", description=description, **fields)
        return entry, classify(entry, config.keywords)

    def test_fills_a_count_the_rules_could_not_find(self, config):
        entry, result = self.base(config, TEXT, bedrooms=6, bathrooms=4)
        result.unit_count, result.confidence, result.property_kind = None, "low", "multifamily_2_4"
        result.units = []
        extraction = LlmExtraction(
            unit_count=4,
            unit_count_quote="four apartments",
            units=[LlmUnit(unit=1, rent=1100, quote="rents for $1,100 a month")],
        )
        updated = apply_extraction(result, extraction, entry)
        assert (updated.unit_count, updated.confidence, updated.property_kind) == (
            4,
            "medium",
            "fourplex",
        )
        assert updated.method == LLM_METHOD and updated.is_fourplex
        assert updated.signals[-1].name == "llm"
        assert updated.signals[-1].evidence == "four apartments"
        assert [unit.in_place_rent for unit in updated.units] == [1100.0, None, None, None]
        assert updated.units[0].rent_quote == "rents for $1,100 a month"
        assert result.unit_count is None  # the original is left alone

    def test_never_overrules_the_rules(self, config):
        entry, result = self.base(config, "Quadplex with separate meters")
        updated = apply_extraction(
            result, LlmExtraction(unit_count=3, unit_count_quote="Quadplex"), entry
        )
        assert (updated.unit_count, updated.confidence) == (4, "medium")
        assert "llm_disagrees" in updated.flags

    def test_agreement_adds_evidence_without_a_flag(self, config):
        entry, result = self.base(config, "Quadplex with separate meters")
        updated = apply_extraction(
            result, LlmExtraction(unit_count=4, unit_count_quote="Quadplex"), entry
        )
        assert updated.unit_count == 4 and "llm_disagrees" not in updated.flags

    def test_a_stated_rent_from_the_rules_is_kept(self, config):
        entry, result = self.base(config, "Fourplex. Unit 1 rents for $1,000. Unit 2 is vacant.")
        extraction = LlmExtraction(
            units=[
                LlmUnit(unit=1, rent=1200, quote="x"),
                LlmUnit(unit=2, rent=900, quote="x"),  # the rules know this unit is vacant
                LlmUnit(unit=3, rent=950, quote="y"),
                LlmUnit(unit=9, rent=800, quote="z"),  # there is no ninth unit
            ]
        )
        updated = apply_extraction(result, extraction, entry)
        assert [unit.in_place_rent for unit in updated.units] == [1000.0, None, 950.0, None]

    def test_stated_layouts_replace_an_even_split(self, config):
        entry, result = self.base(config, "Quadplex", bedrooms=6, bathrooms=4, building_sqft=3200)
        assert [unit.beds for unit in result.units] == [2.0, 2.0, 1.0, 1.0]
        extraction = LlmExtraction(
            units=[LlmUnit(unit=n, beds=beds, baths=1, quote="q") for n, beds in ((1, 3), (2, 1))]
        )
        updated = apply_extraction(result, extraction, entry)
        assert [unit.beds for unit in updated.units] == [3.0, 1.0, 1.0, 1.0]
        assert updated.units[0].sqft is None and updated.units[2].sqft == 800.0
        assert updated.mix_confidence == "low"  # only two of the four units were described

        every = LlmExtraction(
            units=[LlmUnit(unit=n, beds=1, baths=1, quote="q") for n in range(1, 5)]
        )
        assert apply_extraction(result, every, entry).mix_confidence == "high"

    def test_land_is_left_alone(self, config):
        entry, result = self.base(config, "Lot zoned for a fourplex")
        updated = apply_extraction(
            result, LlmExtraction(unit_count=4, unit_count_quote="fourplex"), entry
        )
        assert updated.property_kind == "land" and updated.unit_count is None
