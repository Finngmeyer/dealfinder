"""The rule-based classifier: unit counts, confidence, tags, unit mix and stated rents.

Every description here is made up. The real labeled set is checked by
tests/test_classifier_labeled.py when the owner has supplied one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dealfinder.classify.classifier import (
    ClassifierInput,
    classify,
    kind_for,
    kind_matches,
    matches_types,
)
from dealfinder.classify.evaluate import evaluate, load_labeled
from dealfinder.classify.keywords import find_flags
from dealfinder.classify.rents import find_rents, unit_clauses
from dealfinder.classify.unit_mix import even_mix, split_baths, split_whole

SYNTHETIC = Path(__file__).parent / "fixtures" / "descriptions_synthetic.csv"


@pytest.fixture
def read(config):
    """Classify a description as a source's "2 to 4 units" listing unless told otherwise."""

    def run(description: str | None = None, **fields):
        fields.setdefault("source_kind", "multifamily_2_4")
        return classify(ClassifierInput(description=description, **fields), config.keywords)

    return run


class TestTheNineRequiredCases:
    """The cases the specification says must pass, word for word."""

    def test_four_plex_with_a_layout_for_each_unit(self, read):
        result = read("Fully rented 4-plex, each unit 2bd/1ba")
        assert (result.unit_count, result.confidence, result.property_kind) == (
            4,
            "high",
            "fourplex",
        )
        assert result.is_fourplex
        assert [(unit.beds, unit.baths) for unit in result.units] == [(2.0, 1.0)] * 4
        assert result.mix_confidence == "high"

    def test_quadplex(self, read):
        result = read("Quadplex with separate meters")
        assert result.unit_count == 4 and result.is_fourplex

    def test_two_fourplexes_are_eight_units(self, read):
        result = read("Two fourplexes on one lot, sold together")
        assert result.unit_count == 8 and not result.is_fourplex
        assert result.property_kind == "multifamily_5plus"

    def test_units_available_in_a_larger_complex(self, read):
        result = read("4 units available in this 24-unit complex")
        assert result.unit_count == 24 and not result.is_fourplex

    def test_duplex_with_fours_in_it(self, read):
        result = read("Duplex with 4 bedrooms and a 4 car garage")
        assert result.unit_count == 2 and result.property_kind == "duplex"
        assert not result.is_fourplex

    def test_a_lot_zoned_for_a_fourplex_is_land(self, read):
        result = read("Lot zoned for a fourplex")
        assert result.property_kind == "land" and result.unit_count is None
        assert not result.is_fourplex and result.units == []

    def test_triplex_plus_adu(self, read):
        result = read("Triplex plus detached ADU, 4 units total")
        assert result.unit_count == 4 and "adu" in result.flags

    def test_unit_numbers_with_rents_and_a_vacancy(self, read):
        result = read("Unit 4 is vacant; units 1-3 leased at $1,100")
        assert (result.unit_count, result.confidence) == (4, "medium")
        assert [unit.in_place_rent for unit in result.units] == [1100.0, 1100.0, 1100.0, None]
        assert [unit.vacant for unit in result.units] == [False, False, False, True]
        assert result.units[0].rent_quote == "units 1-3 leased at $1,100"

    def test_investor_special_cash_only(self, read):
        result = read("Investor special 4 unit building, cash only")
        assert result.unit_count == 4
        assert {"as_is", "cash_only"} <= set(result.flags)


class TestSyntheticSet:
    """98 made-up descriptions, a quarter of them written to trip the rules."""

    def test_no_regressions(self, config):
        rows = load_labeled(SYNTHETIC)
        report = evaluate(rows, config.keywords)
        assert report.rows >= 95
        assert report.precision == 1.0, report.text()
        assert report.recall == 1.0, report.text()
        assert report.misses == [], report.text()

    def test_report_text(self, config):
        text = evaluate(load_labeled(SYNTHETIC), config.keywords).text()
        assert "Fourplex precision 100.0%" in text and "labeled descriptions" in text


class TestStructuredFacts:
    def test_a_stated_count_is_high_confidence(self, read):
        result = read(None, stated_units=4, bedrooms=8, bathrooms=4)
        assert (result.unit_count, result.confidence, result.property_kind) == (
            4,
            "high",
            "fourplex",
        )
        assert result.signals[0].name == "stated_by_source"
        assert [unit.beds for unit in result.units] == [2.0] * 4 and result.mix_confidence == "low"

    def test_text_that_agrees_with_the_stated_count(self, read):
        result = read("Solid fourplex near downtown", stated_units=4)
        assert result.confidence == "high" and "unit_count_conflict" not in result.flags

    def test_text_that_disagrees_lowers_confidence_and_flags_it(self, read):
        result = read("Well kept triplex", stated_units=4)
        assert (result.unit_count, result.confidence) == (4, "medium")
        assert "unit_count_conflict" in result.flags

    @pytest.mark.parametrize("kind", ["sfr", "condo", "townhouse"])
    def test_a_single_dwelling_is_one_unit_whatever_the_text_says(self, read, kind):
        result = read("End unit in a fourplex, 24-unit complex", source_kind=kind)
        assert (result.unit_count, result.confidence, result.property_kind) == (1, "high", kind)
        assert not result.is_fourplex

    def test_land_from_the_source(self, read):
        result = read("Fourplex plans included", source_kind="land")
        assert result.property_kind == "land" and result.confidence == "high"

    def test_land_wording_does_not_override_a_building(self, read):
        # "Lot" appears, but there are bedrooms: something is standing on it.
        result = read("Vacant lot next door also available.", bedrooms=8, bathrooms=4)
        assert result.property_kind != "land"

    def test_bed_bath_totals_are_only_a_weak_hint(self, read):
        result = read(None, bedrooms=8, bathrooms=4)
        assert (result.unit_count, result.confidence) == (4, "low")
        assert not result.is_fourplex
        assert result.signals[0].name == "bed_bath_pattern"

    @pytest.mark.parametrize(
        ("beds", "baths"), [(None, None), (8, 4.5), (3, 4), (8, 1), (8, 6), (None, 4)]
    )
    def test_no_hint_when_the_totals_do_not_fit(self, read, beds, baths):
        result = read(None, bedrooms=beds, bathrooms=baths)
        assert result.unit_count is None and result.property_kind == "multifamily_2_4"

    def test_nothing_at_all(self, read):
        result = read(None, source_kind="other")
        assert result.unit_count is None and result.confidence == "low"
        assert result.units == [] and result.mix_confidence is None

    def test_commercial_space_caps_confidence(self, read):
        result = read("4 units: storefront and three apartments above. Mixed-use, retail.")
        assert result.unit_count == 4 and result.confidence == "low"
        assert "commercial" in result.flags and not result.is_fourplex

    def test_kind_for(self):
        assert kind_for(None, "multifamily_2_4") == "multifamily_2_4"
        assert [kind_for(n, "other") for n in (1, 2, 3, 4, 5, 40)] == [
            "sfr", "duplex", "triplex", "fourplex", "multifamily_5plus", "multifamily_5plus",
        ]  # fmt: skip
        assert kind_for(1, "condo") == "condo"


class TestTextReading:
    @pytest.mark.parametrize(
        ("text", "units"),
        [
            ("Classic four-family in a great location", 4),
            ("4 family home with long-term tenants", 4),
            ("Brick 4plex, new roof", 4),
            ("Building has 4 apartments", 4),
            ("Triple decker with porches", 3),
            ("Sixplex on a corner lot", 6),
            ("Three duplexes sold as a package", 6),
            ("Duplex and triplex on one parcel", 5),
            ("Four separate electric meters and four gas meters", 4),
            ("Unit 1 and Unit 2 are updated; Unit 3 needs paint", 3),
        ],
    )
    def test_counts(self, read, text, units):
        result = read(text)
        assert result.unit_count == units, [signal.model_dump() for signal in result.signals]
        assert result.confidence in ("medium", "high")

    @pytest.mark.parametrize(
        "text",
        [
            "Duplex with room to add 2 more units",
            "Duplex, zoned for a fourplex",
            "Large duplex, potential fourplex conversion",
            "Duplex next to a fourplex",
            "Duplex. Could be converted to 4 units",
            "Legal duplex, 4 units possible",
            "Duplex that was a fourplex years ago",
        ],
    )
    def test_what_could_be_built_is_not_counted(self, read, text):
        result = read(text)
        assert result.unit_count == 2, [signal.model_dump() for signal in result.signals]
        assert not result.is_fourplex

    def test_a_lone_unit_number_proves_nothing(self, read):
        assert read("Unit 4 has a new furnace").unit_count is None

    def test_a_comparison_does_not_add_buildings(self, read):
        assert read("Fourplex that lives like a duplex").unit_count == 4

    def test_evidence_quotes_the_listing(self, read):
        result = read("Great QUADPLEX near campus")
        assert result.signals[0].evidence == "QUADPLEX"

    def test_counted_units_give_the_mix(self, read):
        result = read("Two 2BR/1BA units and two 1BR/1BA units, all rented")
        assert (result.unit_count, result.confidence, result.mix_confidence) == (4, "high", "high")
        assert [unit.beds for unit in result.units] == [2.0, 2.0, 1.0, 1.0]

    def test_a_layout_for_every_numbered_unit(self, read):
        text = "Unit 1: 2bd/1ba. Unit 2: 2bd/1ba. Unit 3: 1bd/1ba. Unit 4: studio/1ba."
        result = read(text)
        assert (result.unit_count, result.confidence, result.mix_confidence) == (4, "high", "high")
        assert [unit.beds for unit in result.units] == [2.0, 2.0, 1.0, 0.0]


class TestUnitMix:
    def test_split_whole_puts_the_remainder_first(self):
        assert split_whole(7, 4) == [2.0, 2.0, 2.0, 1.0]
        assert split_whole(8, 4) == [2.0] * 4
        assert split_whole(None, 3) == [None, None, None]
        assert split_whole(5, 0) == []

    def test_split_baths_keeps_the_half_bath(self):
        assert split_baths(4.5, 4) == [1.5, 1.0, 1.0, 1.0]
        assert split_baths(6, 4) == [2.0, 2.0, 1.0, 1.0]
        assert split_baths(None, 2) == [None, None]

    def test_even_mix(self):
        units = even_mix(4, 8, 4, 3600)
        assert [(unit.beds, unit.baths, unit.sqft) for unit in units] == [(2.0, 1.0, 900.0)] * 4
        assert all(unit.in_place_rent is None for unit in even_mix(2, None, None, None))


class TestRents:
    def rents(self, text: str, units: int | None = 4):
        return find_rents(text.lower(), text, units)

    def test_each_unit_at_one_rent(self):
        found = self.rents("All units rented at $950/mo.")
        assert found.rents == {0: 950.0, 1: 950.0, 2: 950.0, 3: 950.0}

    def test_a_list_with_one_figure_per_unit(self):
        found = self.rents("Current rents: $900, $950, $1,000 and $1,050.")
        assert found.rents == {0: 900.0, 1: 950.0, 2: 1000.0, 3: 1050.0}

    def test_a_list_of_the_wrong_length_is_ignored(self):
        assert self.rents("Rents: $900, $950 and $1,000.").rents == {}

    def test_rents_by_unit_number(self):
        found = self.rents("Unit 1 rents for $1,200. Unit 2: $1,150. Unit 3 is vacant.")
        assert found.rents == {0: 1200.0, 1: 1150.0} and found.vacant == {2}

    def test_a_vacant_unit_never_carries_a_rent(self):
        found = self.rents("Units 1-4 rented at $800. Unit 2 is vacant.")
        assert 1 not in found.rents and found.rents[0] == 800.0

    @pytest.mark.parametrize(
        "text",
        [
            "Unit 1 has a $25,000 kitchen remodel.",  # a repair bill, not a rent
            "Unit 2 roof replaced for $9,500 in 2024.",  # other words, no rent cue
            "Units 1-4 rented at $48,000.",  # an annual total, far outside a monthly rent
            "Each unit rents for $150.",  # too low to be a monthly rent
        ],
    )
    def test_amounts_that_are_not_monthly_rents(self, text):
        assert self.rents(text).rents == {}

    def test_units_past_the_count_are_dropped(self):
        assert self.rents("Unit 7 rents for $900.", units=4).rents == {}

    def test_unit_clauses_read_layout_and_rent(self):
        text = "Unit 1: 2bd/1ba, rented at $1,000. Unit 2: studio/1ba, vacant."
        first, second = list(unit_clauses(text.lower(), text))
        assert (first.unit, first.beds, first.baths, first.rent) == (0, 2.0, 1.0, 1000.0)
        assert (second.beds, second.vacant, second.rent) == (0.0, True, None)


KEYWORDS = {"as_is": ["as-is", "sold as is"], "tlc": ["tlc"], "cash_only": ["cash only"]}


class TestKeywords:
    def test_hyphens_and_spaces_are_interchangeable(self):
        assert find_flags("Sold AS IS, cash-only", KEYWORDS) == ["as_is", "cash_only"]

    def test_whole_words_only(self):
        assert find_flags("Stlc Basis cashonly", KEYWORDS) == []

    def test_order_follows_the_keyword_file(self):
        assert find_flags("cash only, needs TLC", KEYWORDS) == ["tlc", "cash_only"]

    def test_the_shipped_keywords_tag_the_usual_phrases(self, config):
        flags = find_flags(
            "Motivated seller! Estate sale, sold as-is. Tenant occupied, below market rents.",
            config.keywords,
        )
        assert {
            "motivated",
            "estate_sale",
            "as_is",
            "tenant_occupied",
            "below_market_rents",
        } <= set(flags)


class TestTypeToggles:
    @pytest.mark.parametrize(
        ("kind", "confidence", "types", "expected"),
        [
            ("fourplex", "high", ["fourplex"], "yes"),
            ("fourplex", "medium", ["fourplex"], "yes"),
            ("fourplex", "low", ["fourplex"], "unverified"),
            ("fourplex", None, ["fourplex"], "unverified"),
            ("multifamily_2_4", None, ["fourplex"], "unverified"),
            ("multifamily_2_4", "low", ["sfr"], "no"),
            ("duplex", "high", ["fourplex"], "no"),
            ("duplex", "low", ["fourplex"], "unverified"),  # a weak guess could be wrong
            ("duplex", "low", ["sfr"], "no"),
            ("duplex", "medium", ["duplex", "fourplex"], "yes"),
            ("sfr", "high", ["sfr", "condo"], "yes"),
            ("sfr", "high", ["fourplex"], "no"),
            ("multifamily_5plus", "medium", ["multifamily_5plus"], "yes"),
            ("multifamily_5plus", "medium", ["fourplex"], "no"),
            ("land", "high", ["fourplex"], "no"),
            ("other", None, ["fourplex"], "no"),
        ],
    )
    def test_kind_matches(self, kind, confidence, types, expected):
        assert kind_matches(kind, confidence, types, "medium") == expected

    def test_the_required_confidence_is_a_setting(self):
        assert kind_matches("fourplex", "medium", ["fourplex"], "high") == "unverified"
        assert kind_matches("fourplex", "low", ["fourplex"], "low") == "yes"

    def test_matches_types_reads_a_classification(self, read):
        assert matches_types(read("Quadplex"), ["fourplex"], "medium") == "yes"
        assert matches_types(read(None, bedrooms=8, bathrooms=4), ["fourplex"], "medium") == (
            "unverified"
        )


class TestLabeledFiles:
    def write(self, tmp_path, text: str) -> Path:
        path = tmp_path / "labeled.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_labels_and_optional_columns(self, tmp_path):
        path = self.write(
            tmp_path,
            "description,units,source_kind,stated_units,bedrooms,bathrooms\n"
            "Quadplex,4,,,8,4\n"
            "Empty lot,land,land,,,\n"
            "Nice building,unknown,,,,\n"
            ",4,,,,\n"  # no description: skipped
            "Fourplex,,,,,\n",  # no label: skipped
        )
        rows = load_labeled(path)
        assert [row.label for row in rows] == [4, "land", "unknown"]
        assert rows[0].entry.bedrooms == 8 and rows[0].entry.source_kind == "multifamily_2_4"
        assert rows[1].entry.source_kind == "land" and rows[0].row == 2

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("text,count\nQuadplex,4\n", "needs a 'description' column"),
            ("description,units\nQuadplex,several\n", "must be a number, land or unknown"),
            ("description,units,source_kind\nQuadplex,4,castle\n", "unknown source_kind"),
        ],
    )
    def test_bad_files_say_what_is_wrong(self, tmp_path, text, message):
        with pytest.raises(ValueError, match=message):
            load_labeled(self.write(tmp_path, text))

    def test_misses_are_listed(self, tmp_path, config):
        path = self.write(
            tmp_path,
            "description,units\n"
            "Quadplex near the river,4\n"
            "Quadplex near the lake,3\n"  # labeled 3, so the classifier's 4 is a false positive
            "Lovely building,4\n",  # a fourplex the text never reveals
        )
        report = evaluate(load_labeled(path), config.keywords)
        assert (report.fourplex_called, report.fourplex_right, report.fourplex_actual) == (2, 1, 2)
        assert report.precision == 0.5 and report.recall == 0.5
        assert [miss.row for miss in report.misses] == [3, 4]
        assert "row 3: expected 3, got 4 (medium)" in report.text()

    def test_empty_report(self, config):
        report = evaluate([], config.keywords)
        assert report.precision is None and "n/a" in report.text()
