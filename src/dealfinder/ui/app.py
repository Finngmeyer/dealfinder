"""The dashboard. Start it with ``dealfinder ui``.

Five pages: Deals (the ranked list), Map, Deal (one listing in full), Markets and Settings.
Every page ends with the same footer.
"""

from __future__ import annotations

import streamlit as st

from dealfinder.analysis import DISCLAIMER
from dealfinder.ui import views


def main() -> None:
    st.set_page_config(page_title="Dealfinder", page_icon=":material/home_work:", layout="wide")
    pages = {
        "Deals": st.Page(
            views.deals_page, title="Deals", icon=":material/leaderboard:", default=True
        ),
        "Map": st.Page(views.map_page, title="Map", icon=":material/map:", url_path="map"),
        "Deal": st.Page(
            views.deal_page, title="Deal", icon=":material/apartment:", url_path="deal"
        ),
        "Markets": st.Page(
            views.markets_page,
            title="Markets",
            icon=":material/query_stats:",
            url_path="markets",
        ),
        "Settings": st.Page(
            views.settings_page, title="Settings", icon=":material/tune:", url_path="settings"
        ),
    }
    st.session_state["_pages"] = pages  # so a page can send the reader to another one
    chosen = st.navigation(list(pages.values()))
    try:
        chosen.run()
    finally:
        st.divider()
        st.caption(DISCLAIMER)


main()
