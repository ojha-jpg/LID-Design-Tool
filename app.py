"""
LID Design Tools — combined Streamlit application shell.

This module intentionally stays thin: it configures the Streamlit app,
registers page entrypoints from the tool modules, and exposes a home page that
routes users into the four workflows. Domain logic lives in the page modules.
"""

import streamlit as st

from app_brc  import main as brc_main
from app_pp   import main as pp_main
from app_rwh  import main as rwh_main
from app_peak import main as peak_main


# ============================================================================
# HOMEPAGE
# ============================================================================

def homepage() -> None:
    """Render the landing page and route users into the registered tools."""
    st.title("LID Design Tools")
    st.markdown(
        "**City of Tulsa Low Impact Development (LID) Manual (2026)**  \n"
        "Select a design tool from the sidebar or the cards below."
    )
    st.divider()

    col1, col2, col3, col4 = st.columns(4, gap="large")

    with col1:
        st.markdown("### Bioretention Cell (BRC)")
        st.markdown(
            "Design bioretention cells following the 10-step process "
            "in **Chapter 101**. Covers site selection, SWV, ponding depth, "
            "media, underdrain, orifice, and overflow sizing."
        )
        st.markdown("*Reference: Chapter 101 — Bioretention and Biofiltration*")
        if st.button("Open BRC Design Tool", use_container_width=True, type="primary"):
            st.switch_page(brc_page)

    with col2:
        st.markdown("### Permeable Pavement (PP)")
        st.markdown(
            "Design permeable pavement systems per **Chapter 103**. "
            "Covers surface type selection, storage depth, subbase, "
            "underdrain, and orifice outlet sizing."
        )
        st.markdown("*Reference: Chapter 103 — Permeable Pavements*")
        if st.button("Open PP Design Tool", use_container_width=True, type="primary"):
            st.switch_page(pp_page)

    with col3:
        st.markdown("### Rainwater Harvesting (RWH)")
        st.markdown(
            "Design rainwater harvesting systems per **Section 104**. "
            "Covers catchment area, stormwater volume, first flush, "
            "tank selection, orifice, and first flush diverter pipe."
        )
        st.markdown("*Reference: Section 104 — Rainwater Harvesting*")
        if st.button("Open RWH Design Tool", use_container_width=True, type="primary"):
            st.switch_page(rwh_page)

    with col4:
        st.markdown("### Peak Runoff Analysis")
        st.markdown(
            "5-step watershed delineation, NOAA Atlas 14 precipitation, "
            "SSURGO soils, NLCD land use, CN + Rational method peak flows. "
            "Runs entirely from public APIs."
        )
        st.markdown("*Oklahoma · USGS StreamStats + USGS 3DEP DEM*")
        if st.button("Open Peak Runoff Tool", use_container_width=True, type="primary"):
            st.switch_page(peak_page)

    st.divider()
    st.caption(
        "City of Tulsa Engineering Manual (2026) · "
        "University of Oklahoma LID Project"
    )


# ============================================================================
# NAVIGATION
# ============================================================================

st.set_page_config(
    page_title="LID Design Tools",
    page_icon=":droplet:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Keep page registration centralized here so navigation and documentation stay
# aligned even as the individual tool modules evolve independently.
home_page = st.Page(homepage,  title="Home",                        icon="🏠", url_path="home",  default=True)
brc_page  = st.Page(brc_main,  title="Bioretention Cell (BRC)",     icon="🌱", url_path="brc")
pp_page   = st.Page(pp_main,   title="Permeable Pavement (PP)",     icon="🧱", url_path="pp")
rwh_page  = st.Page(rwh_main,  title="Rainwater Harvesting (RWH)",  icon="💧", url_path="rwh")
peak_page = st.Page(peak_main, title="Peak Runoff Analysis",        icon="📈", url_path="peak")

pg = st.navigation(
    {
        "Home": [home_page],
        "LID Design Tools": [brc_page, pp_page, rwh_page],
        "Hydrology": [peak_page],
    }
)
pg.run()
