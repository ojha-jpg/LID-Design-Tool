"""
Permeable Pavement (PP) Design Tool
Based on City of Tulsa LID Manual (2026) - Chapter 103

This tool implements the design process for permeable pavements:
- Site selection and siting offsets
- Contributing area and SWV (stormwater volume)
- Infiltration rate
- PP surface type
- Maximum storage depth check (underdrain decision)
- PP area and storage capacity
- Underdrain sizing
- Slow-release orifice outlet

Reference: City of Tulsa Engineering Manual (2021), Chapter 103: Permeable Pavements
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
# CONSTANTS AND DEFAULTS (from Tulsa LID Manual, Chapter 103)
# ============================================================================

# Default design precipitation
DESIGN_PRECIPITATION = 1.20  # inches (Eq. 103.6) — produces ~1 in of runoff from impervious
TARGET_LOADING_RATIO = 0.03  # 3% minimum target

# Subbase aggregate properties
PHI_S = 0.40        # Subbase aggregate porosity (#57 stone typical)
MIN_BASE_DEPTH = 0.5  # Minimum aggregate base = 6 in = 0.5 ft (Table 103.3)

# Design time criteria
TDD_TOTAL = 48.0    # Maximum total drawdown time (hrs) — Eq. 103.2
TINF = 4.0          # Infiltration time credited toward storage capacity (hrs) — Eq. 103.3

# Underdrain specifications
OU_PIPE_IN = 3.0    # Underdrain pipe diameter (inches) — minimum 3 in typical
OU_PIPE_FT = OU_PIPE_IN / 12.0
LU_UNDERDRAIN = 100.0   # Typical underdrain length (ft)

# Orifice outlet constants
CD_ORIFICE = 0.61       # Discharge coefficient for sharp-edge orifice
G_GRAVITY = 32.2        # Gravitational constant (ft/s²)
TD_DESIGN = 48.0 * 3600.0  # Design detention time = 48 hrs in seconds

# Native soil infiltration rates (Table 100.3, normal bulk density)
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

# PP surface types with minimum thicknesses (Table 103.3)
PP_SURFACE_TYPES = {
    "Pervious Concrete": {
        "min_surface_in": 5.5,
        "min_base_in": 6,
        "base_material": "#57",
        "choker_course": "N/A",
    },
    "Porous Asphalt": {
        "min_surface_in": 4.0,
        "min_base_in": 6,
        "base_material": "#2 or #3",
        "choker_course": "2 in #57",
    },
    "Permeable Blocks (PICP)": {
        "min_surface_in": None,  # Mfg. Spec.
        "min_base_in": 6,
        "base_material": "#57",
        "choker_course": "Mfg. Spec. (#8 or #89)",
    },
    "Grid Pavement": {
        "min_surface_in": None,  # Mfg. Spec.
        "min_base_in": 6,
        "base_material": "#57",
        "choker_course": "Mfg. Spec. (#8 or #89)",
    },
}


DEFAULT_SOIL_TYPE = "Sandy Loam"
OKLAHOMA_CENTER = [35.5, -97.5]
DEFAULT_MAP_ZOOM = 7
_fragment = getattr(st, "fragment", lambda f: f)


def _init_state() -> None:
    """Initialise PP widget state used for SSURGO-based soil lookup."""
    defaults = {
        "pp_soil_type": DEFAULT_SOIL_TYPE,
        "pp_prev_soil_type": DEFAULT_SOIL_TYPE,
        "pp_native_infiltration": float(SOIL_INFILTRATION_RATES[DEFAULT_SOIL_TYPE]),
        "pp_soil_lookup_lat": "",
        "pp_soil_lookup_lon": "",
        "pp_soil_lookup_address": "",
        "pp_soil_lookup_result": None,
        "pp_soil_lookup_error": None,
        "pp_selected_lat": None,
        "pp_selected_lon": None,
        "pp_map_center": OKLAHOMA_CENTER,
        "pp_map_zoom": DEFAULT_MAP_ZOOM,
        "pp_geocode_match": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _soil_mix_summary(textures: dict[str, float]) -> str:
    """Compact USDA texture summary for sidebar display."""
    return ", ".join(f"{name} ({pct:.1f}%)" for name, pct in textures.items())


def _set_lookup_point(lat: float, lon: float, zoom: int = 13) -> None:
    """Sync a selected map/address point into the soil-lookup fields."""
    st.session_state["pp_selected_lat"] = float(lat)
    st.session_state["pp_selected_lon"] = float(lon)
    st.session_state["pp_map_center"] = [float(lat), float(lon)]
    st.session_state["pp_map_zoom"] = int(zoom)
    st.session_state["pp_soil_lookup_lat"] = f"{float(lat):.6f}"
    st.session_state["pp_soil_lookup_lon"] = f"{float(lon):.6f}"
    st.session_state["pp_soil_lookup_result"] = None
    st.session_state["pp_soil_lookup_error"] = None


def _perform_soil_lookup() -> None:
    """Fetch USDA soil texture for the currently selected/manual PP location."""
    try:
        lat = float(st.session_state["pp_soil_lookup_lat"])
        lon = float(st.session_state["pp_soil_lookup_lon"])
    except ValueError:
        st.session_state["pp_soil_lookup_error"] = "Enter valid decimal coordinates before running the soil lookup."
        st.session_state["pp_soil_lookup_result"] = None
        return

    soil_lookup, soil_lookup_live = fetch_site_soil_texture(lat, lon)
    if soil_lookup_live and soil_lookup:
        dominant_texture = soil_lookup.get("dominant_texture")
        inferred_soil = infer_design_soil_type(dominant_texture)
        soil_lookup["soil_type"] = inferred_soil
        st.session_state["pp_soil_lookup_result"] = soil_lookup
        st.session_state["pp_soil_lookup_error"] = None
        if inferred_soil in SOIL_INFILTRATION_RATES:
            st.session_state["pp_soil_type"] = inferred_soil
            st.session_state["pp_prev_soil_type"] = inferred_soil
            st.session_state["pp_native_infiltration"] = float(SOIL_INFILTRATION_RATES[inferred_soil])
    else:
        st.session_state["pp_soil_lookup_error"] = (
            "USDA soil texture lookup failed for that location. Adjust the location or enter soil data manually."
        )
        st.session_state["pp_soil_lookup_result"] = None


@_fragment
def _render_site_selector_map() -> None:
    """Fragment-isolated PP point-selection map with immediate marker updates."""
    if st.session_state["pp_selected_lat"] is not None:
        _map_center = [st.session_state["pp_selected_lat"], st.session_state["pp_selected_lon"]]
    else:
        _map_center = st.session_state.get("pp_map_center") or OKLAHOMA_CENTER
    _map_zoom = st.session_state.get("pp_map_zoom") or DEFAULT_MAP_ZOOM

    m = folium.Map(location=_map_center, zoom_start=_map_zoom, tiles="OpenStreetMap")
    MousePosition().add_to(m)

    if st.session_state["pp_selected_lat"] is not None:
        _lat = st.session_state["pp_selected_lat"]
        _lon = st.session_state["pp_selected_lon"]
        folium.Marker(
            location=[_lat, _lon],
            tooltip="Selected PP Site",
            popup=folium.Popup(
                f"<b>Selected point</b><br>{_lat:.5f}&deg;N, {_lon:.5f}&deg;",
                max_width=200,
            ),
            icon=folium.Icon(color="red", icon="info-sign"),
        ).add_to(m)

    map_data = st_folium(m, use_container_width=True, height=360, key="pp_site_selector_map")

    if map_data and map_data.get("last_clicked"):
        if map_data.get("center"):
            c = map_data["center"]
            st.session_state["pp_map_center"] = [c["lat"], c["lng"]]
        if map_data.get("zoom"):
            st.session_state["pp_map_zoom"] = map_data["zoom"]
        st.session_state["pp_selected_lat"] = map_data["last_clicked"]["lat"]
        st.session_state["pp_selected_lon"] = map_data["last_clicked"]["lng"]
        st.session_state["pp_soil_lookup_lat"] = f"{st.session_state['pp_selected_lat']:.6f}"
        st.session_state["pp_soil_lookup_lon"] = f"{st.session_state['pp_selected_lon']:.6f}"
        st.session_state["pp_geocode_match"] = None
        st.rerun()


def _render_site_selector() -> None:
    """Address search + clickable map for selecting the PP soil lookup point."""
    with st.expander("Site Selector", expanded=False):
        st.caption("Search by address or click the map to choose the PP location used for USDA soil lookup.")

        with st.form("pp_address_form", clear_on_submit=False):
            st.text_input(
                "Project Address",
                key="pp_soil_lookup_address",
                placeholder="e.g. 175 E 2nd St, Tulsa, OK",
            )
            geocode_submit = st.form_submit_button("Find Address", use_container_width=True)

        if geocode_submit:
            match, is_live = geocode_address(st.session_state["pp_soil_lookup_address"])
            if is_live and match:
                _set_lookup_point(match["lat"], match["lon"])
                st.session_state["pp_geocode_match"] = match["matched_address"]
            else:
                st.session_state["pp_geocode_match"] = None
                st.warning("Address lookup failed. Try a more specific US street address or click the map instead.")

        _render_site_selector_map()

        selected_lat = st.session_state.get("pp_selected_lat")
        selected_lon = st.session_state.get("pp_selected_lon")
        matched_address = st.session_state.get("pp_geocode_match")
        if matched_address:
            st.caption(f"Matched address: **{matched_address}**")
        if selected_lat is not None and selected_lon is not None:
            st.success(f"Selected Point: Latitude = {selected_lat:.6f}, Longitude = {selected_lon:.6f}")
            col_a, col_b = st.columns([5, 1])
            with col_a:
                if st.button("Fetch Soil For Selected Point", key="pp_fetch_soil_from_map", type="primary", use_container_width=True):
                    _perform_soil_lookup()
            with col_b:
                if st.button("Clear", key="pp_clear_site_point", use_container_width=True):
                    st.session_state["pp_selected_lat"] = None
                    st.session_state["pp_selected_lon"] = None
                    st.session_state["pp_soil_lookup_lat"] = ""
                    st.session_state["pp_soil_lookup_lon"] = ""
                    st.session_state["pp_geocode_match"] = None
                    st.session_state["pp_soil_lookup_result"] = None
                    st.session_state["pp_soil_lookup_error"] = None
                    try:
                        st.rerun(scope="app")
                    except TypeError:
                        st.rerun()

        soil_lookup_result = st.session_state.get("pp_soil_lookup_result")
        soil_lookup_error = st.session_state.get("pp_soil_lookup_error")
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
# DESIGN CALCULATIONS (following Tulsa LID Manual Chapter 103 equations)
# ============================================================================

def calculate_swv(
    impervious_area_ft2: float,
    pp_area_ft2: float,
    total_area_ft2: float,
    placement: str,
    precip_in: float = DESIGN_PRECIPITATION,
) -> float:
    """
    Calculate Stormwater Volume (SWV) required.

    Placement 1 (PP adjacent — Eq. 103.6a):
        SWV = A_ip × (1 in / 12) + A_PP × (D_p / 12)

    Placement 2 (PP replaces impervious — Eq. 103.6b):
        SWV = (A_total − A_PP) × (1 in / 12) + A_PP × (D_p / 12)

    Args:
        impervious_area_ft2: Total impervious area in drainage basin (ft²)
        pp_area_ft2: Permeable pavement area (ft²)
        total_area_ft2: Total drainage area (ft²) — used for Placement 2
        placement: "Placement 1 (Adjacent)" or "Placement 2 (Replaces Impervious)"
        precip_in: Design precipitation depth on PP surface (in)

    Returns:
        swv (ft³): Stormwater volume required
    """
    if "Placement 2" in placement:
        contributing_impervious = total_area_ft2 - pp_area_ft2
    else:
        contributing_impervious = impervious_area_ft2

    runoff_from_impervious = contributing_impervious / 12.0        # 1 in of runoff
    runoff_on_pp = pp_area_ft2 * (precip_in / 12.0)
    return runoff_from_impervious + runoff_on_pp


def calculate_loading_ratio(pp_area_ft2: float, contributing_area_ft2: float) -> float:
    """Calculate loading ratio as A_PP / A_contributing."""
    if contributing_area_ft2 <= 0:
        return 0.0
    return pp_area_ft2 / contributing_area_ft2


def calculate_max_storage_depth(infiltration_rate_in_hr: float, porosity: float = PHI_S) -> float:
    """
    Calculate maximum aggregate storage depth when no underdrain is used.

    Eq. 103.2: d_s_max (ft) = (I × 48) / (φ_S × 12)

    Args:
        infiltration_rate_in_hr: Native soil infiltration rate (in/hr)
        porosity: Subbase aggregate porosity (default 0.40)

    Returns:
        d_s_max (ft): Maximum allowable aggregate storage depth
    """
    return (infiltration_rate_in_hr * TDD_TOTAL) / (porosity * 12.0)


def calculate_storage_no_underdrain(
    pp_area_ft2: float,
    storage_depth_ft: float,
    infiltration_rate_in_hr: float,
    porosity: float = PHI_S,
) -> float:
    """
    Calculate storage capacity for PP without underdrain.

    S = A_PP × (φ_S × d_s + (I/12) × 4)

    4 hours of native-soil infiltration is credited toward storage capacity
    (Eq. 103.3), but this credit does NOT factor into the drawdown time formula.

    Args:
        pp_area_ft2: Permeable pavement area (ft²)
        storage_depth_ft: Selected aggregate storage depth (ft)
        infiltration_rate_in_hr: Native soil infiltration rate (in/hr)
        porosity: Subbase aggregate porosity

    Returns:
        storage (ft³): Total storage capacity
    """
    aggregate_storage = porosity * storage_depth_ft
    infiltration_credit = (infiltration_rate_in_hr / 12.0) * TINF
    return pp_area_ft2 * (aggregate_storage + infiltration_credit)


def calculate_storage_with_underdrain(
    pp_area_ft2: float,
    storage_depth_ft: float,
    underdrain_diameter_ft: float = OU_PIPE_FT,
    underdrain_length_ft: float = LU_UNDERDRAIN,
    porosity: float = PHI_S,
) -> float:
    """
    Calculate storage capacity for PP with underdrain.

    Eq. 103.5: S = A_PP × D_s × φ_S + (π × Ø_u² / 4) × L_u × (1 − φ_S)

    Args:
        pp_area_ft2: Permeable pavement area (ft²)
        storage_depth_ft: Selected aggregate storage depth (ft)
        underdrain_diameter_ft: Underdrain pipe diameter (ft)
        underdrain_length_ft: Underdrain pipe length (ft)
        porosity: Subbase aggregate porosity

    Returns:
        storage (ft³): Total storage capacity
    """
    aggregate_storage = pp_area_ft2 * storage_depth_ft * porosity
    pipe_volume = (math.pi * (underdrain_diameter_ft ** 2) / 4.0) * underdrain_length_ft
    pipe_storage = pipe_volume * (1.0 - porosity)
    return aggregate_storage + pipe_storage


def calculate_orifice_diameter(
    pp_area_ft2: float,
    head_ft: float,
    design_time_sec: float = TD_DESIGN,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> float:
    """
    Calculate slow-release orifice diameter.

    Eq. 103.7: D_o = sqrt(8 × A_PP / (π × C_d × t_d)) × (h_actual / (2g))^0.25

    h_actual = D_s × φ_S (effective head from aggregate storage)

    Args:
        pp_area_ft2: Permeable pavement area (ft²)
        head_ft: Effective head = storage depth × porosity (ft)
        design_time_sec: Design detention time (seconds) — default 48 hrs
        discharge_coeff: Discharge coefficient — 0.61 for sharp-edge
        gravity: Gravitational constant (ft/s²)

    Returns:
        diameter (ft): Orifice diameter in feet
    """
    term1 = math.sqrt(8.0 * pp_area_ft2 / (math.pi * discharge_coeff * design_time_sec))
    term2 = (head_ft / (2.0 * gravity)) ** 0.25
    return term1 * term2


def verify_orifice_detention(
    pp_area_ft2: float,
    orifice_diameter_ft: float,
    head_ft: float,
    discharge_coeff: float = CD_ORIFICE,
    gravity: float = G_GRAVITY,
) -> float:
    """
    Verify actual detention time with rounded orifice diameter.

    Eq. 103.8: t_d = (8 × A_PP) / (π × C_d × D_o²) × sqrt(h / (2g))

    Result should fall between 42–48 hours.

    Args:
        pp_area_ft2: Permeable pavement area (ft²)
        orifice_diameter_ft: Rounded orifice diameter (ft)
        head_ft: Effective head (ft)
        discharge_coeff: Discharge coefficient
        gravity: Gravitational constant (ft/s²)

    Returns:
        time_sec (seconds): Actual detention time
    """
    time_sec = (
        (8.0 * pp_area_ft2)
        / (math.pi * discharge_coeff * orifice_diameter_ft ** 2)
        * math.sqrt(head_ft / (2.0 * gravity))
    )
    return time_sec


# ============================================================================
# PDF REPORT GENERATION
# ============================================================================

def generate_pdf_report(inputs: dict, results: dict) -> bytes:
    """Generate a compact 1-page PDF summary of the PP design."""
    buf = io.BytesIO()
    styles = getSampleStyleSheet()

    MARGIN = 0.5 * inch
    W = 7.5 * inch

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )

    # ── colour palette ───────────────────────────────────────────────────────
    NAVY   = colors.HexColor("#1A3A5C")
    LBLUE  = colors.HexColor("#D6E4F0")
    DBLUE  = colors.HexColor("#2874A6")
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
        "Permeable Pavement (PP) Design Report",
        size=14, bold=True, color=colors.white,
    )
    sub_para = _p(
        f"City of Tulsa LID Manual (2026) — Chapter 103 · Design Process      "
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
        ("PP Placement",             inp["placement"]),
        ("PP Surface Type",          inp["pp_type"]),
        ("Total Drainage Area",      f"{inp['total_area']:,.0f} ft²"),
        ("Impervious Area",          f"{inp['impervious_area']:,.0f} ft²"),
        ("Native Soil Type",         inp["soil_type"]),
        ("Native Infiltration Rate", f"{inp['infiltration_rate']:.3f} in/hr"),
        ("Underdrain Used",          "Yes" if inp["use_underdrain"] else "No"),
        ("Subbase Porosity (φ_S)",   f"{inp['porosity']:.2f}"),
        ("Storage Depth (D_s)",      f"{inp['storage_depth']:.2f} ft"),
        ("PP Area",                  f"{inp['pp_area']:,.0f} ft²"),
        ("Design Precipitation",     f"{inp['precip_depth']:.2f} in"),
        ("Underdrain Diameter",      f"{inp['underdrain_dia_in']:.1f} in" if inp["use_underdrain"] else "N/A"),
    ]))
    story.append(Spacer(1, 8))

    # Section 2: Results
    story.append(_section_header("2   Design Results"))
    story.append(Spacer(1, 2))
    story.append(_kv_table([
        ("SWV Required",             f"{res['swv_required']:.2f} ft³"),
        ("Storage Capacity",         f"{res['storage_capacity']:.2f} ft³  (delta {res['storage_capacity']-res['swv_required']:+.2f} ft³)"),
        ("Total Drawdown (t_dd)",    f"{res['t_dd']:.1f} hrs  (limit 48 hrs)"),
        ("Loading Ratio (LR)",       f"{res['loading_ratio']:.2%}  ({'>=' if res['lr_valid'] else '<'} {TARGET_LOADING_RATIO:.0%} target)"),
        ("",                         ""),
        ("Max Storage Depth",             f"{res['max_storage_depth']:.2f} ft  ({'OK' if inp['storage_depth'] <= res['max_storage_depth'] else 'EXCEEDED'})"),
        ("Contributing Impervious",  f"{res['contributing_impervious']:,.0f} ft²"),
    ]))
    story.append(Spacer(1, 8))

    # Section 3: Orifice (if applicable)
    if res.get("orifice_dia_in") is not None:
        story.append(_section_header("3   Slow-Release Orifice Outlet"))
        story.append(Spacer(1, 2))
        story.append(_kv_table([
            ("Effective Head (D_s × φ_S)", f"{res['head_height']:.3f} ft"),
            ("Calculated Orifice Dia.",    f"{res['orifice_dia_in']:.3f} in"),
            ("Rounded Orifice Dia.",       f"{res['orifice_dia_64ths_num']}/64 in  ({res['orifice_dia_in_rounded']:.4f} in)"),
            ("Detention Time Check",       f"{res['detention_time_hr']:.1f} hrs  (target 42-48 hrs)"),
        ]))
        story.append(Spacer(1, 8))

    # Section 4: Overall Status
    sec_num = "4" if res.get("orifice_dia_in") is not None else "3"
    story.append(_section_header(f"{sec_num}   Overall Design Status"))
    story.append(Spacer(1, 2))

    valid = res["design_valid"]
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
            clean = issue.replace("**", "")
            story.append(_p(f"  - {clean}", size=8, color=DRED))

    # Footer
    story.append(Spacer(1, 10))
    footer_tbl = Table(
        [[_p("Reference: City of Tulsa Engineering Manual (2021), Chapter 103: Permeable Pavements",
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

    st.title("Permeable Pavement (PP) Design Tool")
    st.caption("City of Tulsa LID Manual (2026) — Chapter 103 · Design Process")
    _render_site_selector()

    # ========================================================================
    # SIDEBAR INPUTS
    # ========================================================================
    st.sidebar.header("Design Inputs")

    # PP Placement
    st.sidebar.subheader("PP Placement")
    placement = st.sidebar.radio(
        "PP Location Relative to Contributing Area",
        options=["Placement 1 (Adjacent to Impervious)", "Placement 2 (Replaces Impervious)"],
        index=0,
        help=(
            "**Placement 1:** PP is located adjacent to the impervious area. "
            "The full impervious area drains to the PP. "
            "SWV = A_ip × (1/12) + A_PP × (D_p/12).\n\n"
            "**Placement 2:** PP replaces existing impervious area (retrofit). "
            "SWV = (A_total − A_PP) × (1/12) + A_PP × (D_p/12)."
        ),
    )

    # Determine Contributing Area & SWV
    st.sidebar.subheader("Contributing Drainage Area")

    _c1, _c2 = st.sidebar.columns(2)
    impervious_area = _c1.number_input(
        "Impervious (ft²)",
        min_value=0.0,
        value=8500.0,
        step=100.0,
        help="Total contributing impervious area (roof, pavement, etc.)",
    )
    pervious_area = _c2.number_input(
        "Pervious (ft²)",
        min_value=0.0,
        value=1800.0,
        step=100.0,
        help="Total contributing pervious area (open space).",
    )

    total_area = impervious_area + pervious_area
    st.sidebar.caption(f"Total: **{total_area:,.0f} ft²**")

    # Infiltration Rate
    st.sidebar.subheader("Infiltration Rate")
    soil_options = list(SOIL_INFILTRATION_RATES.keys())
    if st.session_state["pp_soil_type"] not in SOIL_INFILTRATION_RATES:
        st.session_state["pp_soil_type"] = DEFAULT_SOIL_TYPE
    soil_type = st.sidebar.selectbox(
        "Native Soil Type",
        options=soil_options,
        key="pp_soil_type",
        help="Select native soil from Table 100.3. Used to determine default infiltration rate.",
    )
    if soil_type != st.session_state.get("pp_prev_soil_type"):
        st.session_state["pp_prev_soil_type"] = soil_type
        st.session_state["pp_native_infiltration"] = float(SOIL_INFILTRATION_RATES[soil_type])

    native_infiltration_default = SOIL_INFILTRATION_RATES[soil_type]
    infiltration_rate = st.sidebar.number_input(
        "Native Soil Infiltration Rate (in/hr)",
        min_value=0.001,
        step=0.1,
        format="%.3f",
        key="pp_native_infiltration",
        help=(
            f"Default from Table 100.3 for **{soil_type}**: {native_infiltration_default} in/hr. "
            "Edit to match site-specific field measurements."
        ),
    )

    # Subbase porosity
    porosity = st.sidebar.number_input(
        "Subbase Aggregate Porosity (φ_S)",
        min_value=0.10,
        max_value=0.60,
        value=PHI_S,
        step=0.01,
        format="%.2f",
        help="Typical porosity for #57 stone = 0.40.",
    )

    # PP Surface Type
    st.sidebar.subheader("PP Surface Type")
    pp_type = st.sidebar.selectbox(
        "Permeable Pavement Type",
        options=list(PP_SURFACE_TYPES.keys()),
        help=(
            "**Pervious Concrete:** min 5.5 in thick, 6 in #57 base.\n"
            "**Porous Asphalt:** min 4 in thick, 2 in #57 choker, 6 in #2 or #3 base.\n"
            "**Permeable Blocks/PICP:** Mfg. Spec., 6 in #57 base.\n"
            "**Grid Pavement:** Mfg. Spec., 6 in #57 base."
        ),
    )
    pp_info = PP_SURFACE_TYPES[pp_type]

    # Underdrain Decision
    st.sidebar.subheader("Underdrain Decision")
    max_storage_depth = calculate_max_storage_depth(infiltration_rate, porosity)

    underdrain_auto_required = max_storage_depth < MIN_BASE_DEPTH

    if underdrain_auto_required:
        st.sidebar.warning(
            f"Max storage depth ({max_storage_depth:.3f} ft = {max_storage_depth*12:.1f} in) "
            f"< minimum base ({MIN_BASE_DEPTH*12:.0f} in). Underdrain required."
        )
        use_underdrain = True
    else:
        use_underdrain = st.sidebar.checkbox(
            "Use underdrain?",
            value=False,
            help=(
                f"Native soil can drain up to {max_storage_depth:.2f} ft depth in 48 hrs. "
                "Add underdrain if soil is unreliable or system is lined."
            ),
        )

    if use_underdrain:
        _ud1, _ud2 = st.sidebar.columns(2)
        underdrain_dia_in = _ud1.number_input(
            "Underdrain Dia. (in)",
            min_value=3.0,
            value=float(OU_PIPE_IN),
            step=1.0,
            help="Minimum 3-inch diameter underdrain pipe (Section 103.5.6).",
        )
        underdrain_length_ft = _ud2.number_input(
            "Underdrain Length (ft)",
            min_value=5.0,
            value=float(LU_UNDERDRAIN),
            step=5.0,
            help="Total length of underdrain pipe.",
        )
        underdrain_dia_ft = underdrain_dia_in / 12.0
    else:
        underdrain_dia_in = 0.0
        underdrain_length_ft = 0.0
        underdrain_dia_ft = 0.0

    # Design precipitation depth
    st.sidebar.subheader("Precipitation")
    precip_depth = st.sidebar.number_input(
        "Design Precipitation Depth (in)",
        min_value=0.5,
        value=DESIGN_PRECIPITATION,
        step=0.1,
        help="Depth of rain falling on PP surface (typical: 1.2 in for Tulsa).",
    )

    # ========================================================================
    # MAIN AREA: PP AREA AND STORAGE DEPTH SELECTION
    # ========================================================================
    st.divider()
    st.subheader("Select PP Area and Storage Depth")

    col_main1, col_main2 = st.columns(2)
    with col_main1:
        pp_area = st.number_input(
            "Permeable Pavement Area A_PP (ft²)",
            min_value=10.0,
            value=600.0,
            step=50.0,
            help="Footprint area of the permeable pavement. Adjust until storage capacity ≥ SWV.",
        )

    with col_main2:
        depth_max_note = f"max {max_storage_depth:.2f} ft" if not use_underdrain else "no limit (underdrain)"
        storage_depth = st.number_input(
            f"Aggregate Storage Depth D_s (ft)  —  {depth_max_note}",
            min_value=MIN_BASE_DEPTH,
            value=2.0,
            step=0.25,
            format="%.2f",
            help=f"Max depth (no underdrain) = {max_storage_depth:.2f} ft. Min base = 0.5 ft.",
        )

    # ========================================================================
    # CALCULATIONS
    # ========================================================================

    # SWV
    swv_required = calculate_swv(
        impervious_area, pp_area, total_area, placement, precip_depth
    )

    # Contributing impervious for display
    if "Placement 2" in placement:
        contributing_impervious = total_area - pp_area
    else:
        contributing_impervious = impervious_area

    loading_ratio = calculate_loading_ratio(pp_area, contributing_impervious)
    lr_valid = loading_ratio >= TARGET_LOADING_RATIO

    # Storage capacity
    if use_underdrain:
        storage_capacity = calculate_storage_with_underdrain(
            pp_area, storage_depth, underdrain_dia_ft, underdrain_length_ft, porosity
        )
    else:
        storage_capacity = calculate_storage_no_underdrain(pp_area, storage_depth, infiltration_rate, porosity)

    swv_valid = storage_capacity >= swv_required
    depth_valid = use_underdrain or (storage_depth <= max_storage_depth)

    # Drawdown time
    if not use_underdrain:
        # No-underdrain: tdd = SWV (ft³) / Area (ft²) / Is (ft/hr)  [Excel L19]
        if infiltration_rate > 0 and pp_area > 0:
            t_dd = swv_required / pp_area / (infiltration_rate / 12.0)
        else:
            t_dd = float("inf")
    else:
        # Underdrain orifice: tdd = [8·A / (π·Cd·Ou²)] · √(dm / 2g) / 3600  [Excel M19]
        if underdrain_dia_ft > 0 and pp_area > 0 and storage_depth > 0:
            t_dd = (
                (8.0 * pp_area / math.pi / CD_ORIFICE / underdrain_dia_ft ** 2)
                * math.sqrt(storage_depth / (2.0 * G_GRAVITY))
                / 3600.0
            )
        else:
            t_dd = float("inf")
    t_dd_valid = t_dd <= TDD_TOTAL

    design_valid = swv_valid and t_dd_valid and lr_valid

    # ========================================================================
    # RESULTS DISPLAY
    # ========================================================================
    st.divider()
    st.subheader("Results")

    # Summary metrics row
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Drainage Area", f"{total_area:,.0f} ft²")
    m2.metric("PP Area", f"{pp_area:,.0f} ft²")
    m3.metric("Infiltration Rate", f"{infiltration_rate:.3f} in/hr")
    m4.metric("Storage Depth", f"{storage_depth:.2f} ft")
    m5.metric("Underdrain", "Yes" if use_underdrain else "No")

    # SWV / Storage / Drawdown in one row
    n1, n2, n4, n5, n6 = st.columns(5)
    n1.metric(
        "SWV Required (ft³)", f"{swv_required:.1f}",
        help="SWV = contributing impervious area × (1/12) + PP area × (precip/12)",
    )
    n2.metric(
        "Storage Capacity (ft³)", f"{storage_capacity:.1f}",
        delta=f"{storage_capacity - swv_required:+.1f} ft³",
    )
    n4.metric(
        "Total Drawdown", f"{t_dd:.1f} hrs",
        f"limit {TDD_TOTAL:.0f} hrs",
    )
    n5.metric(
        "Contributing Impervious", f"{contributing_impervious:,.0f} ft²",
    )
    n6.metric(
        "Loading Ratio",
        f"{loading_ratio:.2%}",
        delta=f"target {TARGET_LOADING_RATIO:.0%}",
        delta_color="normal" if lr_valid else "inverse",
    )

    # Depth constraint warnings (inline, no extra section)
    if not use_underdrain and storage_depth > max_storage_depth:
        st.warning(
            f"Storage depth {storage_depth:.2f} ft exceeds maximum {max_storage_depth:.2f} ft. "
            "Reduce depth or add underdrain."
        )

    # Validation status row
    v1, v2, v3, v4 = st.columns(4)
    if swv_valid:
        v1.success(f"Storage OK ({storage_capacity:.1f} ≥ {swv_required:.1f} ft³)")
    else:
        shortfall = swv_required - storage_capacity
        required_area_increase = (swv_required / storage_capacity - 1) * 100
        v1.error(
            f"Storage short {shortfall:.1f} ft³ — increase area ~{required_area_increase:.0f}%",
        )

    if depth_valid:
        if use_underdrain:
            v2.success("Underdrain provided — storage depth unrestricted")
        else:
            v2.success(f"Storage depth OK ({storage_depth:.2f} ft ≤ {max_storage_depth:.2f} ft max)")
    else:
        v2.error(
            f"Storage depth {storage_depth:.2f} ft exceeds 48-hr drawdown limit {max_storage_depth:.2f} ft",
        )

    if t_dd_valid:
        v3.success(f"Total drawdown OK ({t_dd:.1f} hrs)")
    else:
        v3.error(f"Total drawdown {t_dd:.1f} hrs > {TDD_TOTAL:.0f}-hr limit")

    if lr_valid:
        v4.success(f"Loading ratio OK ({loading_ratio:.2%})")
    else:
        v4.error(f"Loading ratio {loading_ratio:.2%} < {TARGET_LOADING_RATIO:.0%}")

    # Orifice outlet (if underdrain and design valid)
    orifice_dia_in_rounded = None
    if use_underdrain and design_valid:
        st.divider()
        st.subheader("Slow-Release Orifice Outlet")

        head_height = storage_depth * porosity  # effective head = D_s × φ_S
        orifice_dia_ft = calculate_orifice_diameter(pp_area, head_height)
        orifice_dia_in = orifice_dia_ft * 12.0

        # Round to nearest 1/64 inch
        orifice_dia_64ths_num = math.ceil(orifice_dia_in * 64 + 0.5)
        orifice_dia_in_rounded = orifice_dia_64ths_num / 64.0
        orifice_dia_ft_rounded = orifice_dia_in_rounded / 12.0

        # Verify detention time with rounded orifice
        detention_time_sec = verify_orifice_detention(
            pp_area, orifice_dia_ft_rounded, head_height
        )
        detention_time_hr = detention_time_sec / 3600.0

        col_orif1, col_orif2, col_orif3, col_orif4 = st.columns(4)
        col_orif1.metric(
            "Effective Head (D_s × φ_S)",
            f"{head_height:.3f} ft",
            help="h_actual = storage depth × subbase porosity",
        )
        col_orif2.metric(
            "Calculated Diameter",
            f"{orifice_dia_in:.3f} in",
            help="D_o = sqrt(8·A_PP / (π·C_d·t_d)) × (h/(2g))^0.25",
        )
        col_orif3.metric(
            "Rounded Diameter",
            f"{orifice_dia_64ths_num}/64 in  ({orifice_dia_in_rounded:.4f} in)",
        )
        col_orif4.metric(
            "Detention Time",
            f"{detention_time_hr:.1f} hrs",
            help="t_d = (8·A_PP / (π·C_d·D_o²)) × sqrt(h/(2g))",
        )

        detention_valid = 42.0 <= detention_time_hr <= 48.0
        if detention_valid:
            st.success(
                f"Detention time ({detention_time_hr:.1f} hrs) within 42–48 hr target",
            )
        else:
            st.warning(
                f"Detention time ({detention_time_hr:.1f} hrs) outside 42–48 hr target. "
                "Consider adjusting orifice diameter.",
            )

    # Overall Design Status
    issues = []
    st.divider()
    if design_valid:
        st.success(
            f"**DESIGN VALID** — "
            f"Storage {storage_capacity:.1f} ft³ ≥ {swv_required:.1f} ft³  ·  "
            f"Drawdown {t_dd:.1f} hrs  ·  "
            f"LR {loading_ratio:.2%}",
        )
    else:
        if not swv_valid:
            issues.append(
                f"**Storage short** {swv_required - storage_capacity:.1f} ft³ — "
                f"increase PP area ~{(swv_required / storage_capacity - 1) * 100:.0f}% or increase storage depth"
            )
        if not t_dd_valid:
            issues.append(
                f"**Total drawdown** {t_dd:.1f} hrs > {TDD_TOTAL:.0f}-hr limit — "
                f"add underdrain or reduce storage depth"
            )
        if not lr_valid:
            issues.append(
                f"**Loading ratio** {loading_ratio:.2%} < {TARGET_LOADING_RATIO:.0%} — increase PP area"
            )
        st.error(
            "**DESIGN INVALID**\n\n" + "\n\n".join(f"- {i}" for i in issues),
        )

    # ========================================================================
    # PDF DOWNLOAD
    # ========================================================================
    st.divider()

    # Collect orifice results (if computed)
    orifice_results: dict = {}
    if use_underdrain and design_valid:
        head_height_pdf = storage_depth * porosity
        _odia_ft  = calculate_orifice_diameter(pp_area, head_height_pdf)
        _odia_in  = _odia_ft * 12.0
        _o64      = math.ceil(_odia_in * 64 + 0.5)
        _odia_r   = _o64 / 64.0
        _det_sec  = verify_orifice_detention(pp_area, _odia_r / 12.0, head_height_pdf)
        orifice_results = {
            "head_height":            head_height_pdf,
            "orifice_dia_in":         _odia_in,
            "orifice_dia_64ths_num":  _o64,
            "orifice_dia_in_rounded": _odia_r,
            "detention_time_hr":      _det_sec / 3600.0,
        }

    pdf_inputs = {
        "placement":           placement,
        "pp_type":             pp_type,
        "impervious_area":     impervious_area,
        "pervious_area":       pervious_area,
        "total_area":          total_area,
        "soil_type":           soil_type,
        "infiltration_rate":   infiltration_rate,
        "use_underdrain":      use_underdrain,
        "porosity":            porosity,
        "storage_depth":       storage_depth,
        "pp_area":             pp_area,
        "precip_depth":        precip_depth,
        "underdrain_dia_in":   underdrain_dia_in,
    }

    pdf_results = {
        "swv_required":            swv_required,
        "storage_capacity":        storage_capacity,
        "loading_ratio":           loading_ratio,
        "lr_valid":                lr_valid,
        "max_storage_depth":       max_storage_depth,
        "contributing_impervious": contributing_impervious,
        "t_dd":                    t_dd,
        "design_valid":            design_valid,
        "issues":                  issues if not design_valid else [],
        **orifice_results,
    }

    pdf_bytes = generate_pdf_report(pdf_inputs, pdf_results)
    st.download_button(
        label="Download 1-Page PDF Report",
        data=pdf_bytes,
        file_name="PP_Design_Report.pdf",
        mime="application/pdf",
        use_container_width=True,
    )


if __name__ == "__main__":
    st.set_page_config(
        page_title="Permeable Pavement (PP) Design Tool",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    main()
