"""The dashboard's pages. Each one reads through ``ui.data`` and draws with Streamlit."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
import yaml

from dealfinder.export import rents_note, to_csv, to_xlsx
from dealfinder.geo import CONTIGUOUS_STATES, county_name
from dealfinder.http import Fetcher
from dealfinder.market import hud
from dealfinder.market.context import load_market
from dealfinder.pipeline.report import DealRow, deal_detail
from dealfinder.settings import Config, Environment, SettingsError, load_config
from dealfinder.store.db import open_database, session_scope
from dealfinder.store.overrides import FIELDS
from dealfinder.ui import data, theme
from dealfinder.ui.theme import esc, money, percent, ratio

PROPERTY_TYPES = ["sfr", "condo", "townhouse", "duplex", "triplex", "fourplex", "multifamily_5plus"]


# ---- shared --------------------------------------------------------------------------------------


@st.cache_resource
def _engine(url: str):  # type: ignore[no-untyped-def]
    return open_database(url)


def _context() -> tuple[Config, Environment, Any]:
    """Settings, environment and database, or a clear message and a stop."""
    try:
        config = load_config()
    except SettingsError as error:
        st.error(f"The settings could not be loaded: {error}")
        st.stop()
    environment = Environment()
    return config, environment, _engine(environment.dealfinder_db_url)


def _mode() -> theme.Mode:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # no browser attached, as in tests
        return "light"


def _html(*parts: str) -> None:
    st.html(theme.block(_mode(), *parts))


RANKED = "Each deal's ranking loan"


def _scenario_picker(config: Config, key: str) -> str | None:
    """The loan to show deals under; None means each deal under the loan it is ranked by."""
    presets = config.settings.financing.presets
    box = config.settings.buy_box
    ranking = (box.ranking_scenario, box.ranking_scenario_5plus)
    names: list[str | None] = [None]
    names += [name for name, preset in presets.items() if preset.enabled or name in ranking]
    return st.sidebar.selectbox(
        "Loan scenario",
        names,
        index=0,
        format_func=lambda name: RANKED if name is None else presets[name].label or name,
        key=key,
        help="Stage 1 runs only each deal's ranking loan, so other loans list fewer deals.",
    )


def _scenario_label(config: Config, scenario: str | None) -> str:
    if scenario is None:
        return RANKED.lower()
    return config.settings.financing.presets[scenario].label or scenario


def _kpi(row: DealRow, text: str, note: data.Provenance | None) -> str:
    """A computed figure in the ranked table: italic when its rents are estimated."""
    tip = note.text() if note else ""
    estimated = note.rents_estimated if note else True
    lead = "Rests on estimated rents. " if estimated else "Rents are stated in the listing. "
    return theme.value(text, estimated=estimated, tip=lead + tip)


def _deal_table(rows: list[DealRow], notes: dict[int, data.Provenance]) -> str:
    mode = _mode()
    headers: list[str | tuple[str, str]] = [
        "#",
        ("Grade", "A is 80 and up, B 65, C 50, D 35; see Settings"),
        "Address",
        ("Cash flow / door", "per month, year one, every unit rented"),
        ("CoC", "cash flow over cash invested"),
        ("DSCR", "net operating income over debt service"),
        ("Cap rate", "net operating income over price"),
        ("Score", "0 to 100, after the unit-count confidence multiplier"),
        "Price",
        "Price / door",
        "Units",
        ("Rents", "whether the listing states the rents or they are estimates"),
        ("Box", "inside the buy box"),
        ("Stage", "1: free data only. 2: enriched with rent estimates, records and flood"),
        ("DOM", "days on market"),
    ]
    body = []
    for rank, row in enumerate(rows, start=1):
        note = notes.get(row.listing_id)
        stated = row.rents_stated
        total = stated + row.rents_estimated
        rents = (
            theme.value(f"{stated}/{total} stated", estimated=False, tip=rents_note(row))
            if total and stated == total
            else theme.value(
                f"{stated}/{total} stated" if stated else "estimated",
                estimated=True,
                tip=rents_note(row),
            )
        )
        box = (
            '<span title="inside the buy box">yes</span>'
            if row.in_buy_box
            else f'<span title="{esc("; ".join(row.buy_box_misses))}">no</span>'
        )
        star = "&#9733; " if row.starred else ""
        where = ", ".join(part for part in (row.address, row.city, row.state) if part)
        body.append(
            [
                str(rank),
                theme.grade_pill(row.grade, mode),
                f'<span title="{esc(row.full_address)}">{star}{esc(where)}</span>',
                _kpi(row, money(row.cash_flow_door_month), note),
                _kpi(row, percent(row.coc), note),
                _kpi(row, ratio(row.dscr), note),
                _kpi(row, percent(row.cap_rate), note),
                _kpi(row, "" if row.score is None else f"{row.score:.0f}", note),
                theme.value(money(row.price), estimated=False, tip=f"list price, {row.source}"),
                theme.value(money(row.price_per_door), estimated=False, tip="price over units"),
                theme.value(
                    "?" if row.units is None else str(row.units),
                    estimated=row.unit_confidence != "high",
                    tip=f"unit count confidence: {row.unit_confidence or 'none'}",
                ),
                rents,
                box,
                str(row.stage),
                "" if row.days_on_market is None else str(row.days_on_market),
            ]
        )
    return theme.table(headers, body, left=(1, 2))


def _open_deal(listing_id: int) -> None:
    st.session_state["listing_id"] = listing_id
    st.switch_page(st.session_state["_pages"]["Deal"])


def _deal_label(row: DealRow) -> str:
    return (
        f"{row.grade or '-'}  {money(row.cash_flow_door_month)}/door  "
        f"{money(row.price)}  {row.full_address}"
    )


# ---- Deals ---------------------------------------------------------------------------------------


def deals_page() -> None:
    config, _, engine = _context()
    scenario = _scenario_picker(config, "deals_scenario")
    deals = data.load_deals(engine, config, scenario)
    st.title("Deals")

    if not deals.rows and not deals.problems:
        st.info(
            "Nothing has been underwritten yet. Import a list with "
            "`dealfinder import-csv FILE --mapping NAME` or fetch listings with "
            "`dealfinder sweep --mode weekly --states OH`, then run `dealfinder underwrite`."
        )
        return

    with st.sidebar:
        states = sorted({row.state for row in deals.rows if row.state})
        chosen = data.DealFilter(
            states=st.multiselect("States", states),
            grades=st.multiselect("Grades", ["A", "B", "C", "D", "F"]),
            buy_box_only=st.toggle("Only deals inside the buy box", value=False),
            stage2_only=st.toggle("Only enriched deals (stage 2)", value=False),
            min_cash_flow=st.number_input(
                "Cash flow per door at least", value=None, step=50.0, placeholder="any"
            ),
            max_price=st.number_input(
                "Price at most", value=None, step=25_000.0, placeholder="any"
            ),
            search=st.text_input("Address contains"),
            sort=st.selectbox("Sort by", list(data.SORTS)),
            descending=st.toggle("Highest first", value=True),
        )
        limit = st.slider("Rows to show", 10, 200, 50, step=10)

    confirmed = data.apply_filter(deals.rows, chosen, "yes")
    unverified = data.apply_filter(deals.rows, chosen, "unverified")
    flows = [r.cash_flow_door_month for r in confirmed if r.cash_flow_door_month is not None]
    tiles = st.columns(4)
    tiles[0].metric("Deals shown", f"{len(confirmed):,}")
    tiles[1].metric("Inside the buy box", f"{sum(1 for r in confirmed if r.in_buy_box):,}")
    tiles[2].metric("Graded A or B", f"{sum(1 for r in confirmed if r.grade in ('A', 'B')):,}")
    tiles[3].metric(
        "Median cash flow per door",
        money(pd.Series(flows).median()) if flows else "n/a",
        help="per month, across the deals shown",
    )
    label = _scenario_label(config, deals.scenario)
    st.caption(f"Underwritten under: {label}. Ranked by {chosen.sort.lower()}.")

    ranked, maybe, failed = st.tabs(
        [
            f"Ranked ({len(confirmed)})",
            f"Unit count unconfirmed ({len(unverified)})",
            f"Could not be underwritten ({len(deals.problems)})",
        ]
    )
    with ranked:
        _ranked_tab(confirmed, deals, limit, "ranked")
    with maybe:
        st.caption(
            "These might be a wanted property type, but nothing confirms the unit count, so "
            "their scores are cut and they stay outside the buy box. "
            "`dealfinder resolve-units` looks the counts up, one RentCast request each."
        )
        _ranked_tab(unverified, deals, limit, "unverified")
    with failed:
        if not deals.problems:
            st.success("Every listing of a wanted type was underwritten.")
        else:
            st.dataframe(
                pd.DataFrame(
                    {
                        "Price": [row.price for row in deals.problems],
                        "Type": [row.kind.replace("_", " ") for row in deals.problems],
                        "Units": [row.units for row in deals.problems],
                        "Address": [
                            ", ".join(p for p in (row.address, row.city, row.state) if p)
                            for row in deals.problems
                        ],
                        "Why": [row.problem for row in deals.problems],
                    }
                ),
                hide_index=True,
                column_config={"Price": st.column_config.NumberColumn(format="dollar")},
            )


def _ranked_tab(rows: list[DealRow], deals: data.Deals, limit: int, key: str) -> None:
    if not rows:
        st.info("No deals match. Loosen the filters in the sidebar.")
        return
    shown = rows[:limit]
    _html(theme.note(theme.LEGEND), _deal_table(shown, deals.provenance))
    if len(rows) > limit:
        st.caption(f"Showing {limit} of {len(rows):,}. Raise 'Rows to show' for more.")
    pick, go, csv, xlsx = st.columns([5, 1, 1, 1], vertical_alignment="bottom")
    chosen = pick.selectbox(
        "Open a deal", shown, format_func=_deal_label, key=f"{key}_pick", index=0
    )
    if go.button("Open", key=f"{key}_open", type="primary"):
        _open_deal(chosen.listing_id)
    csv.download_button(
        "CSV", to_csv(rows), file_name=f"deals_{deals.scenario or 'ranked'}.csv", mime="text/csv",
        key=f"{key}_csv", help="Every deal in this list, not only the rows shown.",
    )  # fmt: skip
    xlsx.download_button(
        "Excel",
        to_xlsx(rows),
        file_name=f"deals_{deals.scenario or 'ranked'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{key}_xlsx",
    )


# ---- Map -----------------------------------------------------------------------------------------


def map_page() -> None:
    import pydeck as pdk

    config, _, engine = _context()
    scenario = _scenario_picker(config, "map_scenario")
    deals = data.load_deals(engine, config, scenario)
    st.title("Map")
    with st.sidebar:
        only_box = st.toggle("Only deals inside the buy box", value=False, key="map_box")
        include_unverified = st.toggle("Include unconfirmed unit counts", value=False)
    rows = [
        row
        for row in deals.rows
        if (row.in_buy_box or not only_box) and (row.type_match == "yes" or include_unverified)
    ]
    # Worst grade first, so the best deals are drawn last and sit on top where points overlap.
    located = sorted(
        (row for row in rows if row.latitude is not None and row.longitude is not None),
        key=lambda row: (row.grade or "Z", -(row.cash_flow_door_month or 0.0)),
        reverse=True,
    )
    if not rows:
        st.info("No underwritten deals to map yet. Run `dealfinder underwrite` first.")
        return
    missing = len(rows) - len(located)
    if missing:
        st.caption(
            f"{missing:,} of {len(rows):,} deals have no coordinates and are not on the map. "
            "Stage 2 locates the ones it enriches with the Census geocoder."
        )
    if not located:
        return

    mode = _mode()
    points = [
        {
            "lat": row.latitude,
            "lon": row.longitude,
            "fill": [int(theme.GRADES[mode].get(row.grade or "F", ("#898781",))[0][i : i + 2], 16)
                     for i in (1, 3, 5)],
            "address": row.full_address,
            "grade": row.grade or "none",
            "cash_flow": money(row.cash_flow_door_month),
            "price": money(row.price),
            "units": row.units,
            "rents": rents_note(row),
        }
        for row in located
    ]  # fmt: skip
    ring = [26, 26, 25] if mode == "dark" else [252, 252, 251]
    layer = pdk.Layer(
        "ScatterplotLayer",
        points,
        get_position="[lon, lat]",
        get_fill_color="fill",
        get_line_color=ring,
        stroked=True,
        line_width_min_pixels=2,
        get_radius=60,
        radius_min_pixels=7,
        radius_max_pixels=14,
        pickable=True,
    )
    view = pdk.data_utils.compute_view([[p["lon"], p["lat"]] for p in points])
    view.zoom = min(view.zoom, 13)
    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=view,
            map_style=None,
            tooltip={
                "html": "<b>{address}</b><br/>Grade {grade} &middot; {cash_flow} a door a month"
                "<br/>{price} &middot; {units} units<br/>Rents: {rents}"
            },
        ),
        height=560,
    )
    _html(theme.grade_legend(mode))
    with st.expander("The same deals as a table"):
        st.dataframe(
            pd.DataFrame(
                {
                    "Grade": [row.grade for row in located],
                    "Cash flow per door": [row.cash_flow_door_month for row in located],
                    "Price": [row.price for row in located],
                    "Units": [row.units for row in located],
                    "Rents": [rents_note(row) for row in located],
                    "Address": [row.full_address for row in located],
                }
            ),
            hide_index=True,
            column_config={
                "Cash flow per door": st.column_config.NumberColumn(format="dollar"),
                "Price": st.column_config.NumberColumn(format="dollar"),
            },
        )


# ---- Deal ----------------------------------------------------------------------------------------


def _line_value(line: dict[str, Any]) -> str:
    amount = line.get("value")
    if amount is None:
        return "n/a"
    if isinstance(amount, str):
        return amount
    unit = line.get("unit")
    if unit == "percent":
        return percent(amount, 2)
    if unit == "money":
        return money(amount)
    if unit == "money_month":
        return f"{money(amount)} / mo"
    if unit == "money_year":
        return f"{money(amount)} / yr"
    return f"{amount:g}"


def _inputs_table(lines: list[dict[str, Any]]) -> str:
    kinds = {"actual": "actual", "estimated": "estimate", "override": "your figure"}
    rows = [
        [
            esc(line.get("label")),
            theme.value(
                _line_value(line),
                estimated=line.get("kind") == "estimated",
                tip=str(line.get("source") or ""),
                flagged=bool(line.get("flagged")),
            ),
            esc(kinds.get(str(line.get("kind")), "")),
            esc(line.get("source")),
        ]
        for line in lines
    ]
    return theme.table(
        ["Input", "Value", "Kind", "Where it came from"], rows, left=(0, 2, 3), wrap=(3,)
    )


def _statement_table(statement: dict[str, float], units: int, estimated: bool) -> str:
    def row(label: str, key: str, sign: int = 1) -> list[str]:
        amount = sign * statement.get(key, 0.0)
        return [
            esc(label),
            theme.value(money(amount), estimated=estimated),
            theme.value(money(amount / 12), estimated=estimated),
            theme.value(money(amount / 12 / max(units, 1)), estimated=estimated),
        ]

    rows = [
        row("Gross scheduled rent", "gsr"),
        row("Vacancy", "vacancy_loss", -1),
        row("Effective gross income", "egi"),
        row("Property taxes", "taxes", -1),
        row("Insurance", "insurance", -1),
        row("Flood insurance", "flood_insurance", -1),
        row("Repairs", "repairs", -1),
        row("Capital reserves", "reserves", -1),
        row("Management", "management", -1),
        row("Owner-paid utilities", "utilities", -1),
        row("HOA dues", "hoa", -1),
        row("Misc", "misc", -1),
        row("Net operating income", "noi"),
    ]
    rows = [
        r
        for r in rows
        if r[1] != theme.value("$0", estimated=estimated)
        or r[0].startswith(("Gross", "Net", "Effective"))
    ]
    return theme.table(
        ["Year one", "Per year", "Per month", "Per door per month"], rows, total_row=len(rows) - 1
    )


def deal_page() -> None:
    config, environment, engine = _context()
    deals = data.load_deals(engine, config)
    st.title("Deal")
    if not deals.rows:
        st.info("No underwritten deals yet. Run `dealfinder underwrite`, then pick one here.")
        return
    ids = [row.listing_id for row in deals.rows]
    current = st.session_state.get("listing_id")
    chosen = st.selectbox(
        "Deal",
        deals.rows,
        index=ids.index(current) if current in ids else 0,
        format_func=_deal_label,
    )
    st.session_state["listing_id"] = chosen.listing_id

    with session_scope(engine) as session:
        detail = deal_detail(session, chosen.listing_id)
        if detail is None:
            st.warning("This listing is no longer stored.")
            return
        screen = detail["screen"]
        place = detail["property"]
        scenarios = {
            name: {
                "results": row.results,
                "score": row.score,
                "grade": row.grade,
                "parts": row.score_parts,
                "in_box": row.in_buy_box,
                "misses": list(row.buy_box_misses or []),
            }
            for name, row in detail["scenarios"].items()
        }
        units = [
            {
                "beds": unit.beds,
                "baths": unit.baths,
                "sqft": unit.sqft,
                "rent": unit.in_place_rent,
                "quote": unit.in_place_rent_quote,
                "vacant": unit.vacant,
                "market": unit.market_rent,
                "market_source": unit.market_rent_source,
                "low": unit.rent_low,
                "high": unit.rent_high,
                "comps": list(unit.comps or []),
                "mix": unit.mix_confidence,
            }
            for unit in detail["units"]
        ]
        inputs, flags = list(screen.inputs or []), list(screen.flags or [])
        details, enrichment = dict(screen.details or {}), dict(screen.enrichment or {})
        stage, note_text = screen.stage, screen.note
        ranking = screen.ranking_scenario or config.settings.buy_box.ranking_scenario
        property_id, starred = place.id, place.starred
        confidence = place.unit_confidence

    presets = config.settings.financing.presets
    names = list(scenarios)
    name = st.segmented_control(
        "Loan scenario",
        names,
        default=ranking if ranking in names else names[0],
        format_func=lambda key: presets[key].label or key if key in presets else key,
    ) or (ranking if ranking in names else names[0])
    current_scenario = scenarios[name]
    results = current_scenario["results"]
    kpis = results["kpis"]
    notes = deals.provenance.get(chosen.listing_id)
    estimated = notes.rents_estimated if notes else True
    mode = _mode()

    head, star, hide = st.columns([6, 1, 1], vertical_alignment="center")
    head.subheader(chosen.full_address)
    if star.button("Unstar" if starred else "Star", icon=":material/star:"):
        data.set_star(engine, property_id, not starred)
        st.rerun()
    if hide.button("Hide", icon=":material/visibility_off:", help="Drop it from every list."):
        data.set_hidden(engine, property_id, True)
        st.session_state.pop("listing_id", None)
        st.rerun()
    unit_word = f"{chosen.units} units" if chosen.units else "units unknown"
    _html(
        f"<p>{theme.grade_pill(current_scenario['grade'], mode)} &nbsp;"
        f"{theme.value(money(chosen.price), estimated=False, tip=f'list price, {chosen.source}')}"
        f" &middot; {esc(chosen.kind.replace('_', ' '))}, {esc(unit_word)} "
        f"(unit count confidence: {esc(confidence or 'none')}) &middot; stage {stage} &middot; "
        f"{'inside the buy box' if current_scenario['in_box'] else 'outside the buy box'}</p>"
    )
    if not current_scenario["in_box"] and current_scenario["misses"]:
        st.caption("Outside the buy box: " + "; ".join(current_scenario["misses"]) + ".")

    tiles = st.columns(6)
    marker = ", est." if estimated else ""
    basis = "Rests on estimated rents." if estimated else "Rents are stated in the listing."
    tiles[0].metric(
        "Cash flow / door" + marker,
        money(kpis["cash_flow_door_month"]),
        help=f"Per month, year one, every unit rented. {basis}",
    )
    tiles[1].metric("Cash-on-cash" + marker, percent(kpis["coc"]), help=basis)
    tiles[2].metric("DSCR" + marker, ratio(kpis["dscr"]), help=basis)
    tiles[3].metric("Cap rate" + marker, percent(kpis["cap_rate"]), help=basis)
    tiles[4].metric(
        "Max offer" + marker,
        money(results["mao"]["price"]),
        help=f"The highest price that still meets every buy-box target. {basis}",
    )
    score_now = current_scenario["score"]
    tiles[5].metric("Score", "n/a" if score_now is None else f"{score_now:.0f} / 100")
    if estimated:
        st.caption(
            "(est.) These rest on estimated rents: "
            + (notes.text() if notes else "no rent is stated in the listing.")
        )

    overview, tab_inputs, tab_units, loans, hold, tab_score, listing, yours = st.tabs(
        ["Overview", "Inputs", "Units and rents", "Loans compared", "Hold and offer", "Score",
         "Listing", "Your figures"]
    )  # fmt: skip

    with overview:
        left, right = st.columns([3, 2])
        with left:
            _html(
                theme.note(theme.LEGEND),
                _statement_table(details.get("statement", {}), chosen.units or 1, estimated),
            )
            loan = results["loan"]
            st.caption(
                f"Loan: {money(loan['amount'])} at {percent(loan['annual_rate'], 2)}, "
                f"{money(loan['down_payment'])} down. Debt service "
                f"{money(kpis['annual_debt_service'])} a year. Cash invested "
                f"{money(kpis['cash_invested'])}."
            )
        with right:
            if note_text:
                st.caption(f"Stage 1: {note_text}")
            if flags:
                order = {"red": 0, "warning": 1, "info": 2}
                items = "".join(
                    f'<p class="flag">{theme.status_dot(str(flag.get("level")))}'
                    f"{esc(flag.get('message'))}</p>"
                    for flag in sorted(flags, key=lambda f: order.get(str(f.get("level")), 3))
                )
                _html(items)
            for text in results.get("notes") or []:
                st.caption(text)
        hack, test = results.get("house_hack"), results.get("self_sufficiency")
        if hack:
            st.markdown("**Living in one unit**")
            cols = st.columns(3)
            cols[0].metric("Your net monthly cost", money(hack["owner_net_monthly_cost"]))
            cols[1].metric("Renting instead would cost", money(hack["comparison_rent"]))
            cols[2].metric("Saved against renting", money(hack["savings_vs_renting"]))
            st.caption(
                f"You live in unit {hack['owner_unit'] + 1}. The figures above this box are for "
                "after you move out: every unit rented and a manager paid."
            )
        if test:
            verdict = "passes" if test["passes"] else "fails"
            payment = money(test["payment"], cents=True)
            st.caption(
                f"FHA self-sufficiency {verdict}: the payment is {payment} "
                f"against an allowed {money(test['allowed_payment'], cents=True)}"
                + (
                    f"; the highest price that passes is {money(test['max_passing_price'])}."
                    if test.get("max_passing_price") and not test["passes"]
                    else "."
                )
            )

    with tab_inputs:
        _html(
            theme.note(theme.LEGEND + " <b>!</b> marks a pessimistic stand-in."),
            _inputs_table(inputs),
        )
        if enrichment:
            st.markdown("**What stage 2 did**")
            for step, text in enrichment.items():
                st.caption(f"{step}: {text}")
        for status in details.get("market_sources") or []:
            st.caption(f"{status['label']}: {status['state']} ({status['detail']})")

    with tab_units:
        rows = []
        for number, unit in enumerate(units, start=1):
            mix_note = (
                "from the listing"
                if unit["mix"] == "high"
                else "even split of the building's totals"
            )
            rent = (
                theme.value("vacant", estimated=False, tip="the listing says this unit is vacant")
                if unit["vacant"]
                else theme.value(
                    money(unit["rent"]),
                    estimated=False,
                    tip=f'stated in the listing: "{unit["quote"]}"',
                )
                if unit["rent"] is not None
                else theme.value("not stated", estimated=True, tip="no rent in the listing")
            )
            market = (
                theme.value(
                    money(unit["market"]), estimated=True, tip=str(unit["market_source"] or "")
                )
                if unit["market"] is not None
                else theme.value(
                    "see Inputs", estimated=True, tip="HUD rents by bedroom, if connected"
                )
            )
            spread = (
                f"{money(unit['low'])} to {money(unit['high'])}"
                if unit["low"] is not None and unit["high"] is not None
                else ""
            )
            rows.append(
                [
                    f"Unit {number}",
                    theme.value("?" if unit["beds"] is None else f"{unit['beds']:g}",
                                estimated=unit["mix"] != "high", tip=mix_note),
                    theme.value("?" if unit["baths"] is None else f"{unit['baths']:g}",
                                estimated=unit["mix"] != "high", tip=mix_note),
                    theme.value("" if unit["sqft"] is None else f"{unit['sqft']:,.0f}",
                                estimated=True, tip="even split of the building's area"),
                    rent,
                    market,
                    theme.value(spread, estimated=True, tip="RentCast's range") if spread else "",
                ]
            )  # fmt: skip
        _html(
            theme.note(theme.LEGEND),
            theme.table(
                ["Unit", "Beds", "Baths", "Sq ft", "Rent in place", "Market rent", "Range"], rows
            ),
        )
        comps = [comp for unit in units[:1] for comp in unit["comps"]]
        if comps:
            with st.expander(f"Comparable rentals behind the estimate ({len(comps)})"):
                st.dataframe(pd.DataFrame(comps), hide_index=True)

    with loans:
        rows = []
        rates = details.get("rates") or {}
        for key, item in scenarios.items():
            result = item["results"]
            k, loan = result["kpis"], result["loan"]
            rows.append(
                [
                    esc(presets[key].label or key if key in presets else key),
                    theme.grade_pill(item["grade"], mode),
                    theme.value(money(loan["down_payment"]), estimated=False),
                    theme.value(percent(loan["annual_rate"], 2), estimated=True,
                                tip=str(rates.get(key, {}).get("source", ""))),
                    theme.value(money(k["cash_invested"]), estimated=True),
                    theme.value(money(k["cash_flow_door_month"]), estimated=estimated),
                    theme.value(percent(k["coc"]), estimated=estimated),
                    theme.value(ratio(k["dscr"]), estimated=estimated),
                    theme.value(percent(result["hold"]["irr"]), estimated=True,
                                tip="rests on the growth and sale assumptions in settings"),
                    theme.value(money(result["mao"]["price"]), estimated=estimated),
                    "yes" if item["in_box"] else "no",
                ]
            )  # fmt: skip
        _html(
            theme.note(theme.LEGEND),
            theme.table(
                ["Loan", "Grade", "Down payment", "Rate", "Cash in", "Cash flow / door", "CoC",
                 "DSCR", "IRR", "Most to offer", "Box"],
                rows,
                left=(0, 1),
            ),
        )  # fmt: skip
        skipped = details.get("skipped") or {}
        for key, why in skipped.items():
            st.caption(f"{key}: {why}")
        if stage == 1:
            st.caption("Only the ranking loan is run in stage 1. Stage 2 survivors get every loan.")

    with hold:
        projection, offer = results["hold"], results["mao"]
        cols = st.columns(4)
        cols[0].metric("IRR", percent(projection["irr"]))
        cols[1].metric("Equity multiple", ratio(projection["equity_multiple"]))
        cols[2].metric("Sale price", money(projection["sale_price"]))
        cols[3].metric("Net sale proceeds", money(projection["net_sale_proceeds"]))
        rule = config.settings.hold
        st.caption(
            f"All estimates: a {rule.years}-year hold with rents growing {rule.rent_growth:.0%}, "
            f"expenses {rule.expense_growth:.0%} and value {rule.appreciation:.0%} a year, and "
            f"{rule.selling_cost_pct:.0%} to sell. Change them in settings.yaml under hold."
        )
        rows = [
            [
                f"Year {year['year']}",
                *(theme.value(money(year[key]), estimated=True) for key in
                  ("gsr", "egi", "opex", "noi", "debt_service", "cash_flow")),
            ]
            for year in projection["years"]
        ]  # fmt: skip
        _html(
            theme.table(
                [
                    "",
                    "Gross rent",
                    "Effective income",
                    "Expenses",
                    "NOI",
                    "Debt service",
                    "Cash flow",
                ],
                rows,
            )
        )
        if offer["price"] is not None:
            binding = ", ".join(offer["binding"]) or "none"
            st.markdown(
                f"**Most you can offer: {money(offer['price'])}.** Above it, this target fails "
                f"first: {binding}."
            )
        else:
            st.markdown(f"**No price meets the targets.** {offer.get('reason') or ''}")
        st.markdown("**What if**")
        rows = [
            [
                esc(row["name"]),
                theme.value(money(row["cash_flow_door_month"]), estimated=True),
                theme.value(money(row["change_door_month"]), estimated=True),
                theme.value(percent(row["coc"]), estimated=True),
                theme.value(ratio(row["dscr"]), estimated=True),
            ]
            for row in results["sensitivity"]
        ]
        _html(theme.table(["Change", "Cash flow / door", "Against today", "CoC", "DSCR"], rows))

    with tab_score:
        parts = current_scenario["parts"] or {}
        if not parts.get("parts"):
            st.info("No score yet. Run `dealfinder underwrite`.")
        else:
            items = [
                (part["label"], part["points"], part["out_of"], part["detail"])
                for part in parts["parts"]
            ]
            _html(theme.meter_list(items))
            for penalty in parts.get("penalties") or []:
                st.caption(
                    f"{penalty['label']}: {penalty['points']:+.0f} points. {penalty['detail']}."
                )
            st.caption(
                f"Parts and penalties add to {parts['before_multiplier']:.1f}. Times "
                f"{parts['multiplier']:g} for a unit count held at {parts['confidence']} "
                f"confidence = {score_now:.0f}, grade {current_scenario['grade']}."
            )

    with listing:
        extras = data.listing_extras(engine, chosen.listing_id)
        facts = {
            "Source": extras.get("source"),
            "MLS": extras.get("mls"),
            "Listing type": extras.get("listing_type"),
            "Agent": extras.get("agent"),
            "Days on market": chosen.days_on_market,
            "Year built": chosen.year_built,
            "Tags": ", ".join(tag.replace("_", " ") for tag in extras.get("tags", [])) or None,
        }
        st.caption(" · ".join(f"{k}: {v}" for k, v in facts.items() if v not in (None, "")))
        if extras.get("url"):
            st.link_button("Open the listing", extras["url"])
        if extras.get("description"):
            st.markdown("**Description**")
            st.text(extras["description"])
        if extras.get("signals"):
            st.markdown("**Why the unit count is what it is**")
            st.dataframe(
                pd.DataFrame(
                    {
                        "Evidence": [s["name"].replace("_", " ") for s in extras["signals"]],
                        "Says": [s.get("unit_count") for s in extras["signals"]],
                        "Strength": [s["strength"] for s in extras["signals"]],
                        "From": [s["evidence"] for s in extras["signals"]],
                    }
                ),
                hide_index=True,
            )
        if extras.get("history"):
            st.markdown("**Price history**")
            st.dataframe(
                pd.DataFrame(extras["history"]),
                hide_index=True,
                column_config={
                    "Price": st.column_config.NumberColumn(format="dollar"),
                    "Was": st.column_config.NumberColumn(format="dollar"),
                },
            )

    with yours:
        _overrides_form(config, environment, engine, property_id, chosen, len(units))


def _overrides_form(
    config: Config,
    environment: Environment,
    engine: Any,
    property_id: int,
    deal: DealRow,
    units: int,
) -> None:
    saved = data.overrides_for(engine, property_id)
    st.caption(
        "Your own figures replace the listing's and the estimates, for this property, on every "
        "run. Leave a box empty to keep the app's figure."
    )
    if saved:
        st.caption("In use now: " + ", ".join(FIELDS[name] for name in saved) + ".")

    def amounts(name: str) -> str:
        return ", ".join("vacant" if a is None else f"{a:g}" for a in saved.get(name) or [])

    with st.form("overrides"):
        left, right = st.columns(2)
        price = left.number_input(
            "Offer price", value=saved.get("price"), step=5_000.0, placeholder=money(deal.price)
        )
        rents = left.text_input(
            f"Current rent per unit ({units} amounts, commas between; 'vacant' for an empty unit)",
            value=amounts("rents"),
        )
        market = left.text_input(
            f"Market rent per unit ({units} amounts)", value=amounts("market_rents")
        )
        tax_bill = left.number_input(
            "Last annual tax bill", value=saved.get("tax_bill"), step=100.0
        )
        insurance = left.number_input(
            "Insurance per unit per year", value=saved.get("insurance_per_unit_year"), step=50.0
        )
        rehab = right.number_input("Rehab budget", value=saved.get("rehab"), step=1_000.0)
        vacancy = right.number_input(
            "Vacancy, percent",
            value=None if saved.get("vacancy_rate") is None else saved["vacancy_rate"] * 100,
            min_value=0.0, max_value=99.0, step=1.0,
        )  # fmt: skip
        rate = right.number_input(
            "Interest rate quote, percent",
            value=None if saved.get("rate_quote") is None else saved["rate_quote"] * 100,
            min_value=0.0, max_value=30.0, step=0.125,
        )  # fmt: skip
        choices = {"The app's setting": None, "Self-managed": "self", "Property manager": "manager"}
        management = right.selectbox(
            "Management",
            list(choices),
            index=list(choices.values()).index(saved.get("management")),
        )
        who = {"Unknown": None, "Tenants": True, "Owner": False}
        utilities = right.selectbox(
            "Who pays utilities",
            list(who),
            index=list(who.values()).index(saved.get("tenants_pay_utilities")),
        )
        submitted = st.form_submit_button("Save and underwrite again", type="primary")
    if submitted:
        try:
            changes = {
                "price": price,
                "rents": data.parse_amounts(rents, units),
                "market_rents": data.parse_amounts(market, units),
                "tax_bill": tax_bill,
                "insurance_per_unit_year": insurance,
                "rehab": rehab,
                "vacancy_rate": None if vacancy is None else vacancy / 100,
                "rate_quote": None if rate is None else rate / 100,
                "management": choices[management],
                "tenants_pay_utilities": who[utilities],
            }
            if changes["market_rents"] and any(a is None for a in changes["market_rents"]):
                raise ValueError("Every unit needs a market rent, or leave the box empty.")
        except ValueError as error:
            st.error(str(error))
        else:
            problem = data.save_overrides(
                engine, config, environment, property_id, deal.listing_id, changes
            )
            if problem:
                st.error(problem)
            else:
                st.rerun()
    if saved and st.button("Remove all my figures for this property"):
        data.remove_overrides(engine, config, environment, property_id, deal.listing_id)
        st.rerun()


# ---- Markets -------------------------------------------------------------------------------------


def _county_chart(counties: list[data.CountySummary], mode: theme.Mode):  # type: ignore[no-untyped-def]
    import plotly.graph_objects as go

    ink = theme.INK[mode]
    ordered = sorted(counties, key=lambda county: county.median_cash_flow_door)
    figure = go.Figure(
        go.Bar(
            x=[county.median_cash_flow_door for county in ordered],
            y=[county.name for county in ordered],
            orientation="h",
            marker={"color": ink["series"], "cornerradius": 4},
            text=[money(county.median_cash_flow_door) for county in ordered],
            textposition="outside",
            textfont={"color": ink["ink"], "size": 12},
            cliponaxis=False,
            customdata=[[county.listings, county.in_buy_box] for county in ordered],
            hovertemplate="<b>%{y}</b><br>Median cash flow per door: %{text} a month"
            "<br>%{customdata[0]} deals, %{customdata[1]} inside the buy box<extra></extra>",
        )
    )
    figure.update_layout(
        height=70 + 34 * len(ordered),
        bargap=0.42,  # about 20px bars: thin marks, the rest of each band is air
        margin={"l": 8, "r": 56, "t": 8, "b": 36},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": theme.FONT, "color": ink["secondary"], "size": 12},
        showlegend=False,
        xaxis={
            "title": {
                "text": "Median cash flow per door per month",
                "font": {"color": ink["muted"]},
            },
            "tickprefix": "$",
            "tickformat": ",.0f",
            "gridcolor": ink["line"],
            "gridwidth": 1,
            "zeroline": True,
            "zerolinecolor": ink["axis"],
            "zerolinewidth": 1,
            "tickfont": {"color": ink["muted"]},
        },
        yaxis={"tickfont": {"color": ink["ink"]}, "showgrid": False, "automargin": True},
        hoverlabel={"font": {"family": theme.FONT}},
    )
    return figure


def markets_page() -> None:
    config, environment, engine = _context()
    scenario = _scenario_picker(config, "markets_scenario")
    deals = data.load_deals(engine, config, scenario)
    st.title("Markets")
    counties = data.county_summaries([row for row in deals.rows if row.type_match == "yes"])
    if counties:
        st.subheader("Counties with underwritten deals")
        st.caption(
            "Medians over the stored deals with a confirmed unit count. All figures are "
            "estimates built from each deal's underwriting."
        )
        top = counties[:15]
        st.plotly_chart(_county_chart(top, _mode()), theme=None, config={"displayModeBar": False})
        if len(counties) > len(top):
            st.caption(
                f"The chart shows the {len(top)} counties with the most deals; the table has all."
            )
        st.dataframe(
            pd.DataFrame(
                {
                    "County": [c.name for c in counties],
                    "Deals": [c.listings for c in counties],
                    "Inside the buy box": [c.in_buy_box for c in counties],
                    "Median cash flow per door": [round(c.median_cash_flow_door) for c in counties],
                    "Median cap rate": [c.median_cap_rate * 100 for c in counties],
                    "Median price per door": [round(c.median_price_per_door) for c in counties],
                }
            ),
            hide_index=True,
            column_config={
                "Median cash flow per door": st.column_config.NumberColumn(format="dollar"),
                "Median cap rate": st.column_config.NumberColumn(format="%.1f%%"),
                "Median price per door": st.column_config.NumberColumn(format="dollar"),
            },
        )
    else:
        st.info("No underwritten deals yet, so there are no county figures to show.")

    st.subheader("Look up a ZIP code")
    zip_code = st.text_input("ZIP code", max_chars=5, placeholder="43215")
    if not zip_code:
        return
    fetcher = Fetcher(environment.cache_dir, offline=environment.dealfinder_offline)
    try:
        market = load_market(zip_code, config, environment, fetcher)
    finally:
        fetcher.close()
    if market.place is None or market.place.state is None:
        st.warning(f"'{zip_code}' is not a ZIP code this tool knows.")
        return
    st.markdown(f"**{market.place.describe()}**")
    cols = st.columns(4)
    rate = market.mortgage_rate
    cols[0].metric(
        "30-year mortgage rate", percent(rate.rate, 2) if rate else "n/a",
        help=f"{rate.source}, week of {rate.as_of:%b %d, %Y}" if rate else "not connected",
    )  # fmt: skip
    housing = market.housing
    tax = housing.effective_tax_rate() if housing else None
    cols[1].metric(
        "County tax rate", percent(tax[0], 2) if tax else "n/a",
        help=f"{housing.vintage}: {tax[1]}" if housing and tax else "not connected",
    )  # fmt: skip
    cols[2].metric(
        "Rental vacancy",
        percent(housing.rental_vacancy)
        if housing and housing.rental_vacancy is not None
        else "n/a",
        help=housing.vintage if housing else "not connected",
    )
    index = market.rent_index
    cols[3].metric(
        "Typical rent (Zillow)",
        money(index.level) if index else "n/a",
        delta=None
        if not index or index.yoy_growth is None
        else f"{index.yoy_growth:+.1%} in a year",
        help=index.describe() if index else "not connected",
    )
    if market.risk:
        st.caption(market.risk.describe())
    rents = market.fair_market_rents
    if rents:
        _html(
            theme.note(esc(rents.describe()) + ". Gross rents, utilities included: estimates."),
            theme.table(
                [
                    "Bedrooms",
                    *(("Studio" if beds == 0 else f"{beds}") for beds in sorted(rents.rents)),
                ],
                [
                    [
                        "Fair Market Rent",
                        *(
                            theme.value(
                                money(rents.rents[beds]), estimated=True, tip=rents.describe()
                            )
                            for beds in sorted(rents.rents)
                        ),
                    ]
                ],
            ),
        )
        st.caption(hud.ATTRIBUTION)
    for status in market.sources:
        st.caption(f"{status.label}: {status.state} ({status.detail})")
    name = county_name(market.place.county_fips)
    limits = config.loan_limits
    units = st.segmented_control(
        "Loan limits for", [1, 2, 3, 4], default=4, format_func=lambda n: f"{n} units"
    )
    if units:
        fha = limits.lookup("fha", units, market.place.county_fips)
        conforming = limits.lookup("conforming_county", units, market.place.county_fips)
        st.caption(
            f"{config.settings.financing.loan_limit_year} loan limits for {name or 'this county'}: "
            f"FHA {money(fha.amount)}, conforming {money(conforming.amount)}"
            + ("" if fha.county_specific else " (national minimum; county not in the table)")
            + "."
        )


# ---- Settings ------------------------------------------------------------------------------------

SOURCE_NOTES = {
    "rentcast": "RentCast, a licensed listings API. Needs RENTCAST_API_KEY.",
    "csv": "Your own lists: an agent's MLS export, a broker's sheet.",
    "homeharvest": "SCRAPER, off by default. Reads public listing sites; check their terms first.",
    "apify_zillow": "SCRAPER, off by default. A paid Apify actor; needs APIFY_TOKEN.",
}


def settings_page() -> None:
    config, environment, engine = _context()
    settings = config.settings
    st.title("Settings")
    st.caption(
        f"Settings come from {config.config_dir / 'settings.yaml'}. Changes made here are saved "
        f"to {data.local_settings_path(config).name} beside it, which overrides it and stays out "
        "of git."
    )

    st.subheader("Buy box")
    box = settings.buy_box
    presets = settings.financing.presets
    with st.form("buy_box"):
        left, right = st.columns(2)
        types = left.multiselect(
            "Property types", PROPERTY_TYPES, default=box.property_types,
            format_func=lambda kind: kind.replace("_", " "),
        )  # fmt: skip
        everywhere = box.states == "all"
        states = left.multiselect(
            "States (none selected means all 48 and DC)",
            sorted(CONTIGUOUS_STATES),
            default=[] if everywhere else list(box.states),
        )
        max_price = left.number_input(
            "Highest price", value=box.max_price, step=25_000.0, placeholder="no cap"
        )
        confidence = left.selectbox(
            "Unit count must be at least this certain",
            ["high", "medium", "low"],
            index=["high", "medium", "low"].index(box.min_unit_confidence),
        )
        ranking = right.selectbox(
            "Rank deals under this loan",
            list(presets),
            index=list(presets).index(box.ranking_scenario),
            format_func=lambda key: presets[key].label or key,
        )
        cash_flow = right.number_input(
            "Cash flow per door per month, at least",
            value=box.targets.min_cash_flow_per_door_month, step=25.0, placeholder="off",
        )  # fmt: skip
        coc = right.number_input(
            "Cash-on-cash return, percent, at least",
            value=None if box.targets.min_coc is None else box.targets.min_coc * 100,
            step=0.5, placeholder="off",
        )  # fmt: skip
        dscr = right.number_input(
            "DSCR at least", value=box.targets.min_dscr, step=0.05, placeholder="off"
        )
        management = right.selectbox(
            "Management",
            ["self", "manager"],
            index=["self", "manager"].index(settings.underwriting.management.mode),
            format_func=lambda mode: "Self-managed" if mode == "self" else "Property manager",
        )
        saved = st.form_submit_button("Save", type="primary")
    if saved:
        if not types:
            st.error("Pick at least one property type.")
        else:
            changes = {
                "buy_box": {
                    "property_types": types,
                    "states": states or "all",
                    "max_price": max_price,
                    "min_unit_confidence": confidence,
                    "ranking_scenario": ranking,
                    "targets": {
                        "min_cash_flow_per_door_month": cash_flow,
                        "min_coc": None if coc is None else coc / 100,
                        "min_dscr": dscr,
                    },
                },
                "underwriting": {"management": {"mode": management}},
            }
            try:
                path = data.save_local_settings(config, changes)
            except Exception as error:
                st.error(f"Not saved: {error}")
            else:
                st.success(f"Saved to {path}. Underwrite again to apply it to the stored deals.")

    if st.button(
        "Underwrite again with free data", help="No RentCast requests and no Claude calls."
    ):
        with st.spinner("Underwriting the stored listings..."):
            report = data.rerun_free(engine, load_config(config.config_dir), environment)
        st.text(report.text())

    st.subheader("Where listings come from")
    rows = [
        [
            esc(name),
            "on" if switch.enabled else "off",
            esc(SOURCE_NOTES.get(name, "")),
        ]
        for name, switch in settings.sources.items()
    ]
    _html(theme.table(["Source", "State", "What it is"], rows, left=(0, 1, 2), wrap=(2,)))

    st.subheader("Keys and budget")
    keys = environment.keys_set()
    _html(
        theme.table(
            ["Key", "State"],
            [[esc(name), "set" if present else "not set"] for name, present in keys.items()],
            left=(0, 1),
        )
    )
    st.caption("Keys live in the .env file. Their values are never shown here.")
    budget = data.budget_status(engine, config)
    cap = "no cap" if budget["cap"] is None else f"{budget['cap']:,}"
    cols = st.columns(3)
    cols[0].metric("RentCast requests this period", f"{budget['used']:,} of {cap}")
    cols[1].metric("Plan", str(budget["plan"]))
    cols[2].metric(
        "Cost so far", "n/a" if budget["cost"] is None else money(budget["cost"], cents=True)
    )

    hidden = data.hidden_properties(engine)
    if hidden:
        st.subheader("Hidden properties")
        for property_id, address in hidden:
            if st.button(f"Show again: {address}", key=f"unhide_{property_id}"):
                data.set_hidden(engine, property_id, False)
                st.rerun()

    with st.expander("Every setting in effect"):
        st.code(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False), language="yaml")
    st.caption(
        f"Data folder: {environment.dealfinder_data_dir.resolve()}. "
        f"Database: {environment.dealfinder_db_url}."
    )
