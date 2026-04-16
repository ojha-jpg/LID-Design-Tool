"""
Bio-Retention Cell (BRC) Design Tool
Based on City of Tulsa LID Manual (2026) - Chapter 101

This tool implements the design workflow for bioretention cells:
- Site selection and siting offsets
- Contributing area and SWV (stormwater volume)
- Infiltration rate
- Ponding depth
- Media depth (underdrain only)
- Total drawdown verification
- Cell area and storage capacity
- Underdrain sizing
- Slow-release orifice outlet
- Overflow outlet sizing


Reference: City of Tulsa Engineering Manual (2021), Chapter 101: Bioretention and Biofiltration
"""

import io
import math
from datetime import date

import streamlit as st
import folium
from folium.plugins import MousePosition
from streamlit_folium import st_folium
from api_clients import fetch_site_soil_texture, geocode_address, infer_design_soil_type
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ============================================================================
# CONSTANTS AND DEFAULTS (from Tulsa LID Manual, Chapter 101)
# ============================================================================

# Default design precipitation and criteria
DESIGN_PRECIPITATION = 1.20  # inches (Eq. 101.9)
TARGET_LOADING_RATIO = 0.03  # 3% minimum as initial sizing parameter

# Media and soil properties
PHI_BRC = 0.30  # Engineered media porosity (typical sandy loam mix)
PHI_ROCK = 0.40  # Rock porosity surrounding underdrain
I_BRC_ENGINEERED = 6.0  # Engineered media infiltration rate (in/hr)

# Design time criteria
TS_SURFACE = 24.0  # Maximum surface storage drainage time (hrs) - Eq. 101.3, 101.7
TINF = 4.0  # Infiltration storage time for media (hrs) - Eq. 101.4, 101.8
TDD_TOTAL = 48.0  # Maximum total drawdown time (hrs) - Eq. 101.8

# Underdrain specifications
D_RAB = 0.5  # Combined rock depth above and below underdrain (ft)
OU_PIPE_IN = 2.0  # Underdrain pipe diameter (inches) - minimum 3 in, typical 2 in
OU_PIPE_FT = OU_PIPE_IN / 12.0  # Convert to feet
LU_UNDERDRAIN = 20.0  # Typical underdrain length (ft)

# Orifice outlet constants (Eq. 101.12, 101.13)
CD_ORIFICE = 0.61  # Discharge coefficient for sharp-edge orifice
G_GRAVITY = 32.2  # Gravitational constant (ft/s²)
TD_DESIGN = 48.0 * 3600.0  # Design detention time = 48 hrs in seconds

# Soil infiltration rates (Table 101.2)

SOIL_INFILTRATION_RATES = {
    "Sand": 4.26,
    "Loamy Sand": 3.81,
    "Sandy Loam": 1.98,
    "Loam": 0.61,
    "Silt Loam": 0.63,
    "Silt": 0.87,
    "Sandy Clay Loam": 0.28,
    "Clay Loam": 0.28,
    "Clay": 0.03,
}


DEFAULT_SOIL_TYPE = "Sandy Loam"
OKLAHOMA_CENTER = [35.5, -97.5]
DEFAULT_MAP_ZOOM = 7
_fragment = getattr(st, "fragment", lambda f: f)


def _init_state() -> None:
    """Initialise BRC widget state used for SSURGO-based soil lookup."""
    defaults = {
        "brc_soil_type": DEFAULT_SOIL_TYPE,
        "brc_prev_soil_type": DEFAULT_SOIL_TYPE,
        "brc_native_infiltration": float(SOIL_INFILTRATION_RATES[DEFAULT_SOIL_TYPE]),
        "brc_soil_lookup_lat": "",
        "brc_soil_lookup_lon": "",
        "brc_soil_lookup_address": "",
        "brc_soil_lookup_result": None,
        "brc_soil_lookup_error": None,
        "brc_selected_lat": None,
        "brc_selected_lon": None,
        "brc_map_center": OKLAHOMA_CENTER,
        "brc_map_zoom": DEFAULT_MAP_ZOOM,
        "brc_geocode_match": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _soil_mix_summary(textures: dict[str, float]) -> str:
    """Compact USDA texture summary for sidebar display."""
    return ", ".join(f"{name} ({pct:.1f}%)" for name, pct in textures.items())


def _set_lookup_point(lat: float, lon: float, zoom: int = 13) -> None:
    """Sync a selected map/address point into the soil-lookup fields."""
    st.session_state["brc_selected_lat"] = float(lat)
    st.session_state["brc_selected_lon"] = float(lon)
    st.session_state["brc_map_center"] = [float(lat), float(lon)]
    st.session_state["brc_map_zoom"] = int(zoom)
    st.session_state["brc_soil_lookup_lat"] = f"{float(lat):.6f}"
    st.session_state["brc_soil_lookup_lon"] = f"{float(lon):.6f}"
    st.session_state["brc_soil_lookup_result"] = None
    st.session_state["brc_soil_lookup_error"] = None


def _perform_soil_lookup() -> None:
    """Fetch USDA soil texture for the currently selected/manual BRC location."""
    try:
        lat = float(st.session_state["brc_soil_lookup_lat"])
        lon = float(st.session_state["brc_soil_lookup_lon"])
    except ValueError:
        st.session_state["brc_soil_lookup_error"] = "Enter valid decimal coordinates before running the soil lookup."
        st.session_state["brc_soil_lookup_result"] = None
        return

    soil_lookup, soil_lookup_live = fetch_site_soil_texture(lat, lon)
    if soil_lookup_live and soil_lookup:
        dominant_texture = soil_lookup.get("dominant_texture")
        inferred_soil = infer_design_soil_type(dominant_texture)
        soil_lookup["soil_type"] = inferred_soil
        st.session_state["brc_soil_lookup_result"] = soil_lookup
        st.session_state["brc_soil_lookup_error"] = None
        if inferred_soil in SOIL_INFILTRATION_RATES:
            st.session_state["brc_soil_type"] = inferred_soil
            st.session_state["brc_prev_soil_type"] = inferred_soil
            st.session_state["brc_native_infiltration"] = float(SOIL_INFILTRATION_RATES[inferred_soil])
    else:
        st.session_state["brc_soil_lookup_error"] = (
            "USDA soil texture lookup failed for that location. Adjust the location or enter soil data manually."
        )
        st.session_state["brc_soil_lookup_result"] = None


@_fragment
def _render_site_selector_map() -> None:
    """Fragment-isolated BRC point-selection map with immediate marker updates."""
    if st.session_state["brc_selected_lat"] is not None:
        _map_center = [st.session_state["brc_selected_lat"], st.session_state["brc_selected_lon"]]
    else:
        _map_center = st.session_state.get("brc_map_center") or OKLAHOMA_CENTER
    _map_zoom = st.session_state.get("brc_map_zoom") or DEFAULT_MAP_ZOOM

    m = folium.Map(location=_map_center, zoom_start=_map_zoom, tiles="OpenStreetMap")
    MousePosition().add_to(m)

    if st.session_state["brc_selected_lat"] is not None:
        _lat = st.session_state["brc_selected_lat"]
        _lon = st.session_state["brc_selected_lon"]
        folium.Marker(
            location=[_lat, _lon],
            tooltip="Selected BRC Site",
            popup=folium.Popup(
                f"<b>Selected point</b><br>{_lat:.5f}&deg;N, {_lon:.5f}&deg;",
                max_width=200,
            ),
            icon=folium.Icon(color="red", icon="info-sign"),
        ).add_to(m)

    map_data = st_folium(m, use_container_width=True, height=360, key="brc_site_selector_map")

    if map_data and map_data.get("last_clicked"):
        if map_data.get("center"):
            c = map_data["center"]
            st.session_state["brc_map_center"] = [c["lat"], c["lng"]]
        if map_data.get("zoom"):
            st.session_state["brc_map_zoom"] = map_data["zoom"]
        st.session_state["brc_selected_lat"] = map_data["last_clicked"]["lat"]
        st.session_state["brc_selected_lon"] = map_data["last_clicked"]["lng"]
        st.session_state["brc_soil_lookup_lat"] = f"{st.session_state['brc_selected_lat']:.6f}"
        st.session_state["brc_soil_lookup_lon"] = f"{st.session_state['brc_selected_lon']:.6f}"
        st.session_state["brc_geocode_match"] = None
        st.rerun()


def _render_site_selector() -> None:
    """Address search + clickable map for selecting the BRC soil lookup point."""
    with st.expander("Site Selector", expanded=False):
        st.caption("Search by address or click the map to choose the BRC location used for USDA soil lookup.")

        with st.form("brc_address_form", clear_on_submit=False):
            st.text_input(
                "Project Address",
                key="brc_soil_lookup_address",
                placeholder="e.g. 175 E 2nd St, Tulsa, OK",
            )
            geocode_submit = st.form_submit_button("Find Address", use_container_width=True)

        if geocode_submit:
            match, is_live = geocode_address(st.session_state["brc_soil_lookup_address"])
            if is_live and match:
                _set_lookup_point(match["lat"], match["lon"])
                st.session_state["brc_geocode_match"] = match["matched_address"]
            else:
                st.session_state["brc_geocode_match"] = None
                st.warning("Address lookup failed. Try a more specific US street address or click the map instead.")

        _render_site_selector_map()

        selected_lat = st.session_state.get("brc_selected_lat")
        selected_lon = st.session_state.get("brc_selected_lon")
        matched_address = st.session_state.get("brc_geocode_match")
        if matched_address:
            st.caption(f"Matched address: **{matched_address}**")
        if selected_lat is not None and selected_lon is not None:
            st.success(f"Selected Point: Latitude = {selected_lat:.6f}, Longitude = {selected_lon:.6f}")
            col_a, col_b = st.columns([5, 1])
            with col_a:
                if st.button("Fetch Soil For Selected Point", key="brc_fetch_soil_from_map", type="primary", use_container_width=True):
                    _perform_soil_lookup()
            with col_b:
                if st.button("Clear", key="brc_clear_site_point", use_container_width=True):
                    st.session_state["brc_selected_lat"] = None
                    st.session_state["brc_selected_lon"] = None
                    st.session_state["brc_soil_lookup_lat"] = ""
                    st.session_state["brc_soil_lookup_lon"] = ""
                    st.session_state["brc_geocode_match"] = None
                    st.session_state["brc_soil_lookup_result"] = None
                    st.session_state["brc_soil_lookup_error"] = None
                    try:
                        st.rerun(scope="app")
                    except TypeError:
                        st.rerun()

        soil_lookup_result = st.session_state.get("brc_soil_lookup_result")
        soil_lookup_error = st.session_state.get("brc_soil_lookup_error")
        if soil_lookup_result:
            dominant_texture = soil_lookup_result.get("dominant_texture") or "Unknown"
            inferred_soil = infer_design_soil_type(dominant_texture)
            soil_lookup_result["soil_type"] = inferred_soil
            st.success(f"USDA dominant texture: **{dominant_texture}**")
            if inferred_soil:
                st.caption(f"Mapped to design soil type **{inferred_soil}**.")
            textures = soil_lookup_result.get("textures") or {}
            if textures:
                st.caption(f"Sample mix: {_soil_mix_summary(textures)}")
        elif soil_lookup_error:
            st.warning(soil_lookup_error)

# ============================================================================
# DESIGN CALCULATIONS (following Tulsa LID Manual equations)
# ============================================================================

def calculate_swv(impervious_area_ft2: float, brc_area_ft2: float, precip_in: float = DESIGN_PRECIPITATION) -> float:
    """
    Calculate Stormwater Volume (SWV) required.

    Eq. 101.9: SWV = A_ip × (1 in / 12 in/ft) + A_BRC × (D_Prec / 12 in/ft)

    Args:
        impervious_area_ft2: Total contributing impervious area (ft²)
        brc_area_ft2: Bioretention cell area (ft²)
        precip_in: Design precipitation depth on BRC surface (in)

    Returns:
        swv (ft³): Stormwater volume required
    """
    runoff_from_impervious = (impervious_area_ft2 / 12.0) * 1.0  # 1 in depth
    runoff_on_brc = (brc_area_ft2 / 12.0) * precip_in
    return runoff_from_impervious + runoff_on_brc


def calculate_loading_ratio(brc_area_ft2: float, contributing_area_ft2: float) -> float:
    """
    Calculate Loading Ratio (LR).

    Eq. 101.2: LR = A_BRC / A_C

    For outside placement: A_C = impervious + pervious (BRC not included)
    For inside placement:  A_C = impervious + pervious + A_BRC (BRC included)

    Target: LR ≥ 3% as initial sizing parameter.

    Args:
        brc_area_ft2: Bioretention cell area (ft²)
        contributing_area_ft2: Total contributing area denominator (ft²)

    Returns:
        lr: Loading ratio (expressed as decimal, 0.03 = 3%)
    """
    if contributing_area_ft2 <= 0:
        return 0.0
    return brc_area_ft2 / contributing_area_ft2


def get_max_ponding_depth(infiltration_rate_in_hr: float) -> float:
    """
    Calculate maximum allowable ponding depth.

    Eq. 101.3: D_p (max) ≤ I × 24 hrs ÷ 12 in/ft

    Surface ponding must drain within 24 hours. Typical range: 0.5–1.0 ft.

    Args:
        infiltration_rate_in_hr: Infiltration rate (in/hr)

    Returns:
        max_depth (ft): Maximum allowable ponding depth
    """
    return (infiltration_rate_in_hr * TS_SURFACE) / 12.0


def calculate_surface_ponding_drawdown(ponding_depth_ft: float, infiltration_rate_in_hr: float) -> float:
    """
    Calculate surface ponding drawdown time.

    Eq. 101.7: t_sp = (d_p × 12) / I

    Args:
        ponding_depth_ft: Ponding depth (ft)
        infiltration_rate_in_hr: Infiltration rate (in/hr)

    Returns:
        t_sp (hrs): Surface ponding drawdown time
    """
    if infiltration_rate_in_hr <= 0:
        return float('inf')
    return (ponding_depth_ft * 12.0) / infiltration_rate_in_hr


def get_max_media_depth(infiltration_rate_in_hr: float, porosity: float) -> float:
    """
    Calculate maximum media depth for underdrain designs.

    Eq. 101.4: D_m (max) ≤ (I × 4 hrs) ÷ (φ_BRC × 12 in/ft)

    Only 4 hours of infiltration storage in the media counts toward the SWV.

    Args:
        infiltration_rate_in_hr: Infiltration rate (in/hr)
        porosity: Media porosity (e.g., 0.30)

    Returns:
        max_depth (ft): Maximum allowable media depth
    """
    return (infiltration_rate_in_hr * TINF) / (porosity * 12.0)


def calculate_total_drawdown_time(surface_ponding_time_hr: float) -> float:
    """
    Calculate total drawdown time.

    Eq. 101.8: t_dd = t_sp + 4 hrs (must be ≤ 48 hrs)

    Args:
        surface_ponding_time_hr: Surface ponding drawdown time (hrs)

    Returns:
        t_dd (hrs): Total drawdown time
    """
    return surface_ponding_time_hr + TINF


def calculate_orifice_flow_time_hr(
    brc_area_ft2: float,
    underdrain_diameter_in: float = OU_PIPE_IN,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
    head_ft: float = 0.0,
) -> float:
    """Calculate orifice drain time in hours from the workbook-style equation."""
    underdrain_diameter_ft = underdrain_diameter_in / 12.0
    head_ft = max(head_ft, 0.0)
    time_sec = (
        (8.0 * brc_area_ft2)
        / (math.pi * discharge_coeff * underdrain_diameter_ft ** 2)
        * math.sqrt(head_ft / (2.0 * gravity))
    )
    return time_sec / 3600.0


def calculate_underdrain_surface_drawdown_time(
    brc_area_ft2: float,
    ponding_depth_ft: float,
    infiltration_rate_in_hr: float,
    underdrain_diameter_in: float = OU_PIPE_IN,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> tuple[float, float, float]:
    """Return the controlling surface drawdown time and both candidate times."""
    orifice_time_hr = calculate_orifice_flow_time_hr(
        brc_area_ft2=brc_area_ft2,
        underdrain_diameter_in=underdrain_diameter_in,
        discharge_coeff=discharge_coeff,
        gravity=gravity,
        head_ft=ponding_depth_ft,
    )
    infiltration_time_hr = (
        float("inf")
        if infiltration_rate_in_hr <= 0
        else (ponding_depth_ft * 12.0) / infiltration_rate_in_hr
    )
    return max(orifice_time_hr, infiltration_time_hr), orifice_time_hr, infiltration_time_hr


def calculate_underdrain_additional_drawdown_time(
    brc_area_ft2: float,
    media_depth_ft: float,
    ponding_depth_ft: float,
    infiltration_rate_in_hr: float,
    rock_depth_ft: float = D_RAB,
    underdrain_diameter_in: float = OU_PIPE_IN,
    media_porosity: float = PHI_BRC,
    rock_porosity: float = PHI_ROCK,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> tuple[float, float, float]:
    """Return the controlling media drain time and both candidate times."""
    underdrain_diameter_ft = underdrain_diameter_in / 12.0

    head_ft = (
        ponding_depth_ft
        + (media_depth_ft - rock_depth_ft) * media_porosity
        + (rock_depth_ft - underdrain_diameter_in) * rock_porosity
    ) * media_porosity
    orifice_time_hr = calculate_orifice_flow_time_hr(
        brc_area_ft2=brc_area_ft2,
        underdrain_diameter_in=underdrain_diameter_in,
        discharge_coeff=discharge_coeff,
        gravity=gravity,
        head_ft=head_ft,
    )
    infiltration_time_hr = (
        float("inf")
        if infiltration_rate_in_hr <= 0
        else max((media_depth_ft - rock_depth_ft - underdrain_diameter_ft) * 12.0 / infiltration_rate_in_hr, 0.0)
    )
    return max(orifice_time_hr, infiltration_time_hr), orifice_time_hr, infiltration_time_hr


def calculate_underdrain_total_drawdown_time(
    surface_ponding_time_hr: float,
    brc_area_ft2: float,
    ponding_depth_ft: float,
    media_depth_ft: float,
    infiltration_rate_in_hr: float,
    rock_depth_ft: float = D_RAB,
    underdrain_diameter_in: float = OU_PIPE_IN,
    media_porosity: float = PHI_BRC,
    rock_porosity: float = PHI_ROCK,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> tuple[float, float, float, str]:
    """Return total drawdown time plus the controlling underdrain component."""
    surface_drawdown_time_hr, surface_orifice_time_hr, surface_infiltration_time_hr = calculate_underdrain_surface_drawdown_time(
        brc_area_ft2=brc_area_ft2,
        ponding_depth_ft=ponding_depth_ft,
        infiltration_rate_in_hr=infiltration_rate_in_hr,
        underdrain_diameter_in=underdrain_diameter_in,
        discharge_coeff=discharge_coeff,
        gravity=gravity,
    )
    additional_drawdown_time_hr, media_orifice_time_hr, media_infiltration_time_hr = calculate_underdrain_additional_drawdown_time(
        brc_area_ft2=brc_area_ft2,
        media_depth_ft=media_depth_ft,
        ponding_depth_ft=ponding_depth_ft,
        infiltration_rate_in_hr=infiltration_rate_in_hr,
        rock_depth_ft=rock_depth_ft,
        underdrain_diameter_in=underdrain_diameter_in,
        media_porosity=media_porosity,
        rock_porosity=rock_porosity,
        discharge_coeff=discharge_coeff,
        gravity=gravity,
    )
    if media_orifice_time_hr > media_infiltration_time_hr:
        controlling_mode = "Underdrain orifice"
    else:
        controlling_mode = "Media infiltration"
    return surface_drawdown_time_hr + additional_drawdown_time_hr, media_orifice_time_hr, media_infiltration_time_hr, controlling_mode


def calculate_storage_no_underdrain(
    brc_area_ft2: float,
    ponding_depth_ft: float,
    infiltration_rate_in_hr: float,
) -> float:
    """
    Calculate storage capacity for bioretention without underdrain.

    Eq. 101.5: S = A_BRC × [d_p + (I / 12) × 4 hrs]

    Args:
        brc_area_ft2: Bioretention cell area (ft²)
        ponding_depth_ft: Ponding depth (ft)
        infiltration_rate_in_hr: Infiltration rate (in/hr)

    Returns:
        storage (ft³): Total storage capacity
    """
    infiltration_storage = (infiltration_rate_in_hr / 12.0) * TINF
    total_depth = ponding_depth_ft + infiltration_storage
    return brc_area_ft2 * total_depth


def calculate_storage_with_underdrain(
    brc_area_ft2: float,
    ponding_depth_ft: float,
    media_depth_ft: float,
    rock_depth_ft: float = D_RAB,
    underdrain_diameter_ft: float = OU_PIPE_FT,
    underdrain_length_ft: float = LU_UNDERDRAIN,
    media_porosity: float = PHI_BRC,
    rock_porosity: float = PHI_ROCK,
) -> float:
    """
    Calculate storage capacity for bioretention with underdrain.

    Eq. 101.6:
    S = A_BRC × [d_p + (d_m − d_rab − Ø_u) × φ_BRC + (d_rab + Ø_u) × φ_rock]
          + (π × Ø_u² / 4) × L_u × (1 − φ_rock)

    Args:
        brc_area_ft2: Bioretention cell area (ft²)
        ponding_depth_ft: Ponding depth (ft)
        media_depth_ft: Media depth including underdrain (ft)
        rock_depth_ft: Combined rock above/below underdrain (ft)
        underdrain_diameter_ft: Underdrain pipe diameter (ft)
        underdrain_length_ft: Underdrain length (ft)
        media_porosity: Media porosity (–)
        rock_porosity: Rock porosity (–)

    Returns:
        storage (ft³): Total storage capacity
    """
    # Media zone storage
    media_zone_depth = (media_depth_ft - rock_depth_ft - underdrain_diameter_ft) * media_porosity

    # Rock zone storage (rock above, below, and around pipe)
    rock_zone_depth = (rock_depth_ft + underdrain_diameter_ft) * rock_porosity

    # Total depth in cell
    cell_storage_depth = ponding_depth_ft + media_zone_depth + rock_zone_depth
    cell_storage = brc_area_ft2 * cell_storage_depth

    # Underdrain pipe storage
    pipe_volume = (math.pi * (underdrain_diameter_ft ** 2) / 4.0) * underdrain_length_ft
    pipe_storage = pipe_volume * (1.0 - rock_porosity)

    return cell_storage + pipe_storage


def calculate_orifice_diameter(
    brc_area_ft2: float,
    head_ft: float,
    design_time_sec: float = TD_DESIGN,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> float:
    """
    Calculate slow-release orifice diameter.

    Eq. 101.12: Ø_o = (8 × A_BRC / (π × C_d × t_d))^0.5 × [h / (2g)]^0.25

    Args:
        brc_area_ft2: Bioretention cell area (ft²)
        head_ft: Available head height (ft) — typically ponding depth
        design_time_sec: Design detention time (seconds) — default 48 hrs
        discharge_coeff: Discharge coefficient — 0.61 for sharp-edge
        gravity: Gravitational constant (ft/s²)

    Returns:
        diameter (ft): Orifice diameter in feet
    """
    term1 = math.sqrt(8.0 * brc_area_ft2 / (math.pi * discharge_coeff * design_time_sec))
    term2 = (head_ft / (2.0 * gravity)) ** 0.25
    return term1 * term2


def verify_orifice_detention(
    brc_area_ft2: float,
    orifice_diameter_ft: float,
    head_ft: float,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> float:
    """
    Verify actual detention time with rounded orifice diameter.

    Eq. 101.13: t_d = (8 × A_BRC) / (π × C_d × Ø_o²) × sqrt(h / (2g))

    Result should fall between 42–54 hours.

    Args:
        brc_area_ft2: Bioretention cell area (ft²)
        orifice_diameter_ft: Orifice diameter (ft) — use rounded value
        head_ft: Available head (ft)
        discharge_coeff: Discharge coefficient
        gravity: Gravitational constant (ft/s²)

    Returns:
        time_sec (seconds): Actual detention time
    """
    time_sec = (
        (8.0 * brc_area_ft2)
        / (math.pi * discharge_coeff * orifice_diameter_ft ** 2)
        * math.sqrt(head_ft / (2.0 * gravity))
    )
    return time_sec


# ============================================================================
# PDF REPORT GENERATION
# ============================================================================

def generate_pdf_report(inputs: dict, results: dict) -> bytes:
    """Generate a compact 1-page PDF summary of the BRC design."""
    buf = io.BytesIO()
    styles = getSampleStyleSheet()

    # Letter page: 8.5 in wide. Margins 0.5 in each side → usable = 7.5 in.
    MARGIN = 0.5 * inch
    W = 7.5 * inch  # usable content width

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )

    # ── colour palette ───────────────────────────────────────────────────────
    NAVY   = colors.HexColor("#1A3A5C")   # title banner
    LBLUE  = colors.HexColor("#D6E4F0")   # section header bar
    DBLUE  = colors.HexColor("#2874A6")   # section header text
    GREEN  = colors.HexColor("#D5F5E3")
    DGREEN = colors.HexColor("#1E8449")
    RED    = colors.HexColor("#FADBD8")
    DRED   = colors.HexColor("#C0392B")
    LGREY  = colors.HexColor("#F2F3F4")
    MGREY  = colors.lightgrey

    # ── reusable paragraph styles ────────────────────────────────────────────
    def _p(txt, size=8.0, bold=False, color=colors.black, leading=None):
        s = styles["Normal"].clone(f"s{size}{bold}")
        s.fontSize = size
        s.leading  = leading or (size + 2)
        s.textColor = color
        if bold:
            txt = f"<b>{txt}</b>"
        return Paragraph(txt, s)

    def _kv_table(rows):
        """4-col table: label | value | label | value, total width = W."""
        CW = (2.15 * inch, 1.6 * inch, 2.15 * inch, 1.6 * inch)  # sum = 7.5 in
        data = []
        for i in range(0, len(rows), 2):
            left  = rows[i]
            right = rows[i + 1] if i + 1 < len(rows) else ("", "")
            data.append([
                _p(left[0],  bold=True),  _p(left[1]),
                _p(right[0], bold=True),  _p(right[1]),
            ])
        t = Table(data, colWidths=CW)
        n = len(data)
        style_cmds = [
            ("GRID",          (0, 0), (-1, -1), 0.3, MGREY),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            # shade label columns
            ("BACKGROUND",    (0, 0), (0, n - 1), LGREY),
            ("BACKGROUND",    (2, 0), (2, n - 1), LGREY),
            # alternating row tint on value columns
        ]
        for r in range(n):
            bg = colors.white if r % 2 == 0 else colors.HexColor("#F8FBFD")
            style_cmds.append(("BACKGROUND", (1, r), (1, r), bg))
            style_cmds.append(("BACKGROUND", (3, r), (3, r), bg))
        t.setStyle(TableStyle(style_cmds))
        return t

    def _section_header(txt):
        """Blue bar with white bold section title."""
        header_para = _p(txt, size=9, bold=True, color=colors.white)
        t = Table([[header_para]], colWidths=[W])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), DBLUE),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    # ── title banner ─────────────────────────────────────────────────────────
    title_para = _p(
        "Bio-Retention Cell (BRC) Design Report",
        size=14, bold=True, color=colors.white,
    )
    sub_para = _p(
        f"City of Tulsa LID Manual (2026) — Chapter 101 · Design Process      "
        f"Generated: {date.today().strftime('%B %d, %Y')}",
        size=7.5, color=colors.HexColor("#AED6F1"),
    )
    banner = Table([[title_para], [sub_para]], colWidths=[W])
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (0, 0),   8),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
        ("TOPPADDING",    (0, 1), (-1, -1), 1),
    ]))

    # ── build story ──────────────────────────────────────────────────────────
    inp = inputs
    res = results
    story = [banner, Spacer(1, 8)]

    # Section 1: Inputs
    story.append(_section_header("1   Design Inputs"))
    story.append(Spacer(1, 2))
    story.append(_kv_table([
        ("BRC Placement",            inp["placement"]),
        ("Impervious Area",          f"{inp['impervious_area']:,.0f} ft²"),
        ("Pervious Area",            f"{inp['pervious_area']:,.0f} ft²"),
        ("Total Contributing Area",  f"{inp['total_area']:,.0f} ft²"),
        ("Native Soil Type",         inp["soil_type"]),
        ("Native Infiltration Rate", f"{inp['native_infiltration']:.3f} in/hr"),
        ("Design System",            "Engineered Media + Underdrain" if inp["use_engineered"] else "Native Soil (No Underdrain)"),
        ("Design Infiltration Rate", f"{inp['infiltration_rate']:.3f} in/hr"),
        ("Ponding Depth",            f"{inp['ponding_depth']:.2f} ft"),
        ("Media Depth",              f"{inp['media_depth']:.2f} ft" if inp.get("media_depth") else "N/A"),
        ("Underdrain Diameter",      f"{inp['underdrain_diameter_in']:.2f} in" if inp.get("underdrain_diameter_in") else "N/A"),
        ("Underdrain Length",        f"{inp['underdrain_length_ft']:.1f} ft" if inp.get("underdrain_length_ft") else "N/A"),
        ("Design Precipitation",     f"{inp['precip_depth']:.2f} in"),
        ("BRC Cell Area",            f"{inp['brc_area']:,.0f} ft²"),
    ]))
    story.append(Spacer(1, 8))

    # Section 2: Results
    story.append(_section_header("2   Design Results"))
    story.append(Spacer(1, 2))
    lr_sym = ">=" if res["lr_valid"] else "<"
    results_rows = [
        ("Loading Ratio (LR)",       f"{res['loading_ratio']:.2%}  ({lr_sym} 3% target)"),
        ("SWV Required",             f"{res['swv_required']:.2f} ft³"),
        ("Storage Capacity",         f"{res['storage_capacity']:.2f} ft³  (delta {res['storage_capacity']-res['swv_required']:+.2f} ft³)"),
        ("Surface Drawdown (t_sp)",  f"{res['t_sp']:.1f} hrs  (limit 24 hrs)"),
        ("Total Drawdown (t_dd)",    f"{res['t_dd']:.1f} hrs  (limit 48 hrs)"),
        ("Max Ponding Allowed",      f"{res['max_ponding']:.2f} ft"),
        ("Max Media Depth Allowed",  f"{res['max_media']:.2f} ft" if res.get("max_media") else "N/A"),
    ]
    if res.get("underdrain_control_mode"):
        results_rows.insert(4, ("Underdrain Orifice Time", f"{res['underdrain_orifice_time_hr']:.2f} hrs"))
        results_rows.insert(5, ("Media Infiltration Time", f"{res['underdrain_infiltration_time_hr']:.2f} hrs"))
        results_rows.insert(6, ("Controlling Path", res["underdrain_control_mode"]))
        results_rows.insert(7, ("", ""))
    else:
        results_rows.insert(4, ("Infiltration Storage", "4.0 hrs (fixed)"))
    story.append(_kv_table(results_rows))

    # Loading ratio PASS / FAIL badge
    lr_text  = "Loading Ratio  PASS" if res["lr_valid"] else "Loading Ratio  FAIL"
    lr_color = DGREEN if res["lr_valid"] else DRED
    lr_bg    = GREEN  if res["lr_valid"] else RED
    lr_para  = _p(lr_text, size=9, bold=True, color=lr_color)
    lr_tbl   = Table([[lr_para]], colWidths=[W])
    lr_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), lr_bg),
        ("BOX",           (0, 0), (-1, -1), 0.5, lr_color),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(Spacer(1, 4))
    story.append(lr_tbl)
    story.append(Spacer(1, 8))

    # Section 3: Orifice (if applicable)
    if res.get("orifice_dia_in") is not None:
        story.append(_section_header("3   Slow-Release Orifice Outlet  (Eqs. 101.12 - 101.13)"))
        story.append(Spacer(1, 2))
        story.append(_kv_table([
            ("Calculated Orifice Dia.",  f"{res['orifice_dia_in']:.3f} in"),
            ("Rounded Orifice Dia.",     f"{res['orifice_dia_64ths_num']}/64 in  ({res['orifice_dia_in_rounded']:.4f} in)"),
            ("Head for Calculation",     f"{res['head_height']:.3f} ft"),
            ("Detention Time Check",     f"{res['detention_time_hr']:.1f} hrs  (target 42-54 hrs)"),
        ]))
        story.append(Spacer(1, 8))

    # Section 4: Overall Status
    sec_num = "4" if res.get("orifice_dia_in") is not None else "3"
    story.append(_section_header(f"{sec_num}   Overall Design Status"))
    story.append(Spacer(1, 2))

    valid = res["design_valid"] and res["lr_valid"]
    status_text = "PASS  -  Design Valid" if valid else "FAIL  -  Design Invalid"
    status_color = DGREEN if valid else DRED
    status_bg    = GREEN  if valid else RED

    status_para = _p(status_text, size=11, bold=True, color=status_color)
    status_tbl = Table([[status_para]], colWidths=[W])
    status_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), status_bg),
        ("BOX",           (0, 0), (-1, -1), 1.0, status_color),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(status_tbl)

    issues_list = res.get("issues", [])
    if issues_list:
        story.append(Spacer(1, 4))
        for issue in issues_list:
            # strip markdown bold markers for plain PDF text
            clean = issue.replace("**", "")
            story.append(_p(f"  - {clean}", size=8, color=DRED))

    # Footer
    story.append(Spacer(1, 10))
    footer_tbl = Table(
        [[_p("Reference: City of Tulsa Engineering Manual (2021), Chapter 101: Bioretention and Biofiltration",
             size=7, color=colors.HexColor("#7F8C8D"))]],
        colWidths=[W],
    )
    footer_tbl.setStyle(TableStyle([
        ("LINEABOVE",     (0, 0), (-1, 0), 0.5, MGREY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_tbl)

    doc.build(story)
    return buf.getvalue()


# ============================================================================
# UI AND MAIN APP
# ============================================================================

def main() -> None:
    _init_state()

    st.title("Bio-Retention Cell (BRC) Design Tool")
    st.caption("City of Tulsa LID Manual (2026) — Chapter 101")
    _render_site_selector()

    # ========================================================================
    # SIDEBAR INPUTS
    # ========================================================================
    st.sidebar.header("Design Inputs")

    # BRC Placement
    st.sidebar.subheader("BRC Placement")
    placement = st.sidebar.radio(
        "BRC Location Relative to Contributing Area",
        options=["Outside Placement", "Inside Placement"],
        index=0,
        help=(
            "**Outside Placement:** BRC is located outside the contributing drainage area. "
            "Loading Ratio = A_BRC / (A_imp + A_perv).\n\n"
            "**Inside Placement:** BRC is located within the contributing drainage area. "
            "Loading Ratio = A_BRC / (A_imp + A_perv + A_BRC)."
        ),
    )
    inside_placement = placement == "Inside Placement"

    # Contributing Area & SWV
    st.sidebar.subheader("Contributing Drainage Area")

    _c1, _c2 = st.sidebar.columns(2)
    impervious_area = _c1.number_input(
        "Impervious (ft²)",
        min_value=0.0,
        value=4100.0,
        step=100.0,
        help="Total contributing impervious area (roof, pavement, etc.)",
    )

    pervious_area = _c2.number_input(
        "Pervious (ft²)",
        min_value=0.0,
        value=2400.0,
        step=100.0,
        help="Total contributing pervious area (open space).",
    )

    total_area = impervious_area + pervious_area
    st.sidebar.caption(f"Total: **{total_area:,.0f} ft²**")

    # Infiltration Rate
    st.sidebar.subheader("Infiltration Rate")

    soil_options = list(SOIL_INFILTRATION_RATES.keys())
    if st.session_state["brc_soil_type"] not in SOIL_INFILTRATION_RATES:
        st.session_state["brc_soil_type"] = DEFAULT_SOIL_TYPE
    soil_type = st.sidebar.selectbox(
        "Native Soil Type",
        options=soil_options,
        key="brc_soil_type",
        help="Select native soil from Table 101.2. Used to determine default infiltration rate.",
    )
    if soil_type != st.session_state.get("brc_prev_soil_type"):
        st.session_state["brc_prev_soil_type"] = soil_type
        st.session_state["brc_native_infiltration"] = float(SOIL_INFILTRATION_RATES[soil_type])

    native_infiltration_default = SOIL_INFILTRATION_RATES[soil_type]

    native_infiltration = st.sidebar.number_input(
        "Native Infiltration Rate (in/hr)",
        min_value=0.001,
        step=0.1,
        format="%.3f",
        key="brc_native_infiltration",
        help=(
            f"Default value from Table 101.2 for **{soil_type}**: {native_infiltration_default} in/hr. "
            "You may edit this value to match site-specific test data."
        ),
    )

    # Decision: Can native soil handle design in 48 hrs?
    if native_infiltration < 0.5:
        use_engineered = st.sidebar.checkbox(
            "Use engineered media + underdrain?",
            value=True,
            help="Native soil infiltration too low. Engineered media (6 in/hr) recommended.",
        )
    else:
        use_engineered = st.sidebar.checkbox(
            "Use engineered media + underdrain?",
            value=False,
            help="Optional: Use engineered media for smaller cell footprint.",
        )

    if use_engineered:
        engineered_infiltration = st.sidebar.number_input(
            "Engineered Media Infiltration Rate (in/hr)",
            min_value=0.001,
            value=float(I_BRC_ENGINEERED),
            step=0.5,
            format="%.2f",
            help=f"Default engineered media rate: {I_BRC_ENGINEERED} in/hr. Edit to match specified media.",
        )
        infiltration_rate = engineered_infiltration
        underdrain_required = True
        st.sidebar.info(
            f"**Engineered Media Design**\n\n"
            f"Infiltration: {infiltration_rate} in/hr\n"
            f"Underdrain: Required\n"
            f"Media porosity: {PHI_BRC}"
        )
    else:
        infiltration_rate = native_infiltration
        underdrain_required = False
        st.sidebar.info(
            f"**Native Soil Design**\n\n"
            f"Infiltration: {infiltration_rate} in/hr\n"

        )

    # Ponding Depth and (optional) Media Depth
    max_ponding = get_max_ponding_depth(infiltration_rate)

    media_depth = None
    if underdrain_required:
        max_media = get_max_media_depth(infiltration_rate, PHI_BRC)
        underdrain_diameter_in = float(OU_PIPE_IN)
        underdrain_length_ft   = float(LU_UNDERDRAIN)
    else:
        underdrain_diameter_in = OU_PIPE_IN
        underdrain_length_ft   = LU_UNDERDRAIN

    # Design precipitation depth
    st.sidebar.subheader("Precipitation")
    precip_depth = st.sidebar.number_input(
        "Design Precipitation Depth (in)",
        min_value=0.5,
        value=DESIGN_PRECIPITATION,
        step=0.1,
        help="**Eq. 101.9:** Depth of rain falling on BRC surface (typical: 1.2 in for Tulsa).",
    )

    # ========================================================================
    # MAIN AREA: DEPTHS & CELL AREA
    # ========================================================================
    st.divider()
    if underdrain_required:
        st.subheader("Depths and Cell Area")
        _d1, _d2, _d3 = st.columns(3)
        ponding_depth = _d1.number_input(
            f"Ponding Depth (ft)  —  max {max_ponding:.2f} ft",
            min_value=0.1,
            value=0.75,
            step=0.05,
            format="%.2f",
        )
        media_depth = _d2.number_input(
            f"Media Depth (ft)  —  max {max_media:.2f} ft",
            min_value=0.5,
            value=2.75,
            step=0.1,
            format="%.2f",
        )
        brc_area = _d3.number_input(
            "Bioretention Cell Area (ft²)",
            min_value=10.0,
            value=225.0,
            step=10.0,
            help="Total cell footprint area. Adjust until storage capacity ≥ SWV and loading ratio ≥ 3%.",
        )
    else:
        st.subheader("Ponding Depth and Cell Area")
        _d1, _d2 = st.columns(2)
        ponding_depth = _d1.number_input(
            f"Ponding Depth (ft)  —  max {max_ponding:.2f} ft",
            min_value=0.1,
            value=0.75,
            step=0.05,
            format="%.2f",
        )
        brc_area = _d2.number_input(
            "Bioretention Cell Area (ft²)",
            min_value=10.0,
            value=260.0,
            step=10.0,
            help="Total cell footprint area. Adjust until storage capacity ≥ SWV and loading ratio ≥ 3%.",
        )

    # ========================================================================
    # MAIN AREA: CELL AREA CHECK
    # ========================================================================
    st.divider()
    st.subheader("Cell Area Check")

    # Compute loading ratio denominator based on placement
    if inside_placement:
        lr_denominator = total_area + brc_area
        lr_label = "A_C (incl. BRC)"
    else:
        lr_denominator = total_area
        lr_label = "A_C (excl. BRC)"

    loading_ratio = calculate_loading_ratio(brc_area, lr_denominator)
    lr_valid = loading_ratio >= TARGET_LOADING_RATIO

    st.info(
        f"**Placement:** {placement}  \n"
        f"**Loading Ratio:** A_BRC / {lr_label} = {brc_area:,.0f} / {lr_denominator:,.0f} = **{loading_ratio:.2%}**  \n"
        f"{'LR ≥ 3% — OK' if lr_valid else 'LR < 3% — Consider increasing cell area'}"
    )

    if underdrain_required:
        st.sidebar.subheader("Underdrain")
        _u1, _u2 = st.sidebar.columns(2)
        underdrain_diameter_in = _u1.number_input(
            "Diameter (in)",
            min_value=0.5,
            value=float(OU_PIPE_IN),
            step=0.25,
            format="%.2f",
            help="Underdrain pipe diameter.",
        )
        underdrain_length_ft = _u2.number_input(
            "Length (ft)",
            min_value=1.0,
            value=max(brc_area / 10.0, 1.0),
            step=1.0,
            format="%.1f",
            help="Default = BRC area / 10.",
        )

    # ========================================================================
    # CALCULATIONS
    # ========================================================================

    swv_required = calculate_swv(impervious_area, brc_area, precip_depth)

    if underdrain_required and media_depth:
        t_sp, surface_orifice_time_hr, surface_infiltration_time_hr = calculate_underdrain_surface_drawdown_time(
            brc_area_ft2=brc_area,
            ponding_depth_ft=ponding_depth,
            infiltration_rate_in_hr=infiltration_rate,
            underdrain_diameter_in=underdrain_diameter_in,
            discharge_coeff=CD_ORIFICE,
            gravity=G_GRAVITY,
        )
        t_dd, underdrain_orifice_time_hr, underdrain_infiltration_time_hr, underdrain_control_mode = (
            calculate_underdrain_total_drawdown_time(
                surface_ponding_time_hr=t_sp,
                brc_area_ft2=brc_area,
                ponding_depth_ft=ponding_depth,
                media_depth_ft=media_depth,
                infiltration_rate_in_hr=infiltration_rate,
                rock_depth_ft=D_RAB,
                underdrain_diameter_in=underdrain_diameter_in,
                media_porosity=PHI_BRC,
                rock_porosity=PHI_ROCK,
                discharge_coeff=CD_ORIFICE,
                gravity=G_GRAVITY,
            )
        )
        t_dd_additional_hr = max(underdrain_orifice_time_hr, underdrain_infiltration_time_hr)
        surface_control_mode = "Underdrain orifice" if surface_orifice_time_hr > surface_infiltration_time_hr else "Media infiltration"
    else:
        t_sp = calculate_surface_ponding_drawdown(ponding_depth, infiltration_rate)
        t_dd = calculate_total_drawdown_time(t_sp)
        underdrain_orifice_time_hr = None
        underdrain_infiltration_time_hr = None
        underdrain_control_mode = None
        t_dd_additional_hr = TINF
        surface_orifice_time_hr = None
        surface_infiltration_time_hr = None
        surface_control_mode = None

    t_sp_valid = t_sp <= TS_SURFACE
    t_dd_valid = t_dd <= TDD_TOTAL

    # Calculate storage capacity
    if underdrain_required and media_depth:
        storage_capacity = calculate_storage_with_underdrain(
            brc_area,
            ponding_depth,
            media_depth,
            rock_depth_ft=D_RAB,
            underdrain_diameter_ft=underdrain_diameter_in / 12.0,
            underdrain_length_ft=underdrain_length_ft,
            media_porosity=PHI_BRC,
            rock_porosity=PHI_ROCK,
        )
    else:
        storage_capacity = calculate_storage_no_underdrain(
            brc_area, ponding_depth, infiltration_rate
        )

    swv_valid = storage_capacity >= swv_required

    # Overall design validity
    design_valid = swv_valid and t_sp_valid and t_dd_valid

    # ========================================================================
    # RESULTS DISPLAY
    # ========================================================================

    st.divider()
    st.subheader("Results")

    # Compact single-row summary
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Impervious", f"{impervious_area:,.0f} ft²")
    m2.metric("Pervious",   f"{pervious_area:,.0f} ft²")
    m3.metric("Design Infiltration", f"{infiltration_rate} in/hr")
    m4.metric("Ponding Depth", f"{ponding_depth:.2f} ft",
              help="Eq. 101.3")
    if underdrain_required and media_depth:
        m5.metric("Media Depth", f"{media_depth:.2f} ft", help="Eq. 101.4")
    m6.metric("Underdrain", "Yes" if underdrain_required else "No")

    # SWV / Storage / Drawdown in one row
    n1, n2, n3, n4, n5 = st.columns(5)
    n1.metric("SWV Required (ft³)", f"{swv_required:.1f}",
              help="**Eq. 101.9:** SWV = A_ip/12 + A_BRC × D_prec/12")
    n2.metric("Storage Capacity (ft³)", f"{storage_capacity:.1f}",
              delta=f"{storage_capacity - swv_required:+.1f} ft³")
    n3.metric("Surface Drawdown", f"{t_sp:.1f} hrs",
              f"limit {TS_SURFACE:.0f} hrs", help="Eq. 101.7")
    if underdrain_required and media_depth:
        n4.metric(
            "Underdrain Addl. Time",
            f"{t_dd_additional_hr:.1f} hrs",
            help=f"Controlling path: {underdrain_control_mode}",
        )
    else:
        n4.metric("Infilt. Storage", f"{TINF:.0f} hrs (fixed)")
    n5.metric("Total Drawdown",  f"{t_dd:.1f} hrs",
              f"limit {TDD_TOTAL:.0f} hrs", help="Eq. 101.8")

    if underdrain_required and media_depth:
        u1, u2, u3 = st.columns(3)
        u1.metric("Underdrain Diameter", f"{underdrain_diameter_in:.2f} in")
        u2.metric("Underdrain Length", f"{underdrain_length_ft:.1f} ft")
        u3.metric("Pipe Storage Input", f"{underdrain_diameter_in / 12.0:.3f} ft dia")

    # Depth constraint warnings (inline, no extra section)
    if underdrain_required and media_depth:
        max_p = get_max_ponding_depth(infiltration_rate)
        max_m = get_max_media_depth(infiltration_rate, PHI_BRC)
        if ponding_depth > max_p:
            st.warning(f"Ponding {ponding_depth:.2f} ft exceeds Eq. 101.3 max {max_p:.2f} ft")
        if media_depth > max_m:
            st.warning(f"Media depth {media_depth:.2f} ft exceeds Eq. 101.4 max {max_m:.2f} ft")

    # Validation status row
    v1, v2, v3 = st.columns(3)
    if swv_valid:
        v1.success(f"Storage OK ({storage_capacity:.1f} ≥ {swv_required:.1f} ft³)")
    else:
        shortfall = swv_required - storage_capacity
        required_area_increase = (swv_required / storage_capacity - 1) * 100
        v1.error(
            f"Storage short {shortfall:.1f} ft³ — increase area ~{required_area_increase:.0f}%",
        )
    if t_sp_valid:
        v2.success(f"Surface drawdown OK ({t_sp:.1f} hrs)")
    else:
        v2.error(f"Surface drawdown {t_sp:.1f} hrs > {TS_SURFACE:.0f}-hr limit")
    if t_dd_valid:
        v3.success(f"Total drawdown OK ({t_dd:.1f} hrs)")
    else:
        v3.error(f"Total drawdown {t_dd:.1f} hrs > {TDD_TOTAL:.0f}-hr limit")

    with st.expander("Debug values", expanded=False):
        st.write("Surface drawdown case")
        st.write(
            {
                "surface_orifice_time_hr": surface_orifice_time_hr,
                "surface_infiltration_time_hr": surface_infiltration_time_hr,
                "surface_control_mode": surface_control_mode,
                "surface_drawdown_time_hr": t_sp,
            }
        )
        if underdrain_required and media_depth:
            st.write("Additional underdrain case")
            st.write(
                {
                    "media_orifice_time_hr": underdrain_orifice_time_hr,
                    "media_infiltration_time_hr": underdrain_infiltration_time_hr,
                    "media_control_mode": underdrain_control_mode,
                    "additional_drawdown_time_hr": t_dd_additional_hr,
                    "total_drawdown_time_hr": t_dd,
                }
            )
        else:
            st.write("Additional underdrain case: not applicable")

    if underdrain_required and media_depth:
        st.caption(
            f"Underdrain drawdown = surface drawdown + max(orifice time, infiltration-limited time)  ·  "
            f"orifice {underdrain_orifice_time_hr:.2f} hrs  ·  infiltration {underdrain_infiltration_time_hr:.2f} hrs  ·  "
            f"controlling path: {underdrain_control_mode}  ·  diameter {underdrain_diameter_in:.2f} in  ·  length {underdrain_length_ft:.1f} ft"
        )

    # Orifice outlet (if underdrain and valid)
    orifice_dia_in_rounded = None
    if underdrain_required and design_valid and media_depth:
        st.divider()
        st.subheader("Optional Underdrain Orifice Outlet (Eqs. 101.12, 101.13)")

        # Use ponding depth as head (conservative)
        head_height = ponding_depth + media_depth * PHI_BRC
        orifice_dia_ft = calculate_orifice_diameter(brc_area, head_height)
        orifice_dia_in = orifice_dia_ft * 12.0

        # Round to nearest 1/64 inch
        orifice_dia_64ths_num = math.ceil(orifice_dia_in * 64 + 0.5)  # numerator (X in X/64)
        orifice_dia_in_rounded = orifice_dia_64ths_num / 64.0
        orifice_dia_ft_rounded = orifice_dia_in_rounded / 12.0

        # Verify detention time with rounded orifice
        detention_time_sec = verify_orifice_detention(
            brc_area,
            orifice_dia_ft_rounded,
            head_height,
        )
        detention_time_hr = detention_time_sec / 3600.0

        col_orif1, col_orif2, col_orif3 = st.columns(3)

        with col_orif1:
            st.metric(
                "Calculated Diameter (Eq. 101.12)",
                f"{orifice_dia_in:.3f} in",
                help=(
                    "Eq. 101.12: Ø_o = √(8·A_BRC / (π·C_d·t_d)) × [h/(2g)]^0.25"
                ),
            )

        with col_orif2:
            st.metric(
                "Rounded Diameter",
                f"{orifice_dia_64ths_num}/64 in  ({orifice_dia_in_rounded:.4f} in)",
            )

        with col_orif3:
            st.metric(
                "Detention Time Check (Eq. 101.13)",
                f"{detention_time_hr:.1f} hrs",
                help=(
                    "Eq. 101.13: t_d = (8·A_BRC / (π·C_d·Ø_o²)) × √(h/(2g))"
                ),
            )

        detention_valid = 42.0 <= detention_time_hr <= 54.0
        if detention_valid:
            st.success(
                f"Detention time ({detention_time_hr:.1f} hrs) within 42–54 hr target (Table 101.3)",
            )
        else:
            st.warning(
                f"Detention time ({detention_time_hr:.1f} hrs) outside 42–54 hr target. "
                f"Consider adjusting orifice diameter.",
            )

    # Overall Design Status
    issues = []
    st.divider()
    if design_valid and lr_valid:
        st.success(
            f"**DESIGN VALID** — "
            f"Storage {storage_capacity:.1f} ft³ ≥ {swv_required:.1f} ft³  ·  "
            f"Drawdown {t_dd:.1f} hrs  ·  LR {loading_ratio:.2%}",
        )
    else:
        if not swv_valid:
            issues.append(
                f"**Storage short** {swv_required - storage_capacity:.1f} ft³ — "
                f"increase area ~{(swv_required / storage_capacity - 1) * 100:.0f}% or increase depths"
            )
        if not t_sp_valid:
            issues.append(
                f"**Surface drawdown** {t_sp:.1f} hrs > {TS_SURFACE:.0f}-hr limit — "
                f"reduce ponding depth or increase infiltration rate"
            )
        if not t_dd_valid:
            issues.append(
                f"**Total drawdown** {t_dd:.1f} hrs > {TDD_TOTAL:.0f}-hr limit — "
                f"reduce ponding depth, increase infiltration rate, or adjust underdrain sizing"
            )
        if not lr_valid:
            issues.append(
                f"**Loading ratio** {loading_ratio:.2%} < {TARGET_LOADING_RATIO:.1%} — "
                f"increase cell area (guidance for initial sizing)"
            )
        st.error(
            "**DESIGN INVALID**\n\n" + "\n\n".join(f"- {i}" for i in issues),
        )

    # ========================================================================
    # PDF DOWNLOAD
    # ========================================================================
    st.divider()

    max_ponding_val = get_max_ponding_depth(infiltration_rate)
    max_media_val   = get_max_media_depth(infiltration_rate, PHI_BRC) if underdrain_required else None

    # Collect orifice results (if computed)
    orifice_results: dict = {}
    if underdrain_required and design_valid and media_depth:
        head_height_pdf = ponding_depth + media_depth * PHI_BRC
        _odia_ft  = calculate_orifice_diameter(brc_area, head_height_pdf)
        _odia_in  = _odia_ft * 12.0
        _o64      = math.ceil(_odia_in * 64 + 0.5)
        _odia_r   = _o64 / 64.0
        _det_sec  = verify_orifice_detention(brc_area, _odia_r / 12.0, head_height_pdf)
        orifice_results = {
            "orifice_dia_in":        _odia_in,
            "orifice_dia_64ths_num": _o64,
            "orifice_dia_in_rounded": _odia_r,
            "head_height":           head_height_pdf,
            "detention_time_hr":     _det_sec / 3600.0,
        }

    pdf_inputs = {
        "placement":           placement,
        "impervious_area":     impervious_area,
        "pervious_area":       pervious_area,
        "total_area":          total_area,
        "soil_type":           soil_type,
        "native_infiltration": native_infiltration,
        "use_engineered":      underdrain_required,
        "infiltration_rate":   infiltration_rate,
        "ponding_depth":       ponding_depth,
        "media_depth":         media_depth,
        "underdrain_diameter_in": underdrain_diameter_in,
        "underdrain_length_ft": underdrain_length_ft,
        "precip_depth":        precip_depth,
        "brc_area":            brc_area,
    }

    pdf_results = {
        "loading_ratio":    loading_ratio,
        "lr_valid":         lr_valid,
        "swv_required":     swv_required,
        "storage_capacity": storage_capacity,
        "t_sp":             t_sp,
        "t_dd":             t_dd,
        "t_dd_additional_hr": t_dd_additional_hr,
        "underdrain_orifice_time_hr": underdrain_orifice_time_hr,
        "underdrain_infiltration_time_hr": underdrain_infiltration_time_hr,
        "underdrain_control_mode": underdrain_control_mode,
        "design_valid":     design_valid,
        "max_ponding":      max_ponding_val,
        "max_media":        max_media_val,
        "issues":           issues if not design_valid or not lr_valid else [],
        **orifice_results,
    }

    pdf_bytes = generate_pdf_report(pdf_inputs, pdf_results)
    st.download_button(
        label="Download 1-Page PDF Report",
        data=pdf_bytes,
        file_name="BRC_Design_Report.pdf",
        mime="application/pdf",
        use_container_width=True,
    )


if __name__ == "__main__":
    st.set_page_config(
        page_title="Bio-Retention Cell (BRC) Design Tool",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    main()
