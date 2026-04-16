"""
app.py — LID Peak Runoff Tool (Main Streamlit App)

5-step workflow:
  1. Select a point on the Oklahoma stream network
  2. Delineate the watershed (USGS StreamStats)
  3. Collect data: Atlas 14 precipitation, SSURGO soils, NLCD land use
  4. Calculate composite CN, C, and peak flows (CN method + Rational method)
  5. Display results table; download as CSV
"""

import os
import json
import base64
import io
import time

# PROJ environment — must be set before any geospatial imports
os.environ["PROJ_DATA"] = "/opt/anaconda3/envs/dashboard/share/proj"

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import streamlit as st
import folium
from folium.plugins import MousePosition
from streamlit_folium import st_folium
import rasterio
from rasterio.windows import from_bounds
from rasterio.transform import rowcol
from PIL import Image

from reference_data import RETURN_PERIODS, LANDUSE_TYPES
from hydrology import (
    composite_cn,
    composite_cn_from_intersection,
    composite_c,
    cn_peak_flow,
    build_storm_table,
    rational_peak_flow,
    sqmi_to_acres,
    tlag_to_tc,
    tc_scs_lag,
)
from api_clients import (
    delineate_watershed,
    get_basin_characteristics,
    get_peak_flow_regression,
    fetch_atlas14,
    fetch_soil_composition,
    fetch_soil_texture,
    fetch_landuse_composition,
    fetch_landuse_soil_intersection,
    intersection_to_soil_pct,
    intersection_to_landuse_pct,
    fetch_soil_geodataframe,
    fetch_nlcd_array,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RASTER_PATH = "/Users/ashishojha/Documents/LID excels/ok_streamgrid/streamgrid.tif"
STREAM_THRESHOLD = 1000   # minimum accumulation value to consider a cell a stream
OKLAHOMA_CENTER = [35.5, -97.5]
DEFAULT_ZOOM = 7

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="LID Peak Runoff Tool",
    page_icon=":droplet:",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session state initialization
# ---------------------------------------------------------------------------

def _init_state():
    defaults = {
        "step": 1,
        "selected_lat": None,
        "selected_lon": None,
        "point_on_stream": False,
        "watershed": None,        # dict from delineate_watershed()
        "basin_chars": None,      # dict from get_basin_characteristics()
        "atlas14": None,          # dict from fetch_atlas14()
        "soil_pct": None,              # dict {"A": %, "B": %, ...}
        "soil_texture": None,          # dict {"Silt loam": %, ...}
        "soil_texture_live": None,
        "landuse_pct": None,           # dict {"Pasture/Meadow": %, ...}
        "lu_soil_intersection": None,  # dict {(lu_key, hsg): %} — spatial overlap
        "intersection_live": None,
        "soil_gdf": None,              # GeoDataFrame — clipped SSURGO with HSG + texture
        "nlcd_arr": None,              # np.ndarray — clipped NLCD pixel codes
        "usgs_flows": None,       # list from get_peak_flow_regression()
        "tc_hr": 0.5,             # time of concentration (hours) — set from StreamStats TLAG or default 30 min
        "map1_center": OKLAHOMA_CENTER,
        "map1_zoom": DEFAULT_ZOOM,
        "storm_duration_hr": 24,  # CN method design storm duration (hours)
        "lag_L_ft":   None,       # SCS lag: flow length (ft)
        "lag_Y_pct":  None,       # SCS lag: average slope (%)
        "use_lag_tc": False,      # whether to override Tc with SCS lag formula
        "atlas14_live": None,     # True if Atlas 14 returned live data, False if fallback
        "soil_live": None,        # True if SSURGO returned live data, False if fallback
        "landuse_live": None,     # True if NLCD returned live data, False if fallback
        "results_df": None,       # pd.DataFrame of final results
        "error": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ---------------------------------------------------------------------------
# Raster utilities
# ---------------------------------------------------------------------------

@st.cache_resource
def _open_raster():
    return rasterio.open(RASTER_PATH)


def _is_on_stream(lat: float, lon: float, neighborhood: int = 5) -> bool:
    """Check if (lat, lon) falls on or near a stream pixel."""
    try:
        src = _open_raster()
        row, col = rowcol(src.transform, lon, lat)
        r0 = max(0, row - neighborhood)
        r1 = min(src.height, row + neighborhood + 1)
        c0 = max(0, col - neighborhood)
        c1 = min(src.width, col + neighborhood + 1)
        window = rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)
        data = src.read(1, window=window)
        return bool(np.any(data >= STREAM_THRESHOLD))
    except Exception:
        return True  # allow if raster check fails


def _raster_overlay_image(bounds: list, width: int = 800) -> str:
    """
    Render stream raster for given bounds as base64 PNG for Folium overlay.
    bounds: [south, west, north, east]
    """
    try:
        src = _open_raster()
        south, west, north, east = bounds
        win = from_bounds(west, south, east, north, src.transform)
        data = src.read(1, window=win, out_shape=(int(width * (north - south) / (east - west)), width))
        # Normalize and colorize (blue streams on transparent background)
        arr = data.astype(float)
        mask = arr >= STREAM_THRESHOLD
        rgba = np.zeros((*arr.shape, 4), dtype=np.uint8)
        rgba[mask] = [30, 120, 200, 180]  # blue, semi-transparent
        img = Image.fromarray(rgba, "RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Land use × soil CN breakdown table
# ---------------------------------------------------------------------------

def _build_landuse_cn_table(
    intersection: dict,
    soil_pct: dict,
    landuse_pct: dict,
    area_sqmi: float,
) -> pd.DataFrame:
    """
    Build a per-(land use, HSG) breakdown table showing area, CN, and C.

    When the spatial intersection is available each row is an exact
    (land use, HSG) pixel-level combination. When only marginal percentages
    are available a single row per land use is returned with a soil-weighted
    composite CN and the contributing HSG groups listed.

    Returns a DataFrame with columns:
      Land Use | Soil HSG | Area (acres) | CN | C
    """
    area_acres_total = sqmi_to_acres(area_sqmi)
    rows = []

    if intersection:
        for (lu_key, hsg), pct in intersection.items():
            lu_data = LANDUSE_TYPES.get(lu_key)
            if lu_data is None:
                continue
            rows.append({
                "Land Use":     lu_key,
                "Soil HSG":     hsg,
                "Area (acres)": round(pct / 100.0 * area_acres_total, 2),
                "CN":           lu_data[f"cn_{hsg.lower()}"],
                "C":            lu_data["c_coeff"],
            })
    else:
        # No intersection — one row per land use, CN weighted across soil groups
        for lu_key, lu_pct in landuse_pct.items():
            lu_data = LANDUSE_TYPES.get(lu_key)
            if lu_data is None:
                continue
            cn_weighted = round(
                sum(lu_data[f"cn_{g.lower()}"] * (sp / 100.0) for g, sp in soil_pct.items()), 1
            )
            hsg_label = ", ".join(
                f"{g} ({sp:.0f}%)" for g, sp in sorted(soil_pct.items()) if sp > 0
            )
            rows.append({
                "Land Use":     lu_key,
                "Soil HSG":     hsg_label,
                "Area (acres)": round(lu_pct / 100.0 * area_acres_total, 2),
                "CN":           cn_weighted,
                "C":            lu_data["c_coeff"],
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Area (acres)", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _reset():
    for k in list(st.session_state.keys()):
        if k != "step":
            st.session_state[k] = None
    st.session_state["step"] = 1
    st.session_state["point_on_stream"] = False



# ---------------------------------------------------------------------------
# Map rendering helpers — soil HSG, soil texture, NLCD land use
# ---------------------------------------------------------------------------

_HSG_COLORS = {
    "A": "#2ca02c",
    "B": "#1f77b4",
    "C": "#ff7f0e",
    "D": "#d62728",
}
_HSG_LABELS = {
    "A": "A — Low runoff (sands)",
    "B": "B — Mod. low runoff",
    "C": "C — Mod. high runoff",
    "D": "D — High runoff (clays)",
}

# NLCD 2024 class codes → (hex color, label)
_NLCD_STYLE = {
    11: ("#4575b4", "Open Water"),
    21: ("#ffb3c1", "Developed, Open Space"),
    22: ("#ff6b6b", "Developed, Low Intensity"),
    23: ("#e03131", "Developed, Medium Intensity"),
    24: ("#7f1010", "Developed, High Intensity"),
    31: ("#adb5bd", "Barren Land"),
    41: ("#74b816", "Deciduous Forest"),
    42: ("#2f9e44", "Evergreen Forest"),
    43: ("#8fb56a", "Mixed Forest"),
    52: ("#e8c97a", "Shrub/Scrub"),
    71: ("#d8f5a2", "Grassland/Herbaceous"),
    81: ("#ffe066", "Pasture/Hay"),
    82: ("#f08c00", "Cultivated Crops"),
    90: ("#63a4c4", "Woody Wetlands"),
    95: ("#a9d4e8", "Emergent Herbaceous Wetlands"),
}


def _ws_centroid(ws_geom):
    c = ws_geom.centroid
    return [c.y, c.x]


def _ws_outline_layer(ws_geom):
    return folium.GeoJson(
        ws_geom.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "none", "color": "black",
            "weight": 2.5, "dashArray": "6 4",
        },
        tooltip=folium.Tooltip("Watershed boundary"),
    )


def _legend_html(title: str, items: list[tuple[str, str]]) -> str:
    """Build a fixed-position HTML legend for folium maps."""
    html = (
        "<div style='position:fixed;bottom:30px;left:30px;z-index:1000;"
        "background:white;padding:12px 16px;border-radius:8px;"
        "box-shadow:0 2px 8px rgba(0,0,0,0.25);font-size:12px;"
        "line-height:1.9;max-height:350px;overflow-y:auto'>"
        f"<b>{title}</b><br>"
    )
    for color, label in items:
        html += (
            f"<span style='background:{color};display:inline-block;"
            f"width:13px;height:13px;margin-right:6px;border-radius:3px'></span>"
            f"{label}<br>"
        )
    html += "</div>"
    return html


def render_hsg_map(soil_gdf, ws_geom) -> folium.Map:
    """Folium map of SSURGO soil polygons coloured by hydrologic soil group."""
    m = folium.Map(location=_ws_centroid(ws_geom), zoom_start=13, tiles="CartoDB positron")

    for _, row in soil_gdf.iterrows():
        hsg = str(row.get("dominant_hsg", "")).strip().upper().split("/")[0]
        color = _HSG_COLORS.get(hsg, "#aaaaaa")
        area  = row.get("area_acres", 0)
        popup = folium.Popup(
            f"<b>MUKEY:</b> {row.get('MUKEY','—')}<br>"
            f"<b>HSG:</b> {hsg or '—'}<br>"
            f"<b>Texture:</b> {row.get('texdesc','—')}<br>"
            f"<b>Area:</b> {area:.1f} ac",
            max_width=240,
        )
        folium.GeoJson(
            row.geometry.__geo_interface__,
            style_function=lambda _, c=color: {
                "fillColor": c, "color": "#333333",
                "weight": 0.8, "fillOpacity": 0.65,
            },
            tooltip=folium.Tooltip(f"HSG {hsg or '?'} | {row.get('texdesc','—')}"),
            popup=popup,
        ).add_to(m)

    _ws_outline_layer(ws_geom).add_to(m)

    legend_items = [(c, _HSG_LABELS[h]) for h, c in _HSG_COLORS.items()]
    m.get_root().html.add_child(folium.Element(_legend_html("Hydrologic Soil Group", legend_items)))
    return m


def render_texture_map(soil_gdf, ws_geom) -> folium.Map:
    """Folium map of SSURGO soil polygons coloured by surface soil texture."""
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors

    textures = sorted(soil_gdf["texdesc"].dropna().unique())
    cmap     = cm.get_cmap("tab20", len(textures))
    tex_colors = {t: mcolors.to_hex(cmap(i)) for i, t in enumerate(textures)}

    m = folium.Map(location=_ws_centroid(ws_geom), zoom_start=13, tiles="CartoDB positron")

    for _, row in soil_gdf.iterrows():
        tex   = row.get("texdesc") or "Unknown"
        color = tex_colors.get(tex, "#aaaaaa")
        area  = row.get("area_acres", 0)
        popup = folium.Popup(
            f"<b>MUKEY:</b> {row.get('MUKEY','—')}<br>"
            f"<b>Texture:</b> {tex}<br>"
            f"<b>HSG:</b> {row.get('dominant_hsg','—')}<br>"
            f"<b>Area:</b> {area:.1f} ac",
            max_width=240,
        )
        folium.GeoJson(
            row.geometry.__geo_interface__,
            style_function=lambda _, c=color: {
                "fillColor": c, "color": "#333333",
                "weight": 0.8, "fillOpacity": 0.65,
            },
            tooltip=folium.Tooltip(tex),
            popup=popup,
        ).add_to(m)

    _ws_outline_layer(ws_geom).add_to(m)

    # Legend — sort by area descending
    area_by_tex = soil_gdf.groupby("texdesc")["area_acres"].sum()
    legend_items = [
        (tex_colors[t], f"{t} ({area_by_tex.get(t, 0):.0f} ac)")
        for t in sorted(textures, key=lambda t: -area_by_tex.get(t, 0))
    ]
    m.get_root().html.add_child(folium.Element(_legend_html("Surface Soil Texture", legend_items)))
    return m


def render_nlcd_figure(nlcd_arr) -> "plt.Figure":
    """Matplotlib figure of the clipped NLCD 2024 raster, coloured by class."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.colors as mcolors

    h, w = nlcd_arr.shape
    rgba_img = np.zeros((h, w, 4), dtype=float)  # RGBA, default transparent

    present = []
    for code, (hex_color, label) in _NLCD_STYLE.items():
        mask = nlcd_arr == code
        if not mask.any():
            continue
        r, g, b, _ = mcolors.to_rgba(hex_color)
        rgba_img[mask] = [r, g, b, 0.85]
        count = int(mask.sum())
        pct   = 100.0 * count / max(1, int((nlcd_arr > 0).sum()))
        present.append((hex_color, f"{label} ({pct:.1f}%)"))

    fig, ax = plt.subplots(figsize=(5, 4))
    ax.imshow(rgba_img, interpolation="nearest")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Land Cover", fontsize=11, fontweight="bold")

    patches = [
        mpatches.Patch(facecolor=c, edgecolor="#555", linewidth=0.5, label=lbl)
        for c, lbl in present
    ]
    ax.legend(
        handles=patches, loc="lower left", fontsize=7,
        framealpha=0.9, ncol=1,
        bbox_to_anchor=(0.01, 0.01),
    )
    fig.tight_layout()
    return fig


def _step_badge(n: int, label: str):
    current = st.session_state["step"]
    if n < current:
        color = "#28a745"
        icon = "check"
    elif n == current:
        color = "#007bff"
        icon = str(n)
    else:
        color = "#aaa"
        icon = str(n)
    st.markdown(
        f'<span style="background:{color};color:white;border-radius:50%;'
        f'padding:2px 8px;margin-right:6px;font-weight:bold">{icon}</span> '
        f'**{label}**',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

st.title("LID Peak Runoff Tool — Oklahoma")
st.caption("Produces peak discharge (cfs) via Curve Number and Rational methods.")

if st.button("Reset / Start Over", type="secondary"):
    _reset()
    st.rerun()

col_steps, col_main = st.columns([1, 3])

with col_steps:
    st.markdown("### Steps")
    _step_badge(1, "Select Stream Point")
    _step_badge(2, "Delineate Watershed")
    _step_badge(3, "Collect Data")
    _step_badge(4, "Calculate")
    _step_badge(5, "Results")

    if st.session_state["error"]:
        st.error(st.session_state["error"])

with col_main:

    # -----------------------------------------------------------------------
    # Step 1 — Point selection on map or manual lat/lon entry
    # -----------------------------------------------------------------------
    if st.session_state["step"] == 1:
        st.subheader("Step 1 — Select a stream point or upload a watershed boundary")

        # ---------------------------------------------------------------
        # Option A — Upload a pre-delineated watershed GeoJSON
        # ---------------------------------------------------------------
        with st.expander("Upload watershed GeoJSON (skip StreamStats)", expanded=True):
            st.caption(
                "Upload a GeoJSON file containing your watershed boundary polygon. "
                "Supported types: Feature, FeatureCollection, Polygon, MultiPolygon."
            )
            uploaded = st.file_uploader("Choose a .geojson or .json file", type=["geojson", "json"], key="ws_upload")

            if uploaded is not None:
                try:
                    raw = json.loads(uploaded.read())

                    # Normalise to FeatureCollection
                    geojson_type = raw.get("type", "")
                    if geojson_type == "FeatureCollection":
                        fc = raw
                    elif geojson_type == "Feature":
                        fc = {"type": "FeatureCollection", "features": [raw]}
                    elif geojson_type in ("Polygon", "MultiPolygon"):
                        fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": raw, "properties": {}}]}
                    else:
                        st.error(f"Unsupported GeoJSON type: '{geojson_type}'. Expected Feature, FeatureCollection, Polygon, or MultiPolygon.")
                        fc = None

                    if fc is not None:
                        from shapely.geometry import shape as _shape
                        import geopandas as gpd

                        # Union all features into one geometry
                        geoms = [_shape(f["geometry"]) for f in fc.get("features", []) if f.get("geometry")]
                        if not geoms:
                            st.error("No valid geometries found in the uploaded file.")
                        else:
                            from shapely.ops import unary_union
                            ws_geom_upload = unary_union(geoms)

                            # Compute area in sq mi via equal-area projection
                            ws_gdf_upload = gpd.GeoDataFrame(geometry=[ws_geom_upload], crs="EPSG:4326")
                            area_m2   = ws_gdf_upload.to_crs("EPSG:5070").geometry.area.iloc[0]
                            area_sqmi = area_m2 / 2_589_988.11

                            centroid_upload = ws_geom_upload.centroid
                            lat_upload = centroid_upload.y
                            lon_upload = centroid_upload.x

                            # Build watershed dict compatible with the rest of the app
                            watershed_from_upload = {
                                "workspace_id": "N/A",
                                "geojson": fc,
                                "area_sqmi": round(area_sqmi, 4),
                                "request_url": f"(uploaded: {uploaded.name})",
                            }

                            st.success(
                                f"Loaded **{uploaded.name}** — "
                                f"area: **{area_sqmi:.3f} mi²** ({area_sqmi * 640:.1f} ac), "
                                f"centroid: {lat_upload:.4f}, {lon_upload:.4f}"
                            )

                            # Preview map
                            m_prev = folium.Map(location=[lat_upload, lon_upload], zoom_start=11, tiles="CartoDB positron")
                            folium.GeoJson(
                                fc,
                                style_function=lambda _: {"color": "#1a6bb0", "fillOpacity": 0.15, "weight": 2.5},
                            ).add_to(m_prev)
                            st_folium(m_prev, height=300, returned_objects=[], use_container_width=True)

                            if st.button("Use this watershed → skip to Step 2", type="primary"):
                                st.session_state["watershed"]   = watershed_from_upload
                                st.session_state["basin_chars"] = {}
                                st.session_state["selected_lat"] = lat_upload
                                st.session_state["selected_lon"] = lon_upload
                                st.session_state["step"] = 2
                                st.rerun()

                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    st.error(f"Could not parse GeoJSON: {exc}")

        st.markdown("---")
        st.caption("— or delineate from a stream point below —")

        # ---------------------------------------------------------------
        # Option B — Click / type a stream point (existing flow)
        # ---------------------------------------------------------------
        # --- Manual lat/lon input ---
        with st.form("manual_coords", clear_on_submit=False):
            ci, cj = st.columns(2)
            manual_lat = ci.text_input("Latitude", placeholder="e.g. 35.4676")
            manual_lon = cj.text_input("Longitude", placeholder="e.g. -97.5164")
            submitted = st.form_submit_button("Use These Coordinates")

        if submitted:
            try:
                lat_val = float(manual_lat)
                lon_val = float(manual_lon)
                if not (33.0 <= lat_val <= 37.5) or not (-103.5 <= lon_val <= -94.0):
                    st.warning("Coordinates appear to be outside Oklahoma. Check your values.")
                else:
                    st.session_state["selected_lat"] = lat_val
                    st.session_state["selected_lon"] = lon_val
            except ValueError:
                st.error("Enter valid decimal numbers for latitude and longitude.")

        # --- Map click ---
        _map_center = st.session_state.get("map1_center") or OKLAHOMA_CENTER
        _map_zoom   = st.session_state.get("map1_zoom")   or DEFAULT_ZOOM
        m = folium.Map(location=_map_center, zoom_start=_map_zoom, tiles="OpenStreetMap")
        MousePosition().add_to(m)

        ok_bounds = [33.6, -103.0, 37.0, -94.4]
        b64 = _raster_overlay_image(ok_bounds, width=600)
        if b64:
            folium.raster_layers.ImageOverlay(
                image=f"data:image/png;base64,{b64}",
                bounds=[[ok_bounds[0], ok_bounds[1]], [ok_bounds[2], ok_bounds[3]]],
                opacity=0.7,
                name="Stream Network",
            ).add_to(m)

        # If a point is already selected, mark it on the map
        if st.session_state["selected_lat"] is not None:
            folium.Marker(
                [st.session_state["selected_lat"], st.session_state["selected_lon"]],
                popup="Selected point",
                icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
            ).add_to(m)

        map_data = st_folium(m, width=900, height=500, key="map_step1")

        # Persist map position so reruns don't reset the view
        if map_data:
            if map_data.get("center"):
                c = map_data["center"]
                st.session_state["map1_center"] = [c["lat"], c["lng"]]
            if map_data.get("zoom"):
                st.session_state["map1_zoom"] = map_data["zoom"]

        if map_data and map_data.get("last_clicked"):
            lat = map_data["last_clicked"]["lat"]
            lon = map_data["last_clicked"]["lng"]
            st.session_state["selected_lat"] = lat
            st.session_state["selected_lon"] = lon

        if st.session_state["selected_lat"] is not None:
            lat = st.session_state["selected_lat"]
            lon = st.session_state["selected_lon"]
            st.info(f"Selected point: {lat:.5f}, {lon:.5f}")
            if st.button("Proceed to Delineation", type="primary"):
                st.session_state["point_on_stream"] = True
                st.session_state["step"] = 2
                st.rerun()

    # -----------------------------------------------------------------------
    # Step 2 — Delineate watershed
    # -----------------------------------------------------------------------
    elif st.session_state["step"] == 2:
        lat = st.session_state["selected_lat"]
        lon = st.session_state["selected_lon"]
        st.subheader("Step 2 — Watershed Delineation")

        # Only call the API once — skip if results are already in session state
        if st.session_state["watershed"] is None:
            st.caption(f"Delineating at ({lat:.5f}, {lon:.5f}) via USGS StreamStats...")
            with st.spinner("Calling USGS StreamStats API..."):
                try:
                    result = delineate_watershed(lat, lon)
                    st.session_state["watershed"] = result
                    basin_chars = get_basin_characteristics(result["workspace_id"])
                    st.session_state["basin_chars"] = basin_chars
                except Exception as e:
                    st.session_state["error"] = f"Delineation failed: {e}"
                    st.error(st.session_state["error"])
                    if st.button("Back to Point Selection"):
                        st.session_state["step"] = 1
                        st.rerun()
                    st.stop()

        # Render results from session state (no API call on reruns)
        result = st.session_state["watershed"]
        basin_chars = st.session_state["basin_chars"]

        st.code(result["request_url"], language=None)

        area_sqmi = result.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
        tlag = basin_chars.get("TLAG", None)
        tc_from_api = tlag_to_tc(tlag) if tlag else None

        st.success("Watershed delineated successfully.")

        c1, c2 = st.columns(2)
        c1.metric("Area (sq mi)", f"{area_sqmi:.3f}" if area_sqmi else "N/A")
        c1.metric("Area (acres)", f"{sqmi_to_acres(area_sqmi):.1f}" if area_sqmi else "N/A")
        if tc_from_api:
            c2.metric("Lag Time (hr)", f"{tlag:.2f}")
            c2.metric("Tc (hr)", f"{tc_from_api:.2f}")

        # Set Tc — from StreamStats if available, otherwise default to 0.5 hr (30 min)
        tc_value = round(tc_from_api, 2) if tc_from_api else 0.5
        st.session_state["tc_hr"] = tc_value

        from shapely.geometry import shape as _shape_s2
        _ws_geom_s2 = _shape_s2(
            result["geojson"]["features"][0]["geometry"]
            if result["geojson"].get("type") == "FeatureCollection"
            else result["geojson"].get("geometry", result["geojson"])
        )
        _centroid_s2 = _ws_centroid(_ws_geom_s2)
        m2 = folium.Map(location=_centroid_s2, zoom_start=11)
        folium.GeoJson(
            result["geojson"],
            style_function=lambda _: {"color": "#1a6bb0", "fillOpacity": 0.1, "weight": 2},
        ).add_to(m2)
        folium.Marker([lat, lon], popup="Pour Point", icon=folium.Icon(color="red")).add_to(m2)
        st_folium(m2, width=900, height=400, key="map_step2")

        if st.button("Proceed to Data Collection", type="primary"):
            st.session_state["step"] = 3
            st.rerun()

    # -----------------------------------------------------------------------
    # Step 3 — Data collection
    # -----------------------------------------------------------------------
    elif st.session_state["step"] == 3:
        st.subheader("Step 3 — Collecting Data")

        lat = st.session_state["selected_lat"]
        lon = st.session_state["selected_lon"]
        watershed = st.session_state["watershed"]

        with st.spinner("Fetching precipitation data..."):
            try:
                atlas14, atlas14_live = fetch_atlas14(lat, lon)
            except RuntimeError as e:
                st.error(f"Precipitation data fetch failed: {e}")
                if st.button("Back to Point Selection", key="back_atlas14"):
                    st.session_state["step"] = 1
                    st.rerun()
                st.stop()
            st.session_state["atlas14"] = atlas14
            st.session_state["atlas14_live"] = atlas14_live

        with st.spinner("Loading spatial data..."):
            try:
                intersection, intersection_live = fetch_landuse_soil_intersection(watershed["geojson"])
                st.session_state["lu_soil_intersection"] = intersection
                st.session_state["intersection_live"] = intersection_live

                if intersection_live and intersection:
                    # Derive marginal fractions from the true spatial intersection
                    soil_pct    = intersection_to_soil_pct(intersection)
                    landuse_pct = intersection_to_landuse_pct(intersection)
                    soil_live   = True
                    lu_live     = True
                else:
                    # Intersection unavailable — fetch soil and land use separately
                    soil_pct, soil_live     = fetch_soil_composition(watershed["geojson"])
                    landuse_pct, lu_live    = fetch_landuse_composition(watershed["geojson"])
            except RuntimeError as e:
                st.error(f"Soil / land use data fetch failed: {e}")
                if st.button("Back to Point Selection", key="back_soillu"):
                    st.session_state["step"] = 1
                    st.rerun()
                st.stop()

            st.session_state["soil_pct"]     = soil_pct
            st.session_state["soil_live"]    = soil_live
            st.session_state["landuse_pct"]  = landuse_pct
            st.session_state["landuse_live"] = lu_live

        with st.spinner("Loading soil texture..."):
            soil_texture, soil_texture_live = fetch_soil_texture(watershed["geojson"])
            st.session_state["soil_texture"] = soil_texture
            st.session_state["soil_texture_live"] = soil_texture_live

        with st.spinner("Loading soil polygons for mapping..."):
            soil_gdf, _ = fetch_soil_geodataframe(watershed["geojson"])
            st.session_state["soil_gdf"] = soil_gdf

        with st.spinner("Loading land cover data..."):
            nlcd_arr, _ = fetch_nlcd_array(watershed["geojson"])
            st.session_state["nlcd_arr"] = nlcd_arr

        with st.spinner("Fetching regression flows..."):
            usgs_flows = get_peak_flow_regression(watershed["workspace_id"])
            st.session_state["usgs_flows"] = usgs_flows

        # Display collected data
        st.success("Data collection complete.")

        # Atlas 14 — full width, collapsed
        _DURATIONS_DISPLAY = [1, 2, 3, 6, 12, 24]
        with st.expander("Precipitation Data (NOAA Atlas 14)", expanded=False):
            atlas_rows = []
            for dur in _DURATIONS_DISPLAY:
                row = {"Duration (hr)": dur}
                for rp in RETURN_PERIODS:
                    try:
                        row[f"{rp}-yr (in)"] = round(atlas14.depth(dur, rp), 2)
                    except Exception:
                        row[f"{rp}-yr (in)"] = "—"
                atlas_rows.append(row)
            st.dataframe(pd.DataFrame(atlas_rows), hide_index=True, use_container_width=True)

        col_a, col_b = st.columns(2)

        with col_a:
            with st.expander("Soil Composition", expanded=True):
                soil_rows = [{"HSG": g, "% of Watershed": pct} for g, pct in soil_pct.items()]
                st.dataframe(pd.DataFrame(soil_rows), hide_index=True)

            soil_texture = st.session_state.get("soil_texture") or {}
            if soil_texture:
                with st.expander("Surface Soil Texture", expanded=True):
                    tex_rows = [{"Texture": t, "% of Watershed": pct} for t, pct in soil_texture.items()]
                    st.dataframe(pd.DataFrame(tex_rows), hide_index=True)

        with col_b:
            with st.expander("Land Use Composition", expanded=True):
                lu_rows = [{"Land Use": lu, "% of Watershed": pct} for lu, pct in landuse_pct.items()]
                st.dataframe(pd.DataFrame(lu_rows), hide_index=True)

            if usgs_flows:
                with st.expander("USGS Regression Flows (reference)", expanded=True):
                    usgs_rows = [
                        {"Return Period (yr)": r["return_period"],
                         "Q (cfs)": round(r["flow_cfs"], 1),
                         "Lower CI": round(r["lower_ci"], 1),
                         "Upper CI": round(r["upper_ci"], 1)}
                        for r in usgs_flows
                    ]
                    st.dataframe(pd.DataFrame(usgs_rows), hide_index=True)

        # --- Soil + land use maps ---
        st.markdown("---")
        st.markdown("### Spatial Maps")

        soil_gdf = st.session_state.get("soil_gdf")
        nlcd_arr = st.session_state.get("nlcd_arr")

        from shapely.geometry import shape as _shape
        ws_geom_map = _shape(
            watershed["geojson"]["features"][0]["geometry"]
            if watershed["geojson"].get("type") == "FeatureCollection"
            else watershed["geojson"].get("geometry", watershed["geojson"])
        )

        map_col1, map_col2 = st.columns(2)

        with map_col1:
            with st.expander("Hydrologic Soil Group", expanded=True):
                if soil_gdf is not None and not soil_gdf.empty:
                    hsg_map = render_hsg_map(soil_gdf, ws_geom_map)
                    st_folium(hsg_map, height=400, returned_objects=[], use_container_width=True)
                else:
                    st.info("Soil polygon data unavailable.")

        with map_col2:
            with st.expander("Surface Soil Texture", expanded=True):
                if soil_gdf is not None and not soil_gdf.empty:
                    tex_map = render_texture_map(soil_gdf, ws_geom_map)
                    st_folium(tex_map, height=400, returned_objects=[], use_container_width=True)
                else:
                    st.info("Soil polygon data unavailable.")

        with st.expander("Land Cover", expanded=True):
            if nlcd_arr is not None:
                nlcd_fig = render_nlcd_figure(nlcd_arr)
                col_nlcd, _ = st.columns([1, 1])
                with col_nlcd:
                    st.pyplot(nlcd_fig, use_container_width=True)
                plt.close(nlcd_fig)
            else:
                st.info("Land cover data unavailable.")

        if st.button("Proceed to Calculations", type="primary"):
            st.session_state["step"] = 4
            st.rerun()

    # -----------------------------------------------------------------------
    # Step 4 — Calculations
    # -----------------------------------------------------------------------
    elif st.session_state["step"] == 4:
        st.subheader("Step 4 — Calculating Peak Flows")

        atlas14      = st.session_state["atlas14"]
        soil_pct     = st.session_state["soil_pct"]
        landuse_pct  = st.session_state["landuse_pct"]
        intersection = st.session_state.get("lu_soil_intersection") or {}
        basin_chars  = st.session_state["basin_chars"]
        watershed    = st.session_state["watershed"]
        usgs_flows   = st.session_state["usgs_flows"] or []

        area_sqmi  = watershed.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
        area_acres = sqmi_to_acres(area_sqmi)
        tc         = st.session_state.get("tc_hr", 1.0)

        # Composite CN — use spatial intersection when available
        if intersection:
            CN = composite_cn_from_intersection(intersection)
        else:
            CN = composite_cn(soil_pct, landuse_pct)
        C = composite_c(landuse_pct)

        col1, col2 = st.columns(2)
        col1.metric("Composite CN", f"{CN:.1f}")
        col2.metric("Composite C", f"{C:.3f}")

        # Storm duration selector — CN method
        _STORM_DURATION_OPTIONS = {
            "1 hr":              1,
            "2 hr":              2,
            "3 hr":              3,
            "4 hr":              4,
            "6 hr":              6,
            "8 hr":              8,
            "12 hr":             12,
            "24 hr (standard)":  24,
        }
        _dur_keys = list(_STORM_DURATION_OPTIONS.keys())
        storm_dur_label = st.selectbox(
            "Storm Duration",
            options=_dur_keys,
            index=_dur_keys.index("24 hr (standard)"),
            help=(
                "Duration of the design storm. Peak flows will be calculated for all return periods "
                "using Atlas 14 depth for this duration."
            ),
            key="storm_duration_select",
        )
        storm_duration_hr = _STORM_DURATION_OPTIONS[storm_dur_label]
        st.session_state["storm_duration_hr"] = storm_duration_hr

        # Incremental runoff table expander
        with st.expander("SCS Type II Incremental Runoff Table", expanded=False):
            _rp_options = RETURN_PERIODS
            _rp_for_tbl = st.selectbox(
                "Return Period", _rp_options,
                index=_rp_options.index(100),
                key="storm_table_rp",
            )
            _P_D_tbl = atlas14.depth(storm_duration_hr, _rp_for_tbl)
            _S_tbl   = (1000.0 / CN) - 10.0
            _tbl     = build_storm_table(_P_D_tbl, storm_duration_hr, CN)
            st.dataframe(pd.DataFrame(_tbl), hide_index=True, use_container_width=True)
            st.caption(
                f"Atlas 14 {storm_duration_hr}-hr, {_rp_for_tbl}-yr depth = {_P_D_tbl:.2f} in  |  "
                f"CN = {CN:.1f}  |  S = {_S_tbl:.3f}  |  Ia = {0.2 * _S_tbl:.3f} in  |  "
                f"Storm window: {12 - storm_duration_hr / 2:.2f}–{12 + storm_duration_hr / 2:.2f} hr"
            )

        # Land use × HSG breakdown
        lu_cn_df = _build_landuse_cn_table(intersection, soil_pct, landuse_pct, area_sqmi)
        if not lu_cn_df.empty:
            with st.expander("Land Use × Soil CN Breakdown", expanded=True):
                if intersection:
                    st.caption("Each row is an exact spatial (land use, HSG) combination from the NLCD × SSURGO pixel intersection.")
                else:
                    st.caption("Spatial intersection unavailable — CN shown is the soil-weighted composite per land use class.")
                st.dataframe(lu_cn_df, hide_index=True, use_container_width=True)

        # Build results per return period
        usgs_by_rp = {r["return_period"]: r["flow_cfs"] for r in usgs_flows}

        rows = []
        for rp in RETURN_PERIODS:
            intensity_tc = atlas14.intensity(tc, rp)                     # Tc-based intensity for Rational Method
            depth_D      = atlas14.depth(storm_duration_hr, rp)          # Atlas 14 depth for CN method duration

            q_rational = rational_peak_flow(C, intensity_tc, area_acres)
            q_cn = cn_peak_flow(CN, depth_D, area_sqmi, tc, storm_duration_hr)
            q_usgs = usgs_by_rp.get(rp, None)

            row = {
                "Return Period (yr)": rp,
                "Rational Q (cfs)": q_rational,
                "CN Method Q (cfs)": q_cn,
            }
            if q_usgs is not None:
                row["USGS Regression Q (cfs)"] = round(q_usgs, 1)
            rows.append(row)

        results_df = pd.DataFrame(rows)
        st.session_state["results_df"] = results_df

        st.success("Calculations complete.")
        st.dataframe(results_df, hide_index=True, use_container_width=True)

        if st.button("View Full Results", type="primary"):
            st.session_state["step"] = 5
            st.rerun()

    # -----------------------------------------------------------------------
    # Step 5 — Results
    # -----------------------------------------------------------------------
    elif st.session_state["step"] == 5:
        st.subheader("Step 5 — Peak Discharge Results")

        results_df   = st.session_state["results_df"]
        watershed    = st.session_state["watershed"]
        basin_chars  = st.session_state["basin_chars"]
        soil_pct     = st.session_state["soil_pct"]
        landuse_pct  = st.session_state["landuse_pct"]
        intersection = st.session_state.get("lu_soil_intersection") or {}
        atlas14      = st.session_state["atlas14"]

        area_sqmi = watershed.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
        tc = st.session_state.get("tc_hr", 1.0)
        CN = composite_cn_from_intersection(intersection) if intersection else composite_cn(soil_pct, landuse_pct)
        C  = composite_c(landuse_pct)

        # Summary metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Watershed Area", f"{area_sqmi:.3f} mi²")
        m2.metric("Tc", f"{tc:.2f} hr")
        m3.metric("Composite CN", f"{CN:.1f}")
        m4.metric("Composite C", f"{C:.3f}")

        st.markdown("### Peak Discharge by Return Period")
        st.dataframe(results_df, hide_index=True, use_container_width=True)

        # Bar chart
        chart_df = results_df.set_index("Return Period (yr)")[
            [c for c in results_df.columns if "Q (cfs)" in c]
        ]
        st.bar_chart(chart_df)

        # CSV download
        csv_bytes = results_df.to_csv(index=False).encode()
        st.download_button(
            label="Download Results as CSV",
            data=csv_bytes,
            file_name="peak_runoff_results.csv",
            mime="text/csv",
        )

        # Supporting data expanders
        with st.expander("Watershed Details"):
            # Land use × HSG CN/C breakdown table
            lu_cn_df = _build_landuse_cn_table(intersection, soil_pct, landuse_pct, area_sqmi)
            if not lu_cn_df.empty:
                st.markdown("**Land Use × Soil HSG — CN and C Breakdown**")
                if intersection:
                    st.caption("Each row is an exact spatial (land use, HSG) combination from the NLCD × SSURGO pixel intersection.")
                else:
                    st.caption("Spatial intersection unavailable — CN shown is the soil-weighted composite per land use class.")
                st.dataframe(lu_cn_df, hide_index=True, use_container_width=True)
                st.markdown("---")

            col_s, col_l = st.columns(2)
            with col_s:
                st.markdown("**Soil Composition — HSG (SSURGO)**")
                st.dataframe(
                    pd.DataFrame([{"HSG": g, "%": pct} for g, pct in soil_pct.items()]),
                    hide_index=True,
                )
                soil_texture = st.session_state.get("soil_texture") or {}
                if soil_texture:
                    st.markdown("**Surface Soil Texture (gSSURGO)**")
                    st.dataframe(
                        pd.DataFrame([{"Texture": t, "%": pct} for t, pct in soil_texture.items()]),
                        hide_index=True,
                    )
            with col_l:
                st.markdown("**Land Use Composition (NLCD 2024)**")
                st.dataframe(
                    pd.DataFrame([{"Land Use": lu, "%": pct} for lu, pct in landuse_pct.items()]),
                    hide_index=True,
                )

        with st.expander("Atlas 14 Precipitation"):
            atlas_rows = [
                {"Return Period (yr)": rp,
                 "1-hr Intensity (in/hr)": round(atlas14.intensity(1, rp), 3),
                 f"Tc={tc:.2f}-hr Intensity (in/hr)": round(atlas14.intensity(tc, rp), 3),
                 "24-hr Depth (in)": round(atlas14.depth(24, rp), 2)}
                for rp in RETURN_PERIODS
            ]
            st.dataframe(pd.DataFrame(atlas_rows), hide_index=True)

        with st.expander("Basin Characteristics (raw)"):
            st.json(basin_chars)

        with st.expander("Watershed GeoJSON"):
            st.json(watershed["geojson"])
