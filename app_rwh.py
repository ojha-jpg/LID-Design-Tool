"""
Rainwater Harvesting (RWH) Design Tool
Based on City of Tulsa LID Manual (2026) - Section 104

Design Steps:
1. Calculate Catchment Area
2. Calculate Stormwater Volume (SWV), First Flush Volume, Irrigation Volume, Total Volume
3. Select Tank (verify usable volume ≥ Total Volume)
4. Size Slow-Release Orifice Outlet
5. Size First Flush Diverter Pipe

Reference: City of Tulsa Engineering Manual (2021), Section 104: Rainwater Harvesting
"""

import io
import math
import os
from datetime import date

import pandas as pd
import streamlit as st
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
# CONSTANTS (from Tulsa LID Manual, Section 104)
# ============================================================================

RUNOFF_DEPTH_IN = 1.0          # Capture first 1 inch of runoff (Section 104.2)
CONV_FACTOR = 7.48052          # ft³ → gallons
CONV_IN_TO_FT = 1.0 / 12.0    # inches → feet
FIRST_FLUSH_RATE = 1.0 / 100   # 1 gal per 100 ft² (Eq. 104.5b)
CD_ORIFICE = 0.61              # Discharge coeff, sharp-edge orifice
G_GRAVITY = 32.2               # ft/s²
TD_DESIGN_HR = 48.0            # Design detention time (hours)
TD_MIN_HR = 42.0               # Min acceptable detention time
TD_MAX_HR = 54.0               # Max acceptable detention time
TD_DESIGN_SEC = TD_DESIGN_HR * 3600.0
H_OFFSET_DEFAULT_IN = 5.0      # Recommended orifice offset from bottom (inches)
H_OFFSET_MIN_IN = 4.0          # Minimum orifice offset (inches)

# First flush pipe capacity (inches of pipe per gallon) — Table 104.6
FIRST_FLUSH_PIPE = {
    '3"  Schedule 40 PVC': 32.8,
    '4"  Schedule 40 PVC': 18.5,
    '6"  Schedule 40 PVC': 8.25,
    '8"  Schedule 40 PVC': 4.63,
}

# Roof type catchment efficiencies (Table 104.3)
ROOF_EFFICIENCIES = {
    "Asphalt Shingle": 0.80,
    "Metal Roofing": 0.87,
    "Clay Tile": 0.68,
}


# ============================================================================
# DESIGN CALCULATIONS
# ============================================================================

def calc_catchment_area(length_ft: float, width_ft: float) -> float:
    """Eq. 104.1: Ac = L × W (vertical projection of roof, ft²)."""
    return length_ft * width_ft


def calc_swv_gallons(ac_ft2: float) -> float:
    """
    Eq. 104.2: VolSW = (1 in) × Ac × conv.
    Uses 7.48 gal/ft³: VolSW = Ac × (1/12) × 7.48052
    """
    return ac_ft2 * CONV_IN_TO_FT * CONV_FACTOR * RUNOFF_DEPTH_IN


def calc_first_flush_gal(ac_ft2: float) -> float:
    """Eq. 104.5b: Volff = (1 gal / 100 ft²) × Ac"""
    return ac_ft2 * FIRST_FLUSH_RATE


def calc_irrigation_gal(i_deficit_in: float, a_irr_ft2: float) -> float:
    """Eq. 104.5: Volother = 0.62 × Ideficit × Airr"""
    return 0.62 * i_deficit_in * a_irr_ft2


def calc_total_volume(vol_sw: float, vol_ff: float, vol_other: float) -> float:
    """Formula 5: Voltotal = VolSW − Volff + Volother"""
    return vol_sw - vol_ff + vol_other


def calc_tank_area_ft2(tank_dia_in: float) -> float:
    """Horizontal cross-sectional area of cylindrical tank (ft²)."""
    r_ft = (tank_dia_in / 2.0) / 12.0
    return math.pi * r_ft ** 2


def calc_storage_height_in(volume_gal: float, atank_ft2: float) -> float:
    """Required water depth (inches) to store a target volume in a cylindrical tank."""
    if atank_ft2 <= 0:
        return 0.0
    return (volume_gal / CONV_FACTOR) / atank_ft2 * 12.0


def calc_h_actual(h_tank_in: float, h_offset_in: float) -> float:
    """Formula 6: hactual = htank − hoffset (inches)."""
    return h_tank_in - h_offset_in


def calc_usable_volume_gal(atank_ft2: float, h_actual_in: float) -> float:
    """Usable storage volume above orifice (gallons)."""
    return atank_ft2 * (h_actual_in / 12.0) * CONV_FACTOR


def calc_orifice_diameter_in(atank_ft2: float, h_actual_in: float) -> float:
    """
    Eq. 104.3: Do = sqrt[ (8 × Atank) / (π × Cd × td)] × pow(hactual / 2g, 0.25)

    All units in ft and seconds; result converted to inches.
    """
    h_ft = h_actual_in / 12.0
    term1 = (8.0 * atank_ft2) / (math.pi * CD_ORIFICE * TD_DESIGN_SEC)
    term2 = math.pow(h_ft / (2.0 * G_GRAVITY), 0.25)
    do_ft = math.sqrt(term1) * term2
    return do_ft * 12.0


def round_to_64ths(diameter_in: float) -> tuple[float, int]:
    """
    Round diameter to the nearest 1/64 inch (per Table 104.7).
    Returns (rounded_diameter_in, numerator_over_64).
    Minimum = 1/8 inch = 8/64.
    """
    sixty_fourths = round(diameter_in * 64.0)
    sixty_fourths = max(sixty_fourths, 8)  # minimum 1/8 inch
    return sixty_fourths / 64.0, sixty_fourths


def calc_detention_time_hr(atank_ft2: float, do_in: float, h_actual_in: float) -> float:
    """
    Eq. 104.4: td = (8 × Atank) / (π × Cd × Do²) × sqrt(hactual / 2g)
    Returns detention time in hours.
    """
    do_ft = do_in / 12.0
    h_ft = h_actual_in / 12.0
    td_sec = (
        (8.0 * atank_ft2)
        / (math.pi * CD_ORIFICE * do_ft ** 2)
        * math.sqrt(h_ft / (2.0 * G_GRAVITY))
    )
    return td_sec / 3600.0



# ============================================================================
# TANK DATABASE (tanks_rwh.csv)
# ============================================================================

@st.cache_data
def load_tanks_df() -> pd.DataFrame:
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tanks_rwh.csv")
    df = pd.read_csv(csv_path)
    df["capacity_gal_num"] = pd.to_numeric(df["capacity_gal"], errors="coerce")
    df["diameter_in_num"]  = pd.to_numeric(df["diameter_in"],  errors="coerce")
    df["height_in_num"]    = pd.to_numeric(df["height_in"],    errors="coerce")
    df["price_str"]        = df["price"].fillna("N/A") if "price" in df.columns else "N/A"
    return df


def select_tank(vol_total_gal: float, df: pd.DataFrame):
    """Return the smallest circular tank with capacity >= vol_total_gal.

    Falls back to the largest circular tank if none is large enough.
    Returns a pandas Series or None.
    """
    circular = df[df["diameter_in_num"].notna() & df["height_in_num"].notna()].copy()
    if circular.empty:
        return None
    adequate = circular[circular["capacity_gal_num"] >= vol_total_gal]
    if adequate.empty:
        return circular.loc[circular["capacity_gal_num"].idxmax()]
    return adequate.loc[adequate["capacity_gal_num"].idxmin()]


# ============================================================================
# PDF REPORT GENERATION
# ============================================================================

def generate_pdf_report(inputs: dict, results: dict) -> bytes:
    """Generate a compact PDF summary of the RWH design."""
    buf = io.BytesIO()
    styles = getSampleStyleSheet()

    MARGIN = 0.5 * inch
    W = 7.5 * inch

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )

    NAVY   = colors.HexColor("#1A3A5C")
    LBLUE  = colors.HexColor("#D6E4F0")
    DBLUE  = colors.HexColor("#2874A6")
    GREEN  = colors.HexColor("#D5F5E3")
    DGREEN = colors.HexColor("#1E8449")
    RED    = colors.HexColor("#FADBD8")
    DRED   = colors.HexColor("#C0392B")
    LGREY  = colors.HexColor("#F2F3F4")
    MGREY  = colors.lightgrey

    def _p(txt, size=8.0, bold=False, color=colors.black, leading=None):
        s = styles["Normal"].clone(f"s{size}{bold}")
        s.fontSize = size
        s.leading = leading or (size + 2)
        s.textColor = color
        if bold:
            txt = f"<b>{txt}</b>"
        return Paragraph(txt, s)

    def _kv_table(rows):
        CW = (2.15 * inch, 1.6 * inch, 2.15 * inch, 1.6 * inch)
        data = []
        for i in range(0, len(rows), 2):
            left  = rows[i]
            right = rows[i + 1] if i + 1 < len(rows) else ("", "")
            data.append([
                _p(left[0],  bold=True), _p(left[1]),
                _p(right[0], bold=True), _p(right[1]),
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
            ("BACKGROUND",    (0, 0), (0, n - 1), LGREY),
            ("BACKGROUND",    (2, 0), (2, n - 1), LGREY),
        ]
        for r in range(n):
            bg = colors.white if r % 2 == 0 else colors.HexColor("#F8FBFD")
            style_cmds.append(("BACKGROUND", (1, r), (1, r), bg))
            style_cmds.append(("BACKGROUND", (3, r), (3, r), bg))
        t.setStyle(TableStyle(style_cmds))
        return t

    def _section_header(txt):
        header_para = _p(txt, size=9, bold=True, color=colors.white)
        t = Table([[header_para]], colWidths=[W])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), DBLUE),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    # Title banner
    title_para = _p(
        "Rainwater Harvesting (RWH) Design Report",
        size=14, bold=True, color=colors.white,
    )
    sub_para = _p(
        f"City of Tulsa LID Manual (2026) — Section 104 · Design Process      "
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

    inp = inputs
    res = results
    story = [banner, Spacer(1, 8)]

    # Section 1: Catchment & Volume
    story.append(_section_header("1   Catchment Area & Volume Calculations"))
    story.append(Spacer(1, 2))
    story.append(_kv_table([
        ("Catchment Area",          f"{inp['ac_ft2']:,.1f} ft²"),
        ("Stormwater Vol. (VolSW)", f"{res['vol_sw']:.1f} gal"),
        ("First Flush Vol. (Volff)", f"{res['vol_ff']:.1f} gal"),
        ("Include Irrigation",      "Yes" if inp["use_irrigation"] else "No"),
        ("Irrigation Deficit (July)", f"{inp['i_deficit_in']:.2f} in" if inp["use_irrigation"] else "N/A"),
        ("Irrigated Area (Airr)",   f"{inp['a_irr_ft2']:,.0f} ft²" if inp["use_irrigation"] else "N/A"),
        ("Irrigation Vol. (Volother)", f"{res['vol_other']:.1f} gal" if inp["use_irrigation"] else "N/A"),
        ("Total Storage Required (Voltotal)", f"{res['vol_total']:.1f} gal"),
        ("", ""),
    ]))
    story.append(Spacer(1, 8))

    # Section 2: Tank
    story.append(_section_header("2   Tank Selection & Verification"))
    story.append(Spacer(1, 2))
    story.append(_kv_table([
        ("Tank Capacity",       f"{inp['tank_gal']:,.0f} gal"),
        ("Tank Price",          inp.get("tank_price_str", "N/A")),
        ("Tank Diameter",       f"{inp['tank_dia_in']:.1f} in"),
        ("Tank Height (htank)", f"{inp['tank_h_in']:.1f} in"),
        ("Height for Stormwater Storage", f"{res['h_store_sw_in']:.2f} in"),
        ("Orifice Height from Bottom", f"{inp['h_offset_in']:.2f} in"),
        ("Usable Height (hactual)", f"{res['h_actual_in']:.2f} in"),
        ("Tank Cross-Section Area", f"{res['atank_ft2']:.3f} ft²"),
        ("Usable Volume",       f"{res['usable_vol_gal']:.1f} gal"),
        ("Volume Check",        "OK" if res['volume_ok'] else "FAIL — stormwater depth exceeds tank height"),
    ]))
    story.append(Spacer(1, 8))

    # Section 3: Orifice
    story.append(_section_header("3   Slow-Release Orifice Outlet  (Eq. 104.3 & 104.4)"))
    story.append(Spacer(1, 2))
    do_64 = res['do_64ths']
    story.append(_kv_table([
        ("Calculated Do",       f"{res['do_calc_in']:.4f} in"),
        ("Rounded Do",          f"{do_64}/64 in  ({res['do_rounded_in']:.4f} in)"),
        ("Actual Detention Time", f"{res['td_hr']:.1f} hrs"),
        ("Detention Check",     f"{'OK  (42–54 hrs)' if res['td_ok'] else 'FAIL — outside 42–54 hrs'}"),
    ]))
    story.append(Spacer(1, 8))

    # Section 4: First Flush Pipe
    story.append(_section_header("4   First Flush Diverter Pipe  (Eq. 104.6)"))
    story.append(Spacer(1, 2))
    story.append(_kv_table([
        ("First Flush Volume",  f"{res['vol_ff']:.1f} gal"),
        ("Pipe Size Selected",  inp["ff_pipe_size"]),
        ("", ""),
    ]))
    story.append(Spacer(1, 8))

    # Section 5: Overall Status
    story.append(_section_header("5   Overall Design Status"))
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

    issues = res.get("issues", [])
    if issues:
        story.append(Spacer(1, 4))
        for issue in issues:
            story.append(_p(f"  - {issue.replace('**', '')}", size=8, color=DRED))

    # Footer
    story.append(Spacer(1, 10))
    footer_tbl = Table(
        [[_p("Reference: City of Tulsa Engineering Manual (2021), Section 104: Rainwater Harvesting",
             size=7, color=colors.HexColor("#7F8C8D"))]],
        colWidths=[W],
    )
    footer_tbl.setStyle(TableStyle([
        ("LINEABOVE",   (0, 0), (-1, 0), 0.5, MGREY),
        ("TOPPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_tbl)

    doc.build(story)
    return buf.getvalue()


# ============================================================================
# STREAMLIT UI
# ============================================================================

def main() -> None:
    st.title("Rainwater Harvesting (RWH) Design Tool")
    st.caption("City of Tulsa LID Manual (2026) — Section 104 · Design Process")

    # ========================================================================
    # SIDEBAR
    # ========================================================================
    st.sidebar.header("Design Inputs")

    # Step 1: Catchment Area
    st.sidebar.subheader("Step 1 — Catchment Area")
    ac_ft2 = st.sidebar.number_input(
        "Catchment Area (ft²)",
        min_value=1.0, value=1400.0, step=50.0,
        help="Vertical projection of the roof or impervious collection surface (square feet).",
    )

    # Step 2: Volumes
    st.sidebar.subheader("Step 2 — Volume Design")

    use_irrigation = st.sidebar.checkbox(
        "Include irrigation / other use volume?",
        value=False,
        help="Check if the tank will also serve irrigation or other non-stormwater uses.",
    )

    if use_irrigation:
        i_deficit_in = st.sidebar.number_input(
            "July Irrigation Deficit — Ideficit (inches)",
            min_value=0.0, value=1.01, step=0.01, format="%.2f",
            help=(
                "Precipitation minus ET deficit for the design month (typically July in Oklahoma). "
                "Mean value: 1.01 in. Conservative (P₂₅–ET₇₅): higher."
            ),
        )
        a_irr_ft2 = st.sidebar.number_input(
            "Irrigated Area — Airr (ft²)",
            min_value=0.0, value=200.0, step=50.0,
            help="Area to be irrigated using harvested rainwater.",
        )
    else:
        i_deficit_in = 0.0
        a_irr_ft2    = 0.0

    # Step 3: Tank Selection (auto-sized from tanks_rwh.csv)
    st.sidebar.subheader("Step 3 — Tank Selection")

    # Pre-compute required volume so we can auto-select from the database
    _vol_sw    = calc_swv_gallons(ac_ft2)
    _vol_ff    = calc_first_flush_gal(ac_ft2)
    _vol_other = calc_irrigation_gal(i_deficit_in, a_irr_ft2) if use_irrigation else 0.0
    _vol_total = calc_total_volume(_vol_sw, _vol_ff, _vol_other)

    tanks_df = load_tanks_df()
    rec = select_tank(_vol_total, tanks_df)

    # Update pre-filled values whenever required volume changes
    if st.session_state.get("_auto_vol_total") != _vol_total:
        if rec is not None:
            st.session_state["tank_gal"]    = float(rec["capacity_gal_num"])
            st.session_state["tank_dia_in"] = float(rec["diameter_in_num"])
            st.session_state["tank_h_in"]   = float(rec["height_in_num"])
        st.session_state["_auto_vol_total"] = _vol_total

    tank_price_str = "N/A"
    if rec is not None:
        tank_url = rec.get("url", "") if hasattr(rec, "get") else rec["url"] if "url" in rec.index else ""
        link_md = f"\n\n[View product page]({tank_url})" if tank_url else ""
        tank_price_str = rec.get("price_str", "N/A") if hasattr(rec, "get") else rec["price_str"] if "price_str" in rec.index else "N/A"
        st.sidebar.info(
            f"**Auto-selected:** {rec['name']}\n\n"
            f"Capacity: **{rec['capacity_gal_num']:.0f} gal** · "
            f"Dia: **{rec['diameter_in_num']:.0f} in** · "
            f"Height: **{rec['height_in_num']:.0f} in**\n\n"
            f"Price: **{tank_price_str}**\n\n"
            f"Values pre-filled below — edit if needed.{link_md}"
        )
    else:
        st.sidebar.warning("No suitable tank found in database. Enter dimensions manually.")

    tank_gal = st.sidebar.number_input(
        "Tank Capacity (gal)",
        min_value=50.0, value=1000.0, step=100.0,
        key="tank_gal",
        help="Nominal capacity of selected tank (for reference).",
    )
    tank_dia_in = st.sidebar.number_input(
        "Tank Diameter (inches)",
        min_value=12.0, value=60.0, step=1.0,
        key="tank_dia_in",
        help="Inside diameter of the cylindrical tank (measured in inches).",
    )
    tank_h_in = st.sidebar.number_input(
        "Tank Height to Dome — htank (inches)",
        min_value=12.0, value=84.0, step=1.0,
        key="tank_h_in",
        help=(
            "Height from bottom to the lower portion of the top dome "
            "(excludes the dome and inlet/overflow zone)."
        ),
    )

    # Step 5: First Flush Pipe
    st.sidebar.subheader("Step 5 — First Flush Diverter Pipe")
    ff_pipe_size = st.sidebar.selectbox(
        "PVC Pipe Size (Schedule 40)",
        options=list(FIRST_FLUSH_PIPE.keys()),
        index=2,
        help=(
            "Chamber length per gallon: 3\" = 32.8 in/gal, 4\" = 18.5 in/gal, "
            "6\" = 8.25 in/gal, 8\" = 4.63 in/gal."
        ),
    )

    # ========================================================================
    # CALCULATIONS  (volumes already computed above for tank auto-selection)
    # ========================================================================
    vol_sw    = _vol_sw
    vol_ff    = _vol_ff
    vol_other = _vol_other
    vol_total = _vol_total

    atank_ft2    = calc_tank_area_ft2(tank_dia_in)
    h_store_sw_in = calc_storage_height_in(vol_sw, atank_ft2)
    h_offset_in = tank_h_in - h_store_sw_in
    h_actual_in  = calc_h_actual(tank_h_in, h_offset_in)
    usable_vol   = calc_usable_volume_gal(atank_ft2, h_actual_in)
    volume_ok    = h_store_sw_in <= tank_h_in

    do_calc_in             = calc_orifice_diameter_in(atank_ft2, h_actual_in)
    do_rounded_in, do_64   = round_to_64ths(do_calc_in)
    td_hr                  = calc_detention_time_hr(atank_ft2, do_rounded_in, h_actual_in)
    td_ok                  = TD_MIN_HR <= td_hr <= TD_MAX_HR


    # Validation
    issues: list[str] = []
    if not volume_ok:
        issues.append(
            f"Stormwater storage height required ({h_store_sw_in:.2f} in) exceeds "
            f"tank height ({tank_h_in:.2f} in). Select a larger-diameter or taller tank."
        )
    if h_actual_in <= 0:
        issues.append("Computed usable head is non-positive; check tank dimensions and runoff volume.")
    if not td_ok:
        if td_hr < TD_MIN_HR:
            issues.append(
                f"Detention time ({td_hr:.1f} hrs) < {TD_MIN_HR} hrs. "
                "Use a shorter/wider tank or reduce orifice size."
            )
        else:
            issues.append(
                f"Detention time ({td_hr:.1f} hrs) > {TD_MAX_HR} hrs. "
                "Use a taller/narrower tank or increase orifice size."
            )
    if do_64 <= 8 and do_calc_in < (8 / 64):
        issues.append(
            "Calculated orifice < 1/8 inch (minimum). Redesign with shorter/wider tank."
        )

    design_valid = volume_ok and td_ok and h_actual_in > 0

    results = {
        "vol_sw": vol_sw, "vol_ff": vol_ff, "vol_other": vol_other,
        "vol_total": vol_total, "atank_ft2": atank_ft2,
        "h_store_sw_in": h_store_sw_in,
        "h_actual_in": h_actual_in, "usable_vol_gal": usable_vol,
        "volume_ok": volume_ok, "do_calc_in": do_calc_in,
        "do_rounded_in": do_rounded_in, "do_64ths": do_64,
        "td_hr": td_hr, "td_ok": td_ok,
        "design_valid": design_valid, "issues": issues,
    }
    inputs_dict = {
        "ac_ft2": ac_ft2,
        "use_irrigation": use_irrigation,
        "i_deficit_in": i_deficit_in, "a_irr_ft2": a_irr_ft2,
        "tank_gal": tank_gal, "tank_dia_in": tank_dia_in,
        "tank_h_in": tank_h_in, "h_offset_in": h_offset_in,
        "ff_pipe_size": ff_pipe_size,
        "tank_price_str": tank_price_str,
    }

    st.sidebar.info(
        f"Computed height to store SWV: **{h_store_sw_in:.2f} in**\n\n"
        f"Orifice height from bottom: **{h_offset_in:.2f} in**\n\n"
        f"Formula used: **htank - h_storage**"
    )

    # ========================================================================
    # MAIN AREA: RESULTS
    # ========================================================================
    st.divider()
    st.subheader("Step 2 — Volume Summary")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Catchment Area", f"{ac_ft2:,.0f} ft²")
    col2.metric("VolSW (SWV)", f"{vol_sw:.1f} gal",
                help="Eq. 104.2: Ac × (1/12) × 7.48")
    col3.metric("First Flush (Volff)", f"{vol_ff:.1f} gal",
                help="Eq. 104.5b: Ac / 100")
    col4.metric("Irrigation Vol.", f"{vol_other:.1f} gal" if use_irrigation else "N/A (disabled)",
                help="Eq. 104.5: 0.62 × Ideficit × Airr")

    st.markdown("")
    c1, c2, c3 = st.columns([1, 1, 2])
    delta_color = "inverse" if vol_total <= tank_gal else "normal"
    c1.metric(
        "Total Volume Required (Voltotal)",
        f"{vol_total:.1f} gal",
        delta=f"{vol_total - tank_gal:+.1f} gal vs nominal tank",
        delta_color=delta_color,
        help="Formula 5: VolSW − Volff + Volother",
    )
    c2.metric(
        "Tank Nominal Capacity",
        f"{tank_gal:,.0f} gal",
    )
    with c3:
        pass

    st.divider()
    st.subheader("Step 3 — Tank Verification")

    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Tank Area (Atank)", f"{atank_ft2:.3f} ft²",
              help=f"π × (D/2)² = π × ({tank_dia_in/2:.1f} in / 12)²")
    t2.metric("Height to Store Stormwater", f"{h_store_sw_in:.2f} in",
              help="Computed from SWV and tank area")
    t3.metric(
        "Orifice Height from Bottom",
        f"{h_offset_in:.2f} in",
        delta=f"{tank_h_in - h_store_sw_in:+.2f} in = htank - storage height",
        delta_color="normal" if volume_ok else "inverse",
    )
    t4.metric("Storage Height Check", "OK" if volume_ok else "FAIL")

    st.divider()
    st.subheader("Step 4 — Slow-Release Orifice Outlet")

    o1, o2, o3, o4 = st.columns(4)
    o1.metric("Calculated Do", f"{do_calc_in:.4f} in",
              help="Eq. 104.3: Do = √[(8×Atank)/(π×Cd×td) × √(hactual/2g)]")
    o2.metric("Rounded Do", f"{do_64}/64 in  ({do_rounded_in:.4f} in)",
              help="Rounded to nearest 1/64 inch per Table 104.7. Min = 1/8 in.")
    o3.metric(
        "Actual Detention Time",
        f"{td_hr:.1f} hrs",
        delta=f"target 42–54 hrs",
        delta_color="normal" if td_ok else "inverse",
        help="Eq. 104.4: td = (8×Atank)/(π×Cd×Do²) × √(hactual/2g)",
    )
    o4.metric("Detention Check", "OK (42–54 hrs)" if td_ok else "FAIL")

    st.divider()
    st.subheader("Step 5 — First Flush Diverter Pipe")

    f1, f2 = st.columns(2)
    f1.metric("First Flush Volume", f"{vol_ff:.1f} gal")
    f2.metric("Pipe Size", ff_pipe_size.strip())

    # ========================================================================
    # DESIGN STATUS BANNER
    # ========================================================================
    st.divider()
    if design_valid:
        st.success("### PASS — Design Valid")
    else:
        st.error("### FAIL — Design Invalid")
        for issue in issues:
            st.warning(issue)


    # ========================================================================
    # PDF EXPORT
    # ========================================================================
    st.divider()
    if st.button("Generate PDF Report", type="primary"):
        pdf_bytes = generate_pdf_report(inputs_dict, results)
        st.download_button(
            label="Download PDF Report",
            data=pdf_bytes,
            file_name=f"RWH_Design_{date.today().strftime('%Y%m%d')}.pdf",
            mime="application/pdf",
        )


if __name__ == "__main__":
    st.set_page_config(
        page_title="Rainwater Harvesting (RWH) Design Tool",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    main()
