"""The deal score: straight-line points per part, penalties, the confidence multiplier, grades."""

from __future__ import annotations

import pytest

from dealfinder.score import ScoreInputs, grade_for, scaled, score
from dealfinder.settings import Scale


@pytest.fixture
def rules(config):
    return config.settings.scoring


def inputs(**changes) -> ScoreInputs:
    """A deal that earns full marks on every part."""
    fields = {
        "cash_flow_door_month": 300.0,
        "coc": 0.12,
        "dscr": 1.5,
        "has_debt": True,
        "cap_rate": 0.09,
        "price_per_door": 70_000.0,
        "county_cap_rate": 0.07,
        "county_price_per_door": 100_000.0,
        "county_name": "Sample County",
        "days_on_market": 120,
        "price_cut_pct": 0.10,
        "tags": ["motivated"],
        "rent_upside": 0.20,
        "confidence": "high",
    }
    fields.update(changes)
    return ScoreInputs(**fields)


def part(result, key: str):
    return next(p for p in [*result.parts, *result.penalties] if p.key == key)


class TestScale:
    SCALE = Scale(weight=10, zero_at=1.0, full_at=1.5)

    @pytest.mark.parametrize(
        ("value", "points"),
        [(1.0, 0.0), (0.4, 0.0), (1.25, 5.0), (1.5, 10.0), (3.0, 10.0), (None, 0.0)],
    )
    def test_straight_line_between_the_ends(self, value, points):
        assert scaled(value, self.SCALE) == pytest.approx(points)

    def test_a_scale_with_no_width_is_a_threshold(self):
        step = Scale(weight=10, zero_at=2.0, full_at=2.0)
        assert (scaled(1.99, step), scaled(2.0, step)) == (0.0, 10.0)


class TestScore:
    def test_the_weights_add_up_to_100(self, rules):
        weights = [
            rules.cash_flow_per_door.weight,
            rules.coc.weight,
            rules.dscr.weight,
            rules.cap_rate_vs_county.weight,
            rules.price_per_door_vs_county.weight,
            rules.seller_motivation.weight,
            rules.rent_upside.weight,
        ]
        assert weights == [25, 20, 15, 10, 10, 10, 10]

    def test_full_marks(self, rules):
        result = score(inputs(), rules)
        assert result.total == 100.0 and result.grade == "A"
        assert [p.points for p in result.parts] == [25, 20, 15, 10, 10, 10, 10]
        assert result.penalties == [] and result.multiplier == 1.0

    def test_half_way_on_the_three_cash_parts(self, rules):
        result = score(inputs(cash_flow_door_month=150, coc=0.06, dscr=1.25), rules)
        assert part(result, "cash_flow_per_door").points == pytest.approx(12.5)
        assert part(result, "coc").points == pytest.approx(10.0)
        assert part(result, "dscr").points == pytest.approx(7.5)
        assert result.total == pytest.approx(70.0)
        assert result.grade == "B"

    def test_nothing_below_the_zero_points(self, rules):
        result = score(inputs(cash_flow_door_month=-200, coc=-0.03, dscr=0.8), rules)
        assert [part(result, key).points for key in ("cash_flow_per_door", "coc", "dscr")] == [
            0.0,
            0.0,
            0.0,
        ]

    def test_a_cash_purchase_gets_full_debt_coverage(self, rules):
        result = score(inputs(dscr=None, has_debt=False), rules)
        assert part(result, "dscr").points == 15 and "no loan" in part(result, "dscr").detail

    def test_county_comparisons(self, rules):
        # One point of cap rate above the median is half of the two it takes for full marks;
        # 15% below the median price per door is half of 30%.
        result = score(inputs(cap_rate=0.08, price_per_door=85_000), rules)
        assert part(result, "cap_rate_vs_county").points == pytest.approx(5.0)
        assert part(result, "price_per_door_vs_county").points == pytest.approx(5.0)
        assert "Sample County median of 7.0%" in part(result, "cap_rate_vs_county").detail

        at_median = score(inputs(cap_rate=0.07, price_per_door=100_000), rules)
        assert part(at_median, "cap_rate_vs_county").points == 0
        assert part(at_median, "price_per_door_vs_county").points == 0

    def test_no_county_sample_means_no_county_points(self, rules):
        result = score(inputs(county_cap_rate=None, county_price_per_door=None), rules)
        assert part(result, "cap_rate_vs_county").points == 0
        assert "too few listings" in part(result, "price_per_door_vs_county").detail
        assert result.total == 80.0

    @pytest.mark.parametrize(
        ("changes", "points"),
        [
            ({"days_on_market": 60, "price_cut_pct": None, "tags": []}, 2.0),
            ({"days_on_market": 400, "price_cut_pct": None, "tags": []}, 4.0),
            ({"days_on_market": None, "price_cut_pct": 0.05, "tags": []}, 2.0),
            ({"days_on_market": None, "price_cut_pct": None, "tags": ["as_is", "tlc"]}, 2.0),
            (
                {
                    "days_on_market": None,
                    "price_cut_pct": None,
                    "tags": [],
                    "listing_type": "Foreclosure",
                },
                2.0,
            ),
            (
                {
                    "days_on_market": 500,
                    "price_cut_pct": 0.5,
                    "tags": ["motivated", "estate_sale", "as_is"],
                },
                10.0,
            ),
            ({"days_on_market": None, "price_cut_pct": None, "tags": []}, 0.0),
        ],
    )
    def test_seller_motivation(self, rules, changes, points):
        result = score(inputs(**changes), rules)
        assert part(result, "seller_motivation").points == pytest.approx(points)

    def test_rent_upside(self, rules):
        assert part(score(inputs(rent_upside=0.10), rules), "rent_upside").points == 5.0
        unknown = part(score(inputs(rent_upside=None), rules), "rent_upside")
        assert unknown.points == 0 and "needs both" in unknown.detail
        assert part(score(inputs(rent_upside=-0.1), rules), "rent_upside").points == 0

    def test_penalties(self, rules):
        result = score(inputs(in_flood_zone=True, high_hazard=True), rules)
        assert [p.points for p in result.penalties] == [-5, -5] and result.total == 90.0
        unchecked = score(inputs(high_hazard=True, hazard_checked=False), rules)
        assert "not loaded" in part(unchecked, "high_hazard").detail

    @pytest.mark.parametrize(
        ("confidence", "total"), [("high", 100.0), ("medium", 85.0), ("low", 70.0)]
    )
    def test_confidence_multiplier(self, rules, confidence, total):
        result = score(inputs(confidence=confidence), rules)
        assert result.total == total and result.before_multiplier == 100.0

    def test_the_total_stays_between_0_and_100(self, rules):
        worst = inputs(
            cash_flow_door_month=-500, coc=-0.2, dscr=0.5, cap_rate=0.01, price_per_door=200_000,
            days_on_market=None, price_cut_pct=None, tags=[], rent_upside=None,
            in_flood_zone=True, high_hazard=True, confidence="low",
        )  # fmt: skip
        result = score(worst, rules)
        assert result.total == 0.0 and result.grade == "F" and result.before_multiplier == -10.0

    @pytest.mark.parametrize(
        ("total", "grade"),
        [
            (100, "A"),
            (80, "A"),
            (79.9, "B"),
            (65, "B"),
            (64.9, "C"),
            (50, "C"),
            (35, "D"),
            (34.9, "F"),
        ],
    )
    def test_grades(self, rules, total, grade):
        assert grade_for(total, rules.grades) == grade

    def test_settings_change_the_score(self, rules):
        rules.cash_flow_per_door.full_at = 600
        assert part(score(inputs(), rules), "cash_flow_per_door").points == pytest.approx(12.5)
