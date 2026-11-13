"""The dashboard: its data layer, the HTML it writes, the exports, and each page rendered
without a browser."""

from __future__ import annotations

import io

import pytest
import yaml
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from dealfinder.analysis import DISCLAIMER
from dealfinder.export import deals_frame, rents_note, to_csv, to_xlsx
from dealfinder.pipeline.classify import classify_all
from dealfinder.settings import Environment, load_config
from dealfinder.store.db import session_scope
from dealfinder.store.tables import PropertyRow
from dealfinder.ui import data, theme
from test_classify_pipeline import rentcast_listing, store
from test_funnel import fourplex, funnel


@pytest.fixture
def offline(tmp_path):
    from dealfinder.http import Fetcher

    return Fetcher(tmp_path / "cache", offline=True)


@pytest.fixture
def seeded(engine, config, offline, monkeypatch):
    """Six confirmed fourplexes in one county (two with stated rents), one unconfirmed
    listing and one that cannot be underwritten. Market data is connected for every ZIP, both
    for this first run and for anything the dashboard underwrites again."""
    from dealfinder.pipeline import underwrite
    from factories import market_context

    monkeypatch.setattr(
        underwrite, "load_market", lambda zip_code, *_args, **_kwargs: market_context(zip_code)
    )
    store(
        engine,
        fourplex(1, 200_000.0, "Quadplex. All units rented at $1,200/mo.", days_on_market=140),
        fourplex(2, 250_000.0, "Quadplex. Motivated seller."),
        fourplex(3, 300_000.0, "Quadplex. Units 1-3 leased at $1,000. Unit 4 is vacant."),
        fourplex(4, 350_000.0),
        fourplex(5, 400_000.0),
        fourplex(6, 450_000.0, "Quadplex <script>alert(1)</script> & more"),
        rentcast_listing(7, price=220_000.0),
        fourplex(8, 300_000.0, bedrooms=None, bathrooms=None),
    )
    classify_all(engine, config)
    funnel(engine, config, offline)
    return engine


class TestTheme:
    def test_listing_text_is_escaped(self):
        cell = theme.value('<b>1</b> "x"', estimated=True, tip='<img src=x onerror="y">')
        assert "<b>" not in cell and "&lt;b&gt;1&lt;/b&gt;" in cell
        assert "<img" not in cell and "&quot;y&quot;" in cell
        assert theme.esc(None) == "" and theme.esc("a & b") == "a &amp; b"

    def test_estimates_are_italic_and_actuals_are_not(self):
        assert (
            theme.value("$100", estimated=True, tip="HUD")
            == '<span title="HUD"><em>$100</em></span>'
        )
        assert theme.value("$100", estimated=False) == "<span>$100</span>"
        assert "!</span>" in theme.value("$100", estimated=True, flagged=True)

    def test_table(self):
        html = theme.table(
            [("Deal", "the address"), "Price"], [["a", "b"], ["c", "d"]], wrap=(1,), total_row=1
        )
        assert '<th class="l" title="the address">Deal</th>' in html and "<th>Price</th>" in html
        assert '<td class="l">a</td><td class="wrap">b</td>' in html
        assert '<tr class="total">' in html and html.startswith('<div class="scroll"><table>')

    def test_grade_pills_use_one_ramp_per_theme(self):
        light, dark = theme.grade_pill("A", "light"), theme.grade_pill("A", "dark")
        assert "#104281" in light and "#b7d3f6" in dark and ">A</span>" in light
        assert theme.grade_pill(None, "light") == ""
        assert set(theme.GRADES["light"]) == set(theme.GRADES["dark"]) == set("ABCDF")

    def test_meter_list_is_clamped(self):
        html = theme.meter_list(
            [("Cash flow", 30.0, 25.0, "over"), ("Upside", -2.0, 10.0, "under")]
        )
        assert "width:100.0%" in html and "width:0.0%" in html and "30.0 / 25" in html

    def test_formatting(self):
        assert theme.money(-1234.5) == "-$1,234" or theme.money(-1234.5) == "-$1,235"
        assert theme.money(None) == "n/a" and theme.money(12.345, cents=True) == "$12.35"
        assert theme.percent(0.0712, 2) == "7.12%" and theme.ratio(1.2345) == "1.23"
        assert theme.block("light", "<p>x</p>").endswith('<div class="df"><p>x</p></div>')


class TestData:
    def test_load_and_filter(self, seeded, config):
        deals = data.load_deals(seeded, config)
        assert len(deals.rows) == 7 and len(deals.problems) == 1
        confirmed = data.apply_filter(deals.rows, data.DealFilter(), "yes")
        assert confirmed[0].listing_id == 1  # best cash flow per door first
        assert len(confirmed) == 6
        maybe = data.apply_filter(deals.rows, data.DealFilter(), "unverified")
        assert [row.listing_id for row in maybe] == [7]

        cheapest = data.apply_filter(
            deals.rows, data.DealFilter(sort="Price", descending=False, max_price=260_000)
        )
        assert [row.price for row in cheapest] == [200_000.0, 250_000.0]
        boxed = data.apply_filter(deals.rows, data.DealFilter(buy_box_only=True))
        assert boxed and all(row.in_buy_box for row in boxed)
        assert data.apply_filter(deals.rows, data.DealFilter(states=["IN"])) == []
        found = data.apply_filter(deals.rows, data.DealFilter(search="103 sample"))
        assert [row.listing_id for row in found] == [3]
        flows = data.apply_filter(deals.rows, data.DealFilter(min_cash_flow=100.0))
        assert all(row.cash_flow_door_month >= 100 for row in flows)
        graded = data.apply_filter(deals.rows, data.DealFilter(grades=["A", "B"]))
        assert all(row.grade in ("A", "B") for row in graded)

    def test_provenance_separates_actual_from_estimated(self, seeded, config):
        deals = data.load_deals(seeded, config)
        stated, estimated = deals.provenance[1], deals.provenance[4]
        assert not stated.rents_estimated and "current rents" in stated.actual
        assert estimated.rents_estimated and "market rents" in estimated.estimated
        assert "insurance" in estimated.flagged
        assert stated.text().startswith("Actual: price, current rents")
        partial = deals.provenance[3]  # three rents stated, one unit vacant
        assert partial.rents_estimated

    def test_county_summaries(self, seeded, config):
        deals = data.load_deals(seeded, config)
        counties = data.county_summaries([row for row in deals.rows if row.type_match == "yes"])
        assert len(counties) == 1
        county = counties[0]
        assert (county.name, county.listings) == ("Franklin County, OH", 6)
        assert county.median_price_per_door == 81_250
        assert county.in_buy_box == sum(
            1 for r in deals.rows if r.in_buy_box and r.type_match == "yes"
        )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1200, 1150,vacant ,$1,100", None),  # the comma inside $1,100 breaks the count
            ("1200, 1150, vacant, 1100", [1200.0, 1150.0, None, 1100.0]),
            ("$900,$900,$950,$950", [900.0, 900.0, 950.0, 950.0]),
            ("  ", None),
        ],
    )
    def test_parse_amounts(self, text, expected):
        if expected is None and text.strip():
            with pytest.raises(ValueError, match="amounts were given for 4 units"):
                data.parse_amounts(text, 4)
        else:
            assert data.parse_amounts(text, 4) == expected

    def test_parse_amounts_rejects_words(self):
        with pytest.raises(ValueError, match="'lots' is not an amount"):
            data.parse_amounts("1200, lots, 1100, 1000", 4)

    def test_your_figures_are_saved_and_the_deal_is_underwritten_again(self, seeded, config):
        environment = Environment()
        before = {row.listing_id: row for row in data.load_deals(seeded, config).rows}[4]
        problem = data.save_overrides(
            seeded, config, environment, 4, 4,
            {"price": 300_000, "rents": [1300.0] * 4, "not_a_field": 1},
        )  # fmt: skip
        assert problem is None
        after = {row.listing_id: row for row in data.load_deals(seeded, config).rows}[4]
        assert after.price == 300_000 and after.cash_flow_door_month > before.cash_flow_door_month
        assert after.rents_stated == 0  # the listing still states none; the figures are yours
        assert after.score is not None  # scored again, against the county medians of the last run
        assert data.overrides_for(seeded, 4) == {"price": 300_000, "rents": [1300.0] * 4}
        notes = data.load_deals(seeded, config).provenance[4]
        assert "price (your figure)" in notes.actual

        assert data.remove_overrides(seeded, config, environment, 4, 4) is None
        assert data.overrides_for(seeded, 4) == {}
        restored = {row.listing_id: row for row in data.load_deals(seeded, config).rows}[4]
        assert restored.price == 350_000.0

    def test_a_figure_that_breaks_the_deal_is_reported(self, seeded, config):
        problem = data.save_overrides(seeded, config, Environment(), 4, 4, {"rents": [1.0, 2.0]})
        assert problem is not None and "A figure is not usable" in problem

    def test_star_hide_and_extras(self, seeded, config):
        data.set_star(seeded, 1, True)
        data.set_hidden(seeded, 2, True)
        with session_scope(seeded) as session:
            rows = {row.id: row for row in session.scalars(select(PropertyRow))}
            assert rows[1].starred and rows[2].hidden
        assert data.hidden_properties(seeded) == [(2, "102 Sample St, Columbus, OH")]
        extras = data.listing_extras(seeded, 1)
        assert extras["description"].startswith("Quadplex. All units rented")
        assert extras["history"][0]["Event"] == "listed" and extras["signals"]
        assert data.listing_extras(seeded, 999) == {}

    def test_budget_status(self, engine, config):
        status = data.budget_status(engine, config)
        assert (status["used"], status["cap"], status["left"]) == (0, 50, 50)
        assert status["plan"] == "developer" and status["cost"] == 0

    def test_settings_are_saved_to_the_local_file(self, config_dir):
        config = load_config(config_dir)
        path = data.save_local_settings(
            config, {"buy_box": {"property_types": ["duplex", "fourplex"], "max_price": 500_000}}
        )
        assert path == config_dir / "settings.local.yaml"
        assert load_config(config_dir).settings.buy_box.property_types == ["duplex", "fourplex"]
        # A second save merges with the first instead of replacing it.
        data.save_local_settings(config, {"buy_box": {"min_unit_confidence": "high"}})
        saved = yaml.safe_load(path.read_text())
        assert saved["buy_box"] == {
            "property_types": ["duplex", "fourplex"],
            "max_price": 500_000,
            "min_unit_confidence": "high",
        }

    def test_a_bad_setting_is_never_written(self, config_dir):
        config = load_config(config_dir)
        with pytest.raises(Exception, match="property_types"):
            data.save_local_settings(config, {"buy_box": {"property_types": ["castle"]}})
        assert not (config_dir / "settings.local.yaml").exists()
        data.save_local_settings(config, {"buy_box": {"max_price": 400_000}})
        with pytest.raises(Exception, match="min_unit_confidence"):
            data.save_local_settings(config, {"buy_box": {"min_unit_confidence": "certain"}})
        kept = yaml.safe_load((config_dir / "settings.local.yaml").read_text())
        assert kept == {"buy_box": {"max_price": 400_000}}  # the earlier good save survives


class TestExport:
    def test_frame_and_notes(self, seeded, config):
        rows = data.apply_filter(data.load_deals(seeded, config).rows, data.DealFilter())
        frame = deals_frame(rows)
        assert list(frame["Rank"]) == [1, 2, 3, 4, 5, 6]
        assert frame.loc[0, "Rents"] == "actual: stated in the listing"
        notes = {row.listing_id: rents_note(row) for row in rows}
        assert notes[3] == "3 of 4 stated in the listing, the rest estimated"
        assert notes[4] == "estimated"
        assert set(frame["In buy box"]) <= {"yes", "no"} and frame["Units"].tolist() == [4] * 6

    def test_csv(self, seeded, config):
        rows = data.apply_filter(data.load_deals(seeded, config).rows, data.DealFilter())
        text = to_csv(rows).decode("utf-8")
        assert text.startswith("﻿Rank,Grade,Score,Cash flow per door per month")
        lines = text.strip().splitlines()
        assert len(lines) == 1 + 6 + 1 and lines[-1] == f'"{DISCLAIMER}"'
        assert "101 Sample St" in lines[1]

    def test_xlsx(self, seeded, config):
        from openpyxl import load_workbook

        rows = data.apply_filter(data.load_deals(seeded, config).rows, data.DealFilter())
        sheet = load_workbook(io.BytesIO(to_xlsx(rows)))["Deals"]
        headings = [cell.value for cell in sheet[1]]
        assert headings[:4] == ["Rank", "Grade", "Score", "Cash flow per door per month"]
        price = sheet.cell(row=2, column=headings.index("Price") + 1)
        assert price.value == 200_000 and price.number_format == '"$"#,##0'
        assert sheet.freeze_panes == "A2"
        assert sheet.cell(row=len(rows) + 3, column=1).value == DISCLAIMER

    def test_no_deals(self):
        assert to_csv([]).decode("utf-8").startswith("﻿Rank,Grade")
        assert to_xlsx([])[:2] == b"PK"


def page(name: str, **state) -> AppTest:
    """Run one dashboard page without a browser."""

    def script(page_name: str) -> None:
        import streamlit as st

        from dealfinder.ui import views

        st.session_state.setdefault("_pages", {})
        getattr(views, page_name)()

    app = AppTest.from_function(script, args=(name,), default_timeout=60)
    for key, item in state.items():
        app.session_state[key] = item
    return app.run()


def html_of(app: AppTest) -> str:
    return "\n".join(str(block.proto.body) for block in app.get("html"))


class TestPages:
    @pytest.mark.parametrize(
        ("name", "message"),
        [
            ("deals_page", "Nothing has been underwritten yet"),
            ("map_page", "No underwritten deals to map yet"),
            ("deal_page", "No underwritten deals yet"),
            ("markets_page", "No underwritten deals yet"),
        ],
    )
    def test_an_empty_database_says_what_to_do(self, engine, name, message):
        app = page(name)
        assert not app.exception
        assert any(message in info.value for info in app.info)

    def test_deals(self, seeded):
        app = page("deals_page")
        assert not app.exception and app.title[0].value == "Deals"
        assert app.metric[0].label == "Deals shown" and app.metric[0].value == "6"
        assert [tab.label for tab in app.tabs] == [
            "Ranked (6)",
            "Unit count unconfirmed (1)",
            "Could not be underwritten (1)",
        ]
        body = html_of(app)
        assert "<em>Italic</em> figures are estimates" in body
        assert "101 Sample St, Columbus, OH" in body
        # Listing 1 states every rent: its cash flow is upright. Listing 4 states none: italic.
        assert "4/4 stated" in body and "<em>estimated</em>" in body
        assert "Rents are stated in the listing." in body and "Rests on estimated rents." in body
        assert "<script>" not in body  # text from a listing never reaches the page as markup

    def test_deals_filters(self, seeded):
        app = page("deals_page")
        app.toggle[0].set_value(True).run()  # only inside the buy box
        assert not app.exception
        shown = int(app.metric[0].value)
        assert 1 <= shown < 6 and app.metric[1].value == str(shown)

    def test_map(self, seeded):
        app = page("map_page")
        assert not app.exception and app.title[0].value == "Map"
        assert "Grade A" in html_of(app)  # the legend names the colors

    def test_deal(self, seeded):
        app = page("deal_page", listing_id=3)
        assert not app.exception
        assert app.subheader[0].value == "103 Sample St, Columbus, OH, 43215"
        labels = [metric.label for metric in app.metric]
        assert labels[0] == "Cash flow / door, est." and "Score" in labels
        body = html_of(app)
        assert "stated in the listing: $1,000, $1,000, $1,000, unknown" in body  # an input's source
        assert "Net operating income" in body and "Cash flow per door" in body
        assert "vacant" in body and "units 1-3 leased at $1,000" in body.lower()
        assert [tab.label for tab in app.tabs] == [
            "Overview", "Inputs", "Units and rents", "Loans compared", "Hold and offer", "Score",
            "Listing", "Your figures",
        ]  # fmt: skip

    def test_deal_with_stated_rents_is_not_marked_estimated(self, seeded):
        app = page("deal_page", listing_id=1)
        assert not app.exception
        assert app.metric[0].label == "Cash flow / door"

    def test_markets(self, seeded):
        app = page("markets_page")
        assert not app.exception
        assert app.subheader[0].value == "Counties with underwritten deals"
        assert len(app.get("plotly_chart")) == 1
        assert "Franklin County, OH" in str(app.dataframe[0].value)

    def test_markets_zip_lookup(self, seeded):
        app = page("markets_page")
        app.text_input[0].set_value("43215").run()
        assert not app.exception
        assert any("43215 Franklin County, OH" in item.value for item in app.markdown)
        # Offline with no keys: each source says why it is not connected.
        assert any("HUD_API_TOKEN is not set" in caption.value for caption in app.caption)
        app.text_input[0].set_value("00000").run()
        assert any("not a ZIP code this tool knows" in w.value for w in app.warning)

    def test_settings(self, seeded, config_dir, monkeypatch):
        monkeypatch.setenv("DEALFINDER_CONFIG_DIR", str(config_dir))
        app = page("settings_page")
        assert not app.exception and app.title[0].value == "Settings"
        body = html_of(app)
        assert "SCRAPER, off by default" in body and "RENTCAST_API_KEY" in body
        assert "not set" in body
        assert app.metric[0].value == "0 of 50"

        app.number_input[0].set_value(400_000.0)  # highest price
        app.button[0].click().run()  # Save
        assert not app.exception
        assert any("Saved to" in item.value for item in app.success)
        assert load_config(config_dir).settings.buy_box.max_price == 400_000

    def test_the_disclaimer_is_on_the_real_app(self, seeded):
        from pathlib import Path

        import dealfinder.ui.app as entry

        app = AppTest.from_file(str(Path(entry.__file__)), default_timeout=60).run()
        assert not app.exception
        assert app.caption[-1].value == DISCLAIMER
