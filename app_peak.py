"""
app_peak.py — LID Peak Runoff Tool

5-step workflow:
  1. Select a point on the stream network
  2. Delineate the watershed (USGS StreamStats)
  3. Collect data: Atlas 14 precipitation, SSURGO soils, NLCD land use
  4. Calculate composite CN, C, and peak flows (CN method + Rational method)
  5. Display results table; download as CSV
"""

import os
import sys
import json
import base64
import io
import time
import zipfile
import xml.etree.ElementTree as ET

# PROJ environment — must be set before any geospatial imports
def _find_proj_data():
    candidates = [
        os.path.join(sys.prefix, "share", "proj"),  # conda/venv on any OS
        "/opt/homebrew/share/proj",
        "/usr/local/share/proj",
        "/usr/share/proj",
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p
    return None

_proj = _find_proj_data()
if _proj:
    os.environ.setdefault("PROJ_DATA", _proj)

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import plotly.graph_objects as go
import plotly.subplots as psp
import streamlit as st
import folium
from folium.plugins import MousePosition
from streamlit_folium import st_folium
import geopandas as gpd
try:
    import contextily as ctx
    _HAS_CTX = True
except ImportError:
    _HAS_CTX = False
from PIL import Image

from reference_data import RETURN_PERIODS, LANDUSE_TYPES
from hydrology import (
    composite_cn,
    composite_cn_from_intersection,
    composite_c,
    cn_peak_flow,
    scs_uh_peak_flow,
    scs_uh_hydrograph,
    build_storm_table,
    rational_peak_flow,
    sqmi_to_acres,
    tlag_to_tc,
    tc_scs_lag,
    tc_kirpich,
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
    fetch_dem_features,
    reverse_geocode_state,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USA_CENTER = [39.8283, -98.5795]
DEFAULT_ZOOM = 4

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Session state initialization
# ---------------------------------------------------------------------------

def _init_state():
    defaults = {
        "step": 1,
        "selected_lat": None,
        "selected_lon": None,
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
        "map1_center": USA_CENTER,
        "map1_zoom": DEFAULT_ZOOM,
        "storm_duration_hr": 24,  # CN method design storm duration (hours)
        "lag_L_ft":   None,       # SCS lag: flow length (ft) — from DEM
        "lag_Y_pct":  None,       # SCS lag: average slope (%) — from DEM
        "use_lag_tc": False,      # whether to override Tc with SCS lag formula
        "dem_features": None,     # dict from fetch_dem_features()
        "dem_tc_hr":  None,       # Tc computed from DEM L + Y + CN
        "atlas14_live": None,     # True if Atlas 14 returned live data, False if fallback
        "soil_live": None,        # True if SSURGO returned live data, False if fallback
        "landuse_live": None,     # True if NLCD returned live data, False if fallback
        "results_df": None,       # pd.DataFrame of final results (combined)
        "cn_df":      None,       # pd.DataFrame — CN method results per return period
        "rational_df": None,      # pd.DataFrame — Rational method results per return period
        "dem_fetch_done": False,  # True once DEM fetch has been attempted (even if it failed)
        "error": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

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
    _keep = {"map1_center", "map1_zoom"}   # preserve viewport across resets
    for k in list(st.session_state.keys()):
        if k not in _keep and k != "step":
            st.session_state[k] = None
    st.session_state["step"] = 1


def _coords_from_kml_text(coord_text: str) -> list[tuple[float, float]]:
    """Parse KML coordinates text into a list of (lon, lat) tuples."""
    coords: list[tuple[float, float]] = []
    for token in (coord_text or "").replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
            coords.append((lon, lat))
        except ValueError:
            continue
    return coords


def _feature_collection_from_kml_bytes(data: bytes) -> dict:
    """Build a GeoJSON FeatureCollection from KML Polygon geometries."""
    from shapely.geometry import Polygon, mapping
    from shapely.ops import unary_union

    root = ET.fromstring(data)
    polygons = []

    for poly_el in root.iter():
        if not poly_el.tag.endswith("Polygon"):
            continue

        shell = None
        holes = []
        for child in poly_el.iter():
            if not child.tag.endswith("coordinates"):
                continue

            ring_coords = _coords_from_kml_text(child.text or "")
            if len(ring_coords) < 3:
                continue
            if ring_coords[0] != ring_coords[-1]:
                ring_coords.append(ring_coords[0])

            if any(tag.tag.endswith("outerBoundaryIs") for tag in child.iterancestors()) if hasattr(child, "iterancestors") else False:
                shell = ring_coords
            elif any(tag.tag.endswith("innerBoundaryIs") for tag in child.iterancestors()) if hasattr(child, "iterancestors") else False:
                holes.append(ring_coords)

        if shell is None:
            # Fallback for stdlib ElementTree (no iterancestors):
            # first coordinates in polygon is shell, remaining are holes.
            rings = []
            for child in poly_el.iter():
                if child.tag.endswith("coordinates"):
                    ring = _coords_from_kml_text(child.text or "")
                    if len(ring) >= 3:
                        if ring[0] != ring[-1]:
                            ring.append(ring[0])
                        rings.append(ring)
            if rings:
                shell = rings[0]
                holes = rings[1:]

        if shell is None or len(shell) < 4:
            continue

        poly = Polygon(shell=shell, holes=holes if holes else None)
        if poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            polygons.append(poly)

    if not polygons:
        raise ValueError("No Polygon geometry found in KML/KMZ. Upload a polygon watershed boundary.")

    merged = unary_union(polygons)
    feature = {"type": "Feature", "geometry": mapping(merged), "properties": {}}
    return {"type": "FeatureCollection", "features": [feature]}


def _watershed_fc_from_upload(uploaded_file) -> tuple[dict, str]:
    """Parse uploaded watershed file into a FeatureCollection and source label."""
    filename = (uploaded_file.name or "").lower()
    data = uploaded_file.getvalue()

    if filename.endswith((".geojson", ".json")):
        raw = json.loads(data.decode("utf-8"))
        geojson_type = raw.get("type", "")
        if geojson_type == "FeatureCollection":
            return raw, "GeoJSON"
        if geojson_type == "Feature":
            return {"type": "FeatureCollection", "features": [raw]}, "GeoJSON"
        if geojson_type in ("Polygon", "MultiPolygon"):
            return {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": raw, "properties": {}}],
            }, "GeoJSON"
        raise ValueError(
            f"Unsupported GeoJSON type: '{geojson_type}'. Expected Feature, FeatureCollection, Polygon, or MultiPolygon."
        )

    if filename.endswith(".kml"):
        return _feature_collection_from_kml_bytes(data), "KML"

    if filename.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("KMZ does not contain a .kml file.")
            kml_data = zf.read(kml_names[0])
        return _feature_collection_from_kml_bytes(kml_data), "KMZ"

    raise ValueError("Unsupported file type. Upload .geojson, .json, .kml, or .kmz.")



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


def render_nlcd_figure(nlcd_arr):
    """
    Render the NLCD raster as a matplotlib figure (no embedded legend)
    and return the legend items separately so they can be placed outside
    the image.

    Returns
    -------
    fig : plt.Figure
    legend_items : list of (hex_color, label_str)  — sorted by descending area
    """
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    h, w = nlcd_arr.shape
    rgba_img = np.zeros((h, w, 4), dtype=float)

    total_px = max(1, int((nlcd_arr > 0).sum()))
    legend_items = []
    for code, (hex_color, label) in _NLCD_STYLE.items():
        mask = nlcd_arr == code
        if not mask.any():
            continue
        r, g, b, _ = mcolors.to_rgba(hex_color)
        rgba_img[mask] = [r, g, b, 0.85]
        pct = 100.0 * int(mask.sum()) / total_px
        legend_items.append((hex_color, label, pct))

    # Sort by descending coverage so the dominant class is at the top
    legend_items.sort(key=lambda x: -x[2])

    fig, ax = plt.subplots(figsize=(5, 4))
    ax.imshow(rgba_img, interpolation="nearest")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Land Cover", fontsize=11, fontweight="bold")
    fig.tight_layout()

    return fig, [(c, f"{lbl} ({pct:.1f}%)") for c, lbl, pct in legend_items]


def render_nlcd_map(nlcd_arr, ws_geom) -> folium.Map:
    """
    Interactive Folium map of the NLCD land cover raster.

    The NLCD array is converted to an RGBA ImageOverlay using _NLCD_STYLE
    colours.  Zero-valued pixels (outside the watershed) are transparent.
    Bounds are derived from the watershed geometry + the same 0.01-degree
    buffer that _fetch_nlcd_tile_wcs uses, so the image aligns correctly.
    """
    import matplotlib.colors as mcolors

    _BUF = 0.01   # must match api_clients._NLCD_BBOX_BUF
    minx, miny, maxx, maxy = ws_geom.bounds
    bbox_w = minx - _BUF
    bbox_s = miny - _BUF
    bbox_e = maxx + _BUF
    bbox_n = maxy + _BUF

    h, w = nlcd_arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)

    total_px = max(1, int((nlcd_arr > 0).sum()))
    legend_items = []
    for code, (hex_color, label) in _NLCD_STYLE.items():
        mask = nlcd_arr == code
        if not mask.any():
            continue
        r, g, b, _ = mcolors.to_rgba(hex_color)
        rgba[mask] = [int(r * 255), int(g * 255), int(b * 255), 210]
        pct = 100.0 * int(mask.sum()) / total_px
        legend_items.append((hex_color, label, pct))

    # Sort by descending coverage
    legend_items.sort(key=lambda x: -x[2])

    img_pil = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img_pil.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    centroid = _ws_centroid(ws_geom)
    m = folium.Map(location=centroid, zoom_start=12, tiles="CartoDB positron")
    m.fit_bounds([[bbox_s, bbox_w], [bbox_n, bbox_e]], padding=[10, 10])

    folium.raster_layers.ImageOverlay(
        image=f"data:image/png;base64,{b64}",
        bounds=[[bbox_s, bbox_w], [bbox_n, bbox_e]],
        opacity=1.0,
        name="NLCD Land Cover",
    ).add_to(m)

    _ws_outline_layer(ws_geom).add_to(m)

    legend_entries = [(c, f"{lbl} ({pct:.1f}%)") for c, lbl, pct in legend_items]
    m.get_root().html.add_child(
        folium.Element(_legend_html("Land Cover (NLCD 2024)", legend_entries))
    )
    return m


def render_dem_map(dem_features: dict, ws_geom) -> folium.Map:
    """
    Folium map showing the DEM elevation as a coloured ImageOverlay (terrain
    colourmap) with the watershed boundary on top.
    """
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors

    dem_arr = dem_features["dem_array"]
    bbox_w, bbox_s, bbox_e, bbox_n = dem_features["dem_bounds"]

    valid = dem_arr[np.isfinite(dem_arr)]
    vmin, vmax = float(valid.min()), float(valid.max())

    cmap = cm.get_cmap("terrain")
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)

    # Build RGBA image — transparent outside the watershed mask
    rgba = np.zeros((*dem_arr.shape, 4), dtype=np.uint8)
    inside = np.isfinite(dem_arr)
    colored = cmap(norm(np.where(inside, dem_arr, vmin)))   # (H, W, 4) float
    rgba[inside] = (colored[inside] * 255).astype(np.uint8)
    rgba[inside, 3] = 210   # slight transparency

    img_pil = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img_pil.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    centroid = _ws_centroid(ws_geom)
    m = folium.Map(location=centroid, zoom_start=12, tiles="CartoDB positron")
    m.fit_bounds([[bbox_s, bbox_w], [bbox_n, bbox_e]], padding=[10, 10])

    folium.raster_layers.ImageOverlay(
        image=f"data:image/png;base64,{b64}",
        bounds=[[bbox_s, bbox_w], [bbox_n, bbox_e]],
        opacity=0.85,
        name="DEM Elevation",
    ).add_to(m)

    _ws_outline_layer(ws_geom).add_to(m)

    # Simple elevation legend
    legend_html = (
        "<div style='position:fixed;bottom:30px;left:30px;z-index:1000;"
        "background:white;padding:10px 14px;border-radius:8px;"
        "box-shadow:0 2px 8px rgba(0,0,0,0.25);font-size:12px;line-height:1.8'>"
        "<b>Elevation (m)</b><br>"
        f"<span style='color:#555'>Max:</span> <b>{vmax:.1f} m</b><br>"
        f"<span style='color:#555'>Min:</span> <b>{vmin:.1f} m</b><br>"
        f"<span style='color:#555'>Range:</span> <b>{vmax - vmin:.1f} m</b>"
        "</div>"
    )
    m.get_root().html.add_child(folium.Element(legend_html))
    return m


# ---------------------------------------------------------------------------
# Static map render helpers (matplotlib + contextily basemap)
# ---------------------------------------------------------------------------

def _add_basemap(ax):
    """Add CartoDB Positron basemap tiles to a matplotlib axis (EPSG:3857 assumed)."""
    if _HAS_CTX:
        try:
            ctx.add_basemap(ax, source=ctx.providers.CartoDB.Positron, zoom="auto", zorder=0)
        except Exception:
            pass


def _ws_outline_patch(ws_geom, target_crs="EPSG:3857"):
    """Return a GeoDataFrame of the watershed boundary reprojected to target_crs."""
    return gpd.GeoDataFrame(geometry=[ws_geom], crs="EPSG:4326").to_crs(target_crs)


def _render_legend_html(items: list[tuple[str, str]]) -> str:
    """Render a list of (hex_color, label) tuples as HTML color swatches."""
    rows = ""
    for color, label in items:
        rows += (
            f"<div style='margin:3px 0;line-height:1.4'>"
            f"<span style='display:inline-block;width:13px;height:13px;"
            f"background:{color};border-radius:2px;margin-right:6px;"
            f"vertical-align:middle;flex-shrink:0'></span>"
            f"<span style='font-size:22px'>{label}</span></div>"
        )
    return rows


def render_hsg_static(soil_gdf, ws_geom):
    """Static matplotlib map of SSURGO soil polygons by hydrologic soil group.

    Returns (fig, legend_html_str).
    """
    soil_3857 = soil_gdf.copy().to_crs(epsg=3857)
    soil_3857["_color"] = soil_3857["dominant_hsg"].apply(
        lambda h: _HSG_COLORS.get(str(h).strip().upper().split("/")[0], "#aaaaaa")
    )
    ws_3857 = _ws_outline_patch(ws_geom)

    fig, ax = plt.subplots(figsize=(2, 1.5))
    ax.set_xlim(*soil_3857.total_bounds[[0, 2]])
    ax.set_ylim(*soil_3857.total_bounds[[1, 3]])
    soil_3857.plot(ax=ax, color=soil_3857["_color"], edgecolor="#333333",
                   linewidth=0.6, alpha=0.70, zorder=1)
    ws_3857.plot(ax=ax, facecolor="none", edgecolor="black",
                 linewidth=2, linestyle="--", zorder=2)
    ax.set_axis_off()
    ax.set_title("Hydrologic Soil Group (SSURGO)", fontsize=9, fontweight="bold", pad=6)
    fig.tight_layout(pad=0.3)

    present = set(str(h).strip().upper().split("/")[0]
                  for h in soil_gdf["dominant_hsg"].dropna())
    legend_items = [
        (_HSG_COLORS[h], _HSG_LABELS[h])
        for h in ["A", "B", "C", "D"] if h in present
    ]
    return fig, _render_legend_html(legend_items)


def render_texture_static(soil_gdf, ws_geom):
    """Static matplotlib map of SSURGO soil polygons by surface texture.

    Returns (fig, legend_html_str).
    """
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors

    textures   = sorted(soil_gdf["texdesc"].dropna().unique())
    cmap_tab   = cm.get_cmap("tab20", len(textures))
    tex_colors = {t: mcolors.to_hex(cmap_tab(i)) for i, t in enumerate(textures)}

    soil_3857 = soil_gdf.copy().to_crs(epsg=3857)
    soil_3857["_color"] = soil_3857["texdesc"].apply(
        lambda t: tex_colors.get(t or "Unknown", "#aaaaaa")
    )
    ws_3857 = _ws_outline_patch(ws_geom)

    fig, ax = plt.subplots(figsize=(2, 1.5))
    ax.set_xlim(*soil_3857.total_bounds[[0, 2]])
    ax.set_ylim(*soil_3857.total_bounds[[1, 3]])
    soil_3857.plot(ax=ax, color=soil_3857["_color"], edgecolor="#333333",
                   linewidth=0.6, alpha=0.70, zorder=1)
    ws_3857.plot(ax=ax, facecolor="none", edgecolor="black",
                 linewidth=2, linestyle="--", zorder=2)
    ax.set_axis_off()
    ax.set_title("Surface Soil Texture (gSSURGO)", fontsize=9, fontweight="bold", pad=6)
    fig.tight_layout(pad=0.3)

    area_by_tex = soil_gdf.groupby("texdesc")["area_acres"].sum()
    sorted_tex  = sorted(textures, key=lambda t: -area_by_tex.get(t, 0))
    legend_items = [
        (tex_colors[t], f"{t} ({area_by_tex.get(t, 0):.0f} ac)")
        for t in sorted_tex
    ]
    return fig, _render_legend_html(legend_items)


def render_nlcd_static(nlcd_arr, ws_geom):
    """Static matplotlib map of NLCD land cover with contextily basemap.

    Returns (fig, legend_html_str).
    """
    import matplotlib.colors as mcolors
    from pyproj import Transformer

    _BUF = 0.01
    minx, miny, maxx, maxy = ws_geom.bounds
    t4326_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x0, y0 = t4326_3857.transform(minx - _BUF, miny - _BUF)
    x1, y1 = t4326_3857.transform(maxx + _BUF, maxy + _BUF)

    h, w = nlcd_arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    total_px     = max(1, int((nlcd_arr > 0).sum()))
    legend_tuples = []
    for code, (hex_color, label) in _NLCD_STYLE.items():
        mask = nlcd_arr == code
        if not mask.any():
            continue
        r, g, b, _ = mcolors.to_rgba(hex_color)
        rgba[mask] = [int(r * 255), int(g * 255), int(b * 255), 200]
        pct = 100.0 * int(mask.sum()) / total_px
        legend_tuples.append((hex_color, label, pct))
    legend_tuples.sort(key=lambda x: -x[2])

    ws_3857 = _ws_outline_patch(ws_geom)

    fig, ax = plt.subplots(figsize=(2, 1.5))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.imshow(rgba, extent=[x0, x1, y0, y1], origin="upper",
              aspect="auto", interpolation="nearest", zorder=1)
    ws_3857.plot(ax=ax, facecolor="none", edgecolor="black",
                 linewidth=2, linestyle="--", zorder=2)
    ax.set_axis_off()
    ax.set_title("Land Cover (NLCD 2024)", fontsize=9, fontweight="bold", pad=6)
    fig.tight_layout(pad=0.3)

    legend_items = [(c, f"{lbl} ({pct:.1f}%)") for c, lbl, pct in legend_tuples]
    return fig, _render_legend_html(legend_items)


def render_dem_static(dem_features: dict, ws_geom):
    """Static matplotlib map of DEM elevation with contextily basemap.

    Returns (fig, legend_html_str)  — legend is an elevation range summary.
    """
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors
    from pyproj import Transformer

    dem_arr = dem_features["dem_array"]
    bbox_w, bbox_s, bbox_e, bbox_n = dem_features["dem_bounds"]

    t4326_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x0, y0 = t4326_3857.transform(bbox_w, bbox_s)
    x1, y1 = t4326_3857.transform(bbox_e, bbox_n)

    valid = dem_arr[np.isfinite(dem_arr)]
    vmin, vmax = float(valid.min()), float(valid.max())

    cmap  = cm.get_cmap("terrain")
    norm  = mcolors.Normalize(vmin=vmin, vmax=vmax)
    rgba  = np.zeros((*dem_arr.shape, 4), dtype=np.uint8)
    inside  = np.isfinite(dem_arr)
    colored = cmap(norm(np.where(inside, dem_arr, vmin)))
    rgba[inside]    = (colored[inside] * 255).astype(np.uint8)
    rgba[inside, 3] = 200

    ws_3857 = _ws_outline_patch(ws_geom)

    fig, ax = plt.subplots(figsize=(2, 1.5))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.imshow(rgba, extent=[x0, x1, y0, y1], origin="upper",
              aspect="auto", interpolation="nearest", zorder=1)
    ws_3857.plot(ax=ax, facecolor="none", edgecolor="black",
                 linewidth=2, linestyle="--", zorder=2)
    ax.set_axis_off()
    ax.set_title("DEM Elevation (USGS 3DEP)", fontsize=9, fontweight="bold", pad=6)
    fig.tight_layout(pad=0.3)

    # Gradient swatch legend for elevation
    n_steps = 6
    legend_html = "<div style='font-size:22px;font-weight:bold;margin-bottom:4px'>Elevation (m)</div>"
    for i in range(n_steps):
        frac  = i / (n_steps - 1)
        elev  = vmin + frac * (vmax - vmin)
        color = mcolors.to_hex(cmap(frac))
        legend_html += (
            f"<div style='margin:2px 0'>"
            f"<span style='display:inline-block;width:13px;height:13px;"
            f"background:{color};border-radius:2px;margin-right:6px;"
            f"vertical-align:middle'></span>"
            f"<span style='font-size:22px'>{elev:.1f} m</span></div>"
        )
    return fig, legend_html


def _generate_report_html() -> bytes:
    """
    Build a self-contained HTML report from current session state.
    Returns UTF-8 encoded bytes for st.download_button.
    """
    import base64
    from io import BytesIO
    from datetime import datetime
    from shapely.geometry import shape as _rshape

    ss = st.session_state

    watershed    = ss.get("watershed") or {}
    basin_chars  = ss.get("basin_chars") or {}
    soil_pct     = ss.get("soil_pct") or {}
    soil_texture = ss.get("soil_texture") or {}
    landuse_pct  = ss.get("landuse_pct") or {}
    intersection = ss.get("lu_soil_intersection") or {}
    atlas14      = ss.get("atlas14")
    cn_df        = ss.get("cn_df")
    rational_df  = ss.get("rational_df")
    results_df   = ss.get("results_df")
    dem_feats    = ss.get("dem_features") or {}
    lat          = ss.get("selected_lat")
    lon          = ss.get("selected_lon")
    tc           = ss.get("tc_hr", 1.0)
    storm_dur    = ss.get("storm_duration_hr", 24)
    area_sqmi    = ss.get("area_sqmi_used") or watershed.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
    area_acres   = sqmi_to_acres(area_sqmi)

    if intersection:
        CN = composite_cn_from_intersection(intersection)
    elif soil_pct and landuse_pct:
        CN = composite_cn(soil_pct, landuse_pct)
    else:
        CN = 0.0
    C = composite_c(landuse_pct) if landuse_pct else 0.0

    today = datetime.now().strftime("%B %d, %Y")

    # ------------------------------------------------------------------ helpers
    def fig_b64(fig, dpi=150):
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode()
        plt.close(fig)
        return f"data:image/png;base64,{b64}"

    def metric_box(label, value, cols=None):
        return (
            f'<div class="metric">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-value">{value}</div>'
            f'</div>'
        )

    def img_tag(src, caption="", width="80%"):
        if not src:
            return ""
        cap = f'<figcaption class="caption">{caption}</figcaption>' if caption else ""
        return f'<figure><img src="{src}" style="width:{width};">{cap}</figure>'

    def html_table(df):
        if df is None or df.empty:
            return ""
        heads = "".join(f"<th>{c}</th>" for c in df.columns)
        body  = ""
        for _, row in df.iterrows():
            cells = "".join(f"<td>{v}</td>" for v in row)
            body += f"<tr>{cells}</tr>"
        return f'<table><thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table>'

    # ------------------------------------------------------------------ Figure 1: watershed map
    ws_img  = ""
    ws_geom = None
    try:
        geojson = watershed.get("geojson", {})
        if geojson.get("type") == "FeatureCollection":
            ws_geom = _rshape(geojson["features"][0]["geometry"])
        else:
            ws_geom = _rshape(geojson.get("geometry", geojson))
        ws_3857_r = gpd.GeoDataFrame(geometry=[ws_geom], crs="EPSG:4326").to_crs(epsg=3857)
        fig, ax = plt.subplots(figsize=(6, 5))
        ax.set_xlim(*ws_3857_r.total_bounds[[0, 2]])
        ax.set_ylim(*ws_3857_r.total_bounds[[1, 3]])
        _add_basemap(ax)
        ws_3857_r.plot(ax=ax, facecolor="#1565c020", edgecolor="#0d2b6e",
                       linewidth=2, zorder=1)
        if lat and lon:
            from pyproj import Transformer as _T
            _tx = _T.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
            px, py = _tx.transform(lon, lat)
            ax.plot(px, py, "rv", markersize=9, zorder=3, label="Pour Point")
            ax.legend(fontsize=9)
        ax.set_axis_off()
        ax.set_title("Watershed Boundary (USGS StreamStats)", fontsize=11, fontweight="bold", pad=8)
        fig.tight_layout(pad=0.5)
        ws_img = fig_b64(fig)
    except Exception:
        pass

    # ------------------------------------------------------------------ Figure 3: HSG spatial map
    hsg_map_img = ""
    soil_gdf_r  = ss.get("soil_gdf")
    if soil_gdf_r is not None and not soil_gdf_r.empty and ws_geom is not None:
        try:
            _hsg_f, _ = render_hsg_static(soil_gdf_r, ws_geom)
            hsg_map_img = fig_b64(_hsg_f)
        except Exception:
            pass

    # ------------------------------------------------------------------ Figure 4: NLCD spatial map
    nlcd_map_img = ""
    nlcd_arr_r   = ss.get("nlcd_arr")
    if nlcd_arr_r is not None and ws_geom is not None:
        try:
            _nlcd_f, _ = render_nlcd_static(nlcd_arr_r, ws_geom)
            nlcd_map_img = fig_b64(_nlcd_f)
        except Exception:
            pass

    # ------------------------------------------------------------------ Figure 5: HSG pie
    soil_img = ""
    if soil_pct:
        fig, ax = plt.subplots(figsize=(4, 3.2))
        labels  = [f"HSG {g}" for g in soil_pct]
        sizes   = list(soil_pct.values())
        colors  = ["#4caf50", "#2196f3", "#ff9800", "#e53935"][:len(sizes)]
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, autopct="%1.1f%%",
            colors=colors, startangle=90,
            textprops={"fontsize": 9},
        )
        for at in autotexts:
            at.set_fontsize(8)
        ax.set_title("Hydrologic Soil Group Distribution", fontsize=10, fontweight="bold")
        fig.tight_layout()
        soil_img = fig_b64(fig)

    # ------------------------------------------------------------------ Figure 6: land use bar
    lu_img = ""
    if landuse_pct:
        sorted_lu  = sorted(landuse_pct.items(), key=lambda x: x[1])
        lu_names   = [k for k, _ in sorted_lu]
        lu_vals    = [v for _, v in sorted_lu]
        fig_h      = max(2.8, len(lu_names) * 0.38)
        fig, ax = plt.subplots(figsize=(5.5, fig_h))
        bars = ax.barh(lu_names, lu_vals, color="#1565c0", alpha=0.75)
        for bar, val in zip(bars, lu_vals):
            ax.text(val + 0.4, bar.get_y() + bar.get_height() / 2,
                    f"{val:.1f}%", va="center", fontsize=8)
        ax.set_xlabel("Area (%)", fontsize=9)
        ax.set_xlim(0, 105)
        ax.set_title("Land Use Distribution (NLCD 2024)", fontsize=10, fontweight="bold")
        ax.tick_params(labelsize=8)
        ax.grid(axis="x", alpha=0.3, linestyle="--")
        fig.tight_layout()
        lu_img = fig_b64(fig)

    # ------------------------------------------------------------------ Figure 7: CN bars
    cn_img = ""
    if cn_df is not None and not cn_df.empty:
        rps   = [str(r) for r in cn_df["Return Period (yr)"].tolist()]
        qcns  = cn_df["CN Peak Q (cfs)"].tolist()
        fig, ax = plt.subplots(figsize=(5.5, 3.2))
        bars = ax.bar(rps, qcns, color="#0d2b6e", alpha=0.82, width=0.6)
        _top = max(qcns) if qcns else 1
        for bar, val in zip(bars, qcns):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + _top * 0.012,
                    f"{val:,.0f}", ha="center", va="bottom", fontsize=8)
        ax.set_xlabel("Return Period (yr)", fontsize=9)
        ax.set_ylabel("Peak Discharge (cfs)", fontsize=9)
        ax.set_title(f"CN Method — {storm_dur}-hr Storm", fontsize=10, fontweight="bold")
        ax.grid(axis="y", alpha=0.3, linestyle="--")
        ax.tick_params(labelsize=9)
        fig.tight_layout()
        cn_img = fig_b64(fig)

    # ------------------------------------------------------------------ Figure 8: Rational bars
    rat_img = ""
    if rational_df is not None and not rational_df.empty:
        rps   = [str(r) for r in rational_df["Return Period (yr)"].tolist()]
        qrats = rational_df["Rational Peak Q (cfs)"].tolist()
        fig, ax = plt.subplots(figsize=(5.5, 3.2))
        bars = ax.bar(rps, qrats, color="#1b5e20", alpha=0.82, width=0.6)
        _top = max(qrats) if qrats else 1
        for bar, val in zip(bars, qrats):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + _top * 0.012,
                    f"{val:,.0f}", ha="center", va="bottom", fontsize=8)
        ax.set_xlabel("Return Period (yr)", fontsize=9)
        ax.set_ylabel("Peak Discharge (cfs)", fontsize=9)
        ax.set_title(f"Rational Method  (Tc = {tc * 60:.0f} min)", fontsize=10, fontweight="bold")
        ax.grid(axis="y", alpha=0.3, linestyle="--")
        ax.tick_params(labelsize=9)
        fig.tight_layout()
        rat_img = fig_b64(fig)

    # ------------------------------------------------------------------ Figure 9: combined grouped
    combined_img = ""
    if results_df is not None and not results_df.empty:
        q_cols  = [c for c in results_df.columns if "Q (cfs)" in c]
        rps     = [str(r) for r in results_df["Return Period (yr)"].tolist()]
        n       = len(q_cols)
        xs      = np.arange(len(rps))
        width   = 0.7 / n
        palette = ["#0d2b6e", "#1b5e20", "#b71c1c", "#4a148c"]
        fig, ax = plt.subplots(figsize=(6.5, 3.8))
        for i, col in enumerate(q_cols):
            vals = results_df[col].tolist()
            ax.bar(xs + (i - (n - 1) / 2) * width, vals, width,
                   label=col.replace(" Q (cfs)", ""),
                   color=palette[i % len(palette)], alpha=0.85)
        ax.set_xticks(xs)
        ax.set_xticklabels(rps, fontsize=9)
        ax.set_xlabel("Return Period (yr)", fontsize=9)
        ax.set_ylabel("Peak Discharge (cfs)", fontsize=9)
        ax.set_title("Peak Discharge Comparison — All Methods", fontsize=10, fontweight="bold")
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(axis="y", alpha=0.3, linestyle="--")
        fig.tight_layout()
        combined_img = fig_b64(fig)

    # ------------------------------------------------------------------ Atlas 14 table
    atlas_html = ""
    if atlas14:
        dur_display = [1, 2, 3, 6, 12, 24]
        atlas_rows_data = []
        for rp in RETURN_PERIODS:
            row = {"Return Period (yr)": rp}
            for dur in dur_display:
                try:
                    row[f"{dur}-hr Depth (in)"] = f"{atlas14.depth(dur, rp):.2f}"
                except Exception:
                    pass
            atlas_rows_data.append(row)
        atlas_html = html_table(pd.DataFrame(atlas_rows_data))

    # ------------------------------------------------------------------ CN breakdown table
    lu_cn_html = ""
    if soil_pct and landuse_pct:
        lu_cn_df_r = _build_landuse_cn_table(intersection, soil_pct, landuse_pct, area_sqmi)
        lu_cn_html = html_table(lu_cn_df_r)

    # ------------------------------------------------------------------ DEM metrics block
    dem_block = ""
    if dem_feats:
        dem_block = (
            '<div class="metric-grid">'
            + metric_box("Flow Length (L)", f"{dem_feats['flow_length_ft']:,.0f} ft" if dem_feats.get('flow_length_ft') is not None else "N/A")
            + metric_box("Mean Slope (Y)",  f"{dem_feats.get('mean_slope_pct', 0):.2f}%")
            + metric_box("Min Elevation",   f"{dem_feats.get('elev_min_m', 0):.1f} m")
            + metric_box("Max Elevation",   f"{dem_feats.get('elev_max_m', 0):.1f} m")
            + "</div>"
        )

    # ------------------------------------------------------------------ soil texture table
    soil_texture_html = ""
    if soil_texture:
        soil_texture_html = (
            "<h4>Surface Soil Texture (gSSURGO)</h4>"
            + html_table(pd.DataFrame([{"Texture": t, "%": f"{pct:.1f}"} for t, pct in soil_texture.items()]))
        )

    soil_hsg_table = html_table(
        pd.DataFrame([{"HSG": g, "Area (%)": f"{pct:.1f}"} for g, pct in soil_pct.items()])
    ) if soil_pct else "<p>Not available.</p>"

    lu_table = html_table(
        pd.DataFrame([{"Land Use": lu, "Area (%)": f"{pct:.1f}"}
                      for lu, pct in sorted(landuse_pct.items(), key=lambda x: -x[1])])
    ) if landuse_pct else "<p>Not available.</p>"

    cn_table_html       = html_table(cn_df)       if cn_df is not None       else ""
    rat_table_html      = html_table(rational_df) if rational_df is not None else ""
    combined_table_html = html_table(results_df)  if results_df is not None  else ""

    coord_str = (f"{lat:.5f}°N, {abs(lon):.5f}°W" if lat and lon else "N/A")

    # ------------------------------------------------------------------ assemble HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LID Peak Runoff Analysis Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#222;background:#fff;max-width:960px;margin:0 auto;padding:36px 48px}}
h1{{font-size:24px;color:#fff;background:#0d2b6e;padding:20px 28px;border-radius:6px;margin-bottom:4px;letter-spacing:0.3px}}
.subtitle{{font-size:12px;color:#666;padding:6px 2px 26px}}
h2{{font-size:14px;color:#0d2b6e;border-bottom:2px solid #0d2b6e;padding-bottom:5px;margin:32px 0 12px;text-transform:uppercase;letter-spacing:0.6px}}
h3{{font-size:13px;color:#1565c0;margin:16px 0 7px;font-weight:bold}}
h4{{font-size:12px;color:#444;margin:12px 0 5px}}
p{{line-height:1.7;margin:5px 0}}
ul{{line-height:1.9;padding-left:22px;margin:8px 0}}
table{{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:12px}}
thead th{{background:#0d2b6e;color:#fff;padding:8px 11px;text-align:left;font-weight:600}}
tbody td{{padding:6px 11px;border-bottom:1px solid #e4e8f0}}
tbody tr:nth-child(even){{background:#f4f7fb}}
tbody tr:hover{{background:#e8f0fe}}
.metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}}
.metric-grid-2{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:14px 0}}
.metric{{background:#f0f4fa;border-left:4px solid #0d2b6e;padding:11px 14px;border-radius:4px}}
.metric-label{{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:3px}}
.metric-value{{font-size:19px;font-weight:bold;color:#0d2b6e}}
figure{{text-align:center;margin:18px auto}}
img{{max-width:100%;border-radius:4px;box-shadow:0 1px 5px rgba(0,0,0,.13)}}
figcaption{{font-size:11px;color:#777;font-style:italic;margin-top:6px}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:28px;align-items:start;margin:10px 0}}
.note{{background:#fff8e1;border-left:4px solid #ffc107;padding:10px 14px;border-radius:4px;margin:12px 0;font-size:12px;line-height:1.6}}
.info{{background:#e3f2fd;border-left:4px solid #1565c0;padding:10px 14px;border-radius:4px;margin:12px 0;font-size:12px;line-height:1.6}}
.footer{{margin-top:52px;padding-top:14px;border-top:1px solid #ddd;font-size:11px;color:#aaa;text-align:center}}
@media print{{h2{{page-break-before:always}} h2:first-of-type{{page-break-before:avoid}} .no-print{{display:none}}}}
</style>
</head>
<body>

<h1>LID Peak Runoff Analysis Report</h1>
<p class="subtitle">
  Generated: {today} &nbsp;|&nbsp;
  Pour Point: {coord_str} &nbsp;|&nbsp;
  Tool: LID Peak Runoff Tool (NRCS TR-55 + Rational Method)
</p>

<!-- ===== 1. WATERSHED OVERVIEW ===== -->
<h2>1. Watershed Overview</h2>
<div class="two-col">
  <div>
    <div class="metric-grid-2">
      {metric_box("Watershed Area", f"{area_sqmi:.3f} mi²")}
      {metric_box("Watershed Area", f"{area_acres:.1f} ac")}
    </div>
    <h4>Pour Point</h4>
    <p>{f"Latitude: {lat:.5f}°N" if lat else "N/A"}<br>{f"Longitude: {lon:.5f}°W" if lon else "N/A"}</p>
  </div>
  <div>
    {img_tag(ws_img, "Figure 1. Watershed boundary delineated via USGS StreamStats.", "100%")}
  </div>
</div>

<!-- ===== 2. WATERSHED PROPERTIES ===== -->
<h2>2. Watershed Properties</h2>
<div class="metric-grid">
  {metric_box("Time of Concentration", f"{tc * 60:.1f} min")}
  {metric_box("Composite CN", f"{CN:.1f}")}
  {metric_box("Composite C", f"{C:.3f}")}
  {metric_box("Design Storm Duration", f"{storm_dur} hr")}
</div>
{"<h3>DEM-derived Features (USGS 3DEP 1/3 arc-sec)</h3>" + dem_block if dem_feats else ""}
<div class="info">
  <strong>Time of Concentration (Tc)</strong> = {tc * 60:.1f} min drives both methods:
  the Rational Method uses Atlas&nbsp;14 intensity at duration&nbsp;=&nbsp;Tc,
  and the CN method routes incremental runoff through the SCS unit hydrograph (tp&nbsp;=&nbsp;dt/2&nbsp;+&nbsp;0.6·Tc).
</div>

<!-- ===== 3. SOIL COMPOSITION ===== -->
<h2>3. Soil Composition</h2>
<div class="two-col">
  <div>
    <h3>Hydrologic Soil Group — SSURGO</h3>
    {soil_hsg_table}
    {soil_texture_html}
  </div>
  <div>
    {img_tag(soil_img, "Figure 5. Hydrologic Soil Group area distribution.", "100%")}
  </div>
</div>

<!-- ===== 4. LAND USE COMPOSITION ===== -->
<h2>4. Land Use Composition</h2>
<div class="two-col">
  <div>
    <h3>NLCD 2024 Land Cover</h3>
    {lu_table}
  </div>
  <div>
    {img_tag(lu_img, "Figure 6. Land use distribution by area percentage.", "100%")}
  </div>
</div>

<!-- ===== 5. RUNOFF PARAMETER DERIVATION ===== -->
<h2>5. Runoff Parameter Derivation</h2>
<h3>Land Use × Soil HSG — CN and C Breakdown</h3>
{lu_cn_html if lu_cn_html else "<p>Not available.</p>"}
<div class="note">
  Composite CN = <strong>{CN:.1f}</strong> and Composite C = <strong>{C:.3f}</strong>
  are area-weighted averages computed from the
  {"spatial pixel-level NLCD × SSURGO intersection" if intersection else "marginal land use and soil HSG distributions"}.
  AMC&nbsp;II (average antecedent moisture) is assumed.
</div>

<!-- ===== 6. ATLAS 14 PRECIPITATION ===== -->
<h2>6. NOAA Atlas 14 Precipitation Frequency</h2>
<p>Point precipitation depths (inches) at the watershed centroid from NOAA Atlas 14.</p>
{atlas_html if atlas_html else "<p>Not available.</p>"}

<!-- ===== 7. CN METHOD ===== -->
<h2>7. CN Method Peak Discharge (NRCS TR-55)</h2>
<div class="info">
  <strong>Formula:</strong> q<sub>p</sub> = peak of [incremental runoff × SCS unit hydrograph convolution]<br>
  SCS dimensionless UH: tp = dt/2 + 0.6·Tc = {(0.25/2 + 0.6*tc):.2f} hr, qp = 484·A/tp.<br>
  Incremental runoff from the central {storm_dur}-hr window of the SCS Type&nbsp;II mass curve, scaled to Atlas&nbsp;14 depth.<br>
  A = {area_sqmi:.3f}&nbsp;mi² — watershed area.
</div>
{cn_table_html if cn_table_html else "<p>Results not available — complete Step 4 first.</p>"}
{img_tag(cn_img, f"Figure 7. CN method peak discharge by return period ({storm_dur}-hr design storm, SCS UH convolution).", "80%")}

<!-- ===== 8. RATIONAL METHOD ===== -->
<h2>8. Rational Method Peak Discharge</h2>
<div class="info">
  <strong>Formula:</strong> Q = C &times; I &times; A<br>
  C = {C:.3f} — composite runoff coefficient (area-weighted from NLCD land use).<br>
  I (in/hr) — Atlas&nbsp;14 rainfall intensity at duration = Tc&nbsp;=&nbsp;{tc * 60:.0f}&nbsp;min.<br>
  A = {area_acres:.1f}&nbsp;ac — watershed area.
  <br><em>Note: Rational Method is most reliable for watersheds &lt; 640 ac (1 mi²).</em>
</div>
{rat_table_html if rat_table_html else "<p>Results not available — complete Step 4 first.</p>"}
{img_tag(rat_img, f"Figure 8. Rational method peak discharge by return period (intensity at Tc = {tc * 60:.0f} min).", "80%")}

<!-- ===== 9. COMBINED SUMMARY ===== -->
<h2>9. Peak Discharge Summary — All Methods</h2>
{combined_table_html if combined_table_html else "<p>Results not available.</p>"}
{img_tag(combined_img, "Figure 9. Side-by-side peak discharge comparison across all methods.", "95%")}

<!-- ===== 10. ASSUMPTIONS & LIMITATIONS ===== -->
<h2>10. Assumptions and Limitations</h2>
<ul>
  <li>Watershed delineated via USGS StreamStats using the regional stream network and elevation products available for the selected state/territory.</li>
  <li>Soil hydrologic group from SSURGO (gSSURGO, USDA-NRCS). Land use from NLCD 2024.</li>
  <li>Precipitation from NOAA Atlas 14 point precipitation frequency estimates. 90% confidence bounds not shown.</li>
  <li>CN method routes incremental SCS Type II runoff through the SCS dimensionless unit hydrograph (NEH-4 Table 16-2).</li>
  <li>Rational Method (Q = CIA) is most reliable for small, highly impervious watersheds (&lt; 640 ac).</li>
  <li>AMC II (average antecedent moisture conditions) assumed throughout.</li>
  <li>Results represent peak discharge only. Channel routing and downstream attenuation are not modeled.</li>
  <li>Basemap tiles: CartoDB Positron via contextily (requires internet connection at report generation time).</li>
</ul>

<div class="footer">
  LID Peak Runoff Tool &nbsp;&bull;&nbsp; Generated {today} &nbsp;&bull;&nbsp;
  NRCS TR-55 + Rational Method &nbsp;&bull;&nbsp; NOAA Atlas 14
</div>
</body>
</html>"""

    return html.encode("utf-8")


def _step_badge(n: int, label: str):
    current = st.session_state["step"]
    if n < current:
        # Completed step — clickable, navigates back without resetting data
        if st.button(
            f"✓  {n}. {label}",
            key=f"nav_step_{n}",
            help=f"Return to Step {n}",
            use_container_width=True,
        ):
            st.session_state["step"] = n
            st.rerun()
    elif n == current:
        st.markdown(
            f'<span style="background:#007bff;color:white;border-radius:50%;'
            f'padding:2px 8px;margin-right:6px;font-weight:bold">{n}</span> '
            f'**{label}**',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<span style="background:#aaa;color:white;border-radius:50%;'
            f'padding:2px 8px;margin-right:6px;font-weight:bold">{n}</span> '
            f'**{label}**',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Step 1 map — wrapped in @st.fragment when available so that map clicks
# only rerun the fragment (no full-page reload / viewport reset).
# Falls back to a plain function on older Streamlit versions.
# ---------------------------------------------------------------------------

_fragment = getattr(st, "fragment", lambda f: f)


@_fragment
def _render_step1_map():
    """
    Interactive point-selection map for Step 1.

    Follows the same pattern as the reference implementation:
      1. Build the map — if a point is already selected, center on it and
         draw the marker before st_folium renders.
      2. Read last_clicked from map_data.
      3. If a new click arrived, store it and call st.rerun() so the NEXT
         render builds the map with the marker already in place.

    This avoids the one-rerun lag where the marker appears a click late.
    """
    # Center on the selected point once one exists; otherwise last viewport
    if st.session_state["selected_lat"] is not None:
        _map_center = [st.session_state["selected_lat"], st.session_state["selected_lon"]]
    else:
        _map_center = st.session_state.get("map1_center") or USA_CENTER
    _map_zoom = st.session_state.get("map1_zoom") or DEFAULT_ZOOM

    # ---- Build Folium map ------------------------------------------------
    m = folium.Map(location=_map_center, zoom_start=_map_zoom, tiles="OpenStreetMap")
    MousePosition().add_to(m)

    # Marker drawn into the map BEFORE st_folium renders it, so it's
    # visible on the same rerun that set the selected point.
    if st.session_state["selected_lat"] is not None:
        _lat = st.session_state["selected_lat"]
        _lon = st.session_state["selected_lon"]
        folium.Marker(
            location=[_lat, _lon],
            tooltip="Selected Point",
            popup=folium.Popup(
                f"<b>Selected point</b><br>{_lat:.5f}&deg;N, {_lon:.5f}&deg;",
                max_width=200,
            ),
            icon=folium.Icon(color="red", icon="info-sign"),
        ).add_to(m)

    # ---- Render ----------------------------------------------------------
    map_data = st_folium(m, use_container_width=True, height=520, key="map_step1")

    # ---- Capture click → store → rerun so marker appears immediately -----
    if map_data and map_data.get("last_clicked"):
        # Snapshot viewport before rerun so the map rebuilds at same zoom
        if map_data.get("center"):
            c = map_data["center"]
            st.session_state["map1_center"] = [c["lat"], c["lng"]]
        if map_data.get("zoom"):
            st.session_state["map1_zoom"] = map_data["zoom"]
        st.session_state["selected_lat"] = map_data["last_clicked"]["lat"]
        st.session_state["selected_lon"] = map_data["last_clicked"]["lng"]
        st.rerun()

    # ---- Controls shown only after a point is selected -------------------
    if st.session_state["selected_lat"] is not None:
        lat = st.session_state["selected_lat"]
        lon = st.session_state["selected_lon"]
        st.success(f"Selected Point: Latitude = {lat:.6f}, Longitude = {lon:.6f}")

        col_a, col_b = st.columns([5, 1])
        with col_a:
            if st.button("Proceed to Delineation \u2192", type="primary", key="proceed_step1", use_container_width=True):
                st.session_state["step"] = 2
                try:
                    st.rerun(scope="app")
                except TypeError:
                    st.rerun()
        with col_b:
            if st.button("Clear", key="clear_point_step1", use_container_width=True):
                st.session_state["selected_lat"] = None
                st.session_state["selected_lon"] = None


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

def main() -> None:
    _init_state()

    st.title("LID Peak Runoff Tool")
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
            with st.expander("Upload watershed boundary (skip StreamStats)", expanded=True):
                st.caption(
                    "Upload a watershed boundary polygon file. "
                    "Supported formats: GeoJSON (.geojson/.json), KML (.kml), KMZ (.kmz)."
                )
                uploaded = st.file_uploader(
                    "Choose a watershed boundary file",
                    type=["geojson", "json", "kml", "kmz"],
                    key="ws_upload",
                )

                if uploaded is not None:
                    try:
                        fc, source_fmt = _watershed_fc_from_upload(uploaded)

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
                            _upload_state, _upload_state_live = reverse_geocode_state(lat_upload, lon_upload)

                            # Build watershed dict compatible with the rest of the app
                            watershed_from_upload = {
                                "workspace_id": "N/A",
                                "geojson": fc,
                                "area_sqmi": round(area_sqmi, 4),
                                "region": _upload_state.get("state_abbrev") if _upload_state_live else None,
                                "request_url": f"(uploaded {source_fmt}: {uploaded.name})",
                            }

                            st.success(
                                f"Loaded **{uploaded.name}** ({source_fmt}) — "
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

                    except (json.JSONDecodeError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
                        st.error(f"Could not parse uploaded watershed file: {exc}")

            st.markdown("---")
            st.caption("— or delineate from a stream point below —")

            # ---------------------------------------------------------------
            # Option B — Click / type a stream point (existing flow)
            # ---------------------------------------------------------------
            # --- Manual lat/lon input ---
            with st.form("manual_coords", clear_on_submit=False):
                ci, cj = st.columns(2)
                manual_lat = ci.text_input("Latitude", placeholder="e.g. 39.0997")
                manual_lon = cj.text_input("Longitude", placeholder="e.g. -94.5786")
                submitted = st.form_submit_button("Use These Coordinates")

            if submitted:
                try:
                    lat_val = float(manual_lat)
                    lon_val = float(manual_lon)
                    if not (-90.0 <= lat_val <= 90.0) or not (-180.0 <= lon_val <= 180.0):
                        st.warning("Coordinates must be valid decimal latitude/longitude values.")
                    else:
                        st.session_state["selected_lat"] = lat_val
                        st.session_state["selected_lon"] = lon_val
                except ValueError:
                    st.error("Enter valid decimal numbers for latitude and longitude.")

            # --- Map click (fragment-isolated) ---
            _render_step1_map()

        # -----------------------------------------------------------------------
        # Step 2 — Delineate watershed
        # -----------------------------------------------------------------------
        elif st.session_state["step"] == 2:
            lat = st.session_state["selected_lat"]
            lon = st.session_state["selected_lon"]
            st.subheader("Step 2 — Watershed Delineation")

            # ── Delineate via StreamStats automatically on entering Step 2 ─────
            if st.session_state["watershed"] is None:
                st.caption(f"Pour point: ({lat:.5f}, {lon:.5f})")
                with st.spinner("Calling USGS StreamStats API..."):
                    try:
                        result = delineate_watershed(lat, lon)
                        st.session_state["watershed"]   = result
                        basin_chars = get_basin_characteristics(
                            result["workspace_id"],
                            region=result.get("region", "OK"),
                        )
                        st.session_state["basin_chars"] = basin_chars
                    except Exception as e:
                        st.error(f"StreamStats delineation failed: {e}")
                        st.stop()
                st.rerun()

            # ── Results display (watershed already in session state) ────────────
            result     = st.session_state["watershed"]
            basin_chars = st.session_state["basin_chars"]
            region_code = result.get("region")
            region_name = None
            if region_code and lat is not None and lon is not None:
                _state_info, _state_live = reverse_geocode_state(lat, lon)
                if _state_live:
                    region_name = _state_info.get("state_name")

            if result.get("request_url"):
                st.caption(result["request_url"])
            if region_code:
                st.caption(
                    f"StreamStats region: **{region_code}**"
                    + (f" ({region_name})" if region_name else "")
                )

            area_sqmi  = result.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
            tlag       = basin_chars.get("TLAG", None)
            tc_from_api = tlag_to_tc(tlag) if tlag else None

            st.success("Watershed delineated via USGS StreamStats.")

            c1, c2 = st.columns(2)
            c1.metric("Area (sq mi)", f"{area_sqmi:.3f}" if area_sqmi else "N/A")
            c1.metric("Area (acres)", f"{sqmi_to_acres(area_sqmi):.1f}" if area_sqmi else "N/A")
            if tc_from_api:
                c2.metric("Lag Time (min)", f"{tlag * 60:.1f}")
                c2.metric("Tc (min)", f"{tc_from_api * 60:.1f}")

            # Set Tc — from StreamStats if available, otherwise default 0.5 hr
            tc_value = round(tc_from_api, 2) if tc_from_api else st.session_state.get("tc_hr", 0.5)
            st.session_state["tc_hr"] = tc_value

            from shapely.geometry import shape as _shape_s2
            _ws_geom_s2 = _shape_s2(
                result["geojson"]["features"][0]["geometry"]
                if result["geojson"].get("type") == "FeatureCollection"
                else result["geojson"].get("geometry", result["geojson"])
            )
            # Fit the map to the watershed bounding box so it fills the viewport
            # regardless of watershed size — no hardcoded zoom level needed.
            _bounds = _ws_geom_s2.bounds  # (minx, miny, maxx, maxy) = (W, S, E, N)
            _sw = [_bounds[1], _bounds[0]]   # [south, west]
            _ne = [_bounds[3], _bounds[2]]   # [north, east]
            _centroid_s2 = _ws_centroid(_ws_geom_s2)
            m2 = folium.Map(location=_centroid_s2, zoom_start=11)
            m2.fit_bounds([_sw, _ne], padding=[20, 20])
            folium.GeoJson(
                result["geojson"],
                style_function=lambda _: {"color": "#1a6bb0", "fillOpacity": 0.1, "weight": 2},
            ).add_to(m2)
            folium.Marker([lat, lon], popup="Pour Point", icon=folium.Icon(color="red")).add_to(m2)
            st_folium(m2, use_container_width=True, height=400, key="map_step2", returned_objects=[])

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

            if st.session_state.get("atlas14") is None:
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

            if st.session_state.get("soil_pct") is None:
                with st.spinner("Loading spatial data..."):
                    try:
                        intersection, intersection_live = fetch_landuse_soil_intersection(watershed["geojson"])
                        st.session_state["lu_soil_intersection"] = intersection
                        st.session_state["intersection_live"] = intersection_live

                        if intersection_live and intersection:
                            soil_pct    = intersection_to_soil_pct(intersection)
                            landuse_pct = intersection_to_landuse_pct(intersection)
                            soil_live   = True
                            lu_live     = True
                        else:
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

            if st.session_state.get("soil_texture") is None:
                with st.spinner("Loading soil texture..."):
                    soil_texture, soil_texture_live = fetch_soil_texture(watershed["geojson"])
                    st.session_state["soil_texture"] = soil_texture
                    st.session_state["soil_texture_live"] = soil_texture_live

            if st.session_state.get("soil_gdf") is None:
                with st.spinner("Loading soil polygons for mapping..."):
                    soil_gdf, _ = fetch_soil_geodataframe(watershed["geojson"])
                    st.session_state["soil_gdf"] = soil_gdf

            if st.session_state.get("nlcd_arr") is None:
                with st.spinner("Loading land cover data..."):
                    nlcd_arr, _ = fetch_nlcd_array(watershed["geojson"])
                    st.session_state["nlcd_arr"] = nlcd_arr

            if st.session_state.get("usgs_flows") is None:
                _ws_id = watershed.get("workspace_id")
                _region = watershed.get("region", "OK")
                if _ws_id and _ws_id != "N/A":
                    with st.spinner("Fetching regression flows..."):
                        st.session_state["usgs_flows"] = get_peak_flow_regression(_ws_id, region=_region)
                else:
                    st.session_state["usgs_flows"] = []

            if not st.session_state.get("dem_fetch_done"):
                with st.spinner("Downloading DEM and computing flow length / slope / elevation…"):
                    dem_feats, dem_live = fetch_dem_features(
                        watershed["geojson"],
                        pour_lat=st.session_state.get("selected_lat"),
                        pour_lon=st.session_state.get("selected_lon"),
                    )
                    if dem_live and dem_feats:
                        st.session_state["dem_features"] = dem_feats
                        st.session_state["lag_L_ft"]     = dem_feats["flow_length_ft"]
                        st.session_state["lag_Y_pct"]    = dem_feats["mean_slope_pct"]
                        if dem_feats.get("_flow_length_warning"):
                            st.warning(
                                f"DEM loaded — elevation map and slope available. "
                                f"Flow length could not be computed "
                                f"({dem_feats['_flow_length_warning']}). "
                                f"SCS Lag Tc option will be unavailable; enter Tc manually."
                            )
                    else:
                        st.session_state["dem_features"] = None
                        _dem_err = dem_feats.get("_error", "unknown error") if dem_feats else "no response"
                        st.warning(f"DEM features unavailable — {_dem_err}")
                    st.session_state["dem_fetch_done"] = True

            # Pull everything from session state for display
            atlas14     = st.session_state["atlas14"]
            soil_pct    = st.session_state["soil_pct"]
            landuse_pct = st.session_state["landuse_pct"]
            usgs_flows  = st.session_state.get("usgs_flows") or []

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

            # --- DEM features summary ---
            dem_feats = st.session_state.get("dem_features")
            if dem_feats and "_error" not in dem_feats:
                st.markdown("---")
                st.markdown("### DEM-derived Watershed Features")
                dc1, dc2, dc3, dc4 = st.columns(4)
                dc1.metric("Flow Length (L)",
                           f"{dem_feats['flow_length_ft']:,.0f} ft" if dem_feats.get('flow_length_ft') is not None else "N/A",
                           help="Longest D8 flow path to outlet within StreamStats boundary.")
                dc2.metric("Mean Slope (Y)", f"{dem_feats['mean_slope_pct']:.2f}%")
                dc3.metric("Min Elevation", f"{dem_feats['elev_min_m']:.1f} m")
                dc4.metric("Max Elevation", f"{dem_feats['elev_max_m']:.1f} m",
                           delta=f"Δ {dem_feats['elev_max_m'] - dem_feats['elev_min_m']:.1f} m relief",
                           delta_color="off")
            elif dem_feats and "_error" in dem_feats:
                st.markdown("---")
                st.info(f"⚠️ DEM features unavailable: {dem_feats.get('_error', 'Unknown error')}")
                if dem_feats.get("_note"):
                    st.caption(dem_feats["_note"])

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

            # DEM elevation map
            if dem_feats and "_error" not in dem_feats:
                with st.expander("DEM Elevation", expanded=True):
                    st_folium(render_dem_map(dem_feats, ws_geom_map),
                              use_container_width=True, height=400,
                              key="map_s3_dem", returned_objects=[])
            elif dem_feats and "_error" in dem_feats:
                with st.expander("DEM Elevation", expanded=False):
                    st.info(f"ℹ️ {dem_feats.get('_note', 'DEM data unavailable in this environment.')}")

            with st.expander("Hydrologic Soil Group", expanded=True):
                if soil_gdf is not None and not soil_gdf.empty:
                    st_folium(render_hsg_map(soil_gdf, ws_geom_map),
                              use_container_width=True, height=400,
                              key="map_s3_hsg", returned_objects=[])
                else:
                    st.info("Soil polygon data unavailable.")

            with st.expander("Surface Soil Texture", expanded=True):
                if soil_gdf is not None and not soil_gdf.empty:
                    st_folium(render_texture_map(soil_gdf, ws_geom_map),
                              use_container_width=True, height=400,
                              key="map_s3_tex", returned_objects=[])
                else:
                    st.info("Soil polygon data unavailable.")

            with st.expander("Land Cover", expanded=True):
                if nlcd_arr is not None:
                    st_folium(render_nlcd_map(nlcd_arr, ws_geom_map),
                              use_container_width=True, height=400,
                              key="map_s3_nlcd", returned_objects=[])
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

            _ss_area   = watershed.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
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

            # --- Watershed area (from StreamStats) ---
            area_sqmi = _ss_area or 0.0
            area_acres = sqmi_to_acres(area_sqmi)
            st.session_state["area_sqmi_used"] = area_sqmi

            # --- Tc source selector ---
            _tc_options: dict[str, float | None] = {}
            _lag_L = st.session_state.get("lag_L_ft")
            _lag_Y = st.session_state.get("lag_Y_pct")
            if _lag_L and np.isfinite(_lag_L) and _lag_Y and np.isfinite(_lag_Y) and CN > 0:
                _dem_tc = tc_scs_lag(_lag_L, _lag_Y, CN)
                st.session_state["dem_tc_hr"] = _dem_tc
                _tc_options[
                    f"NRCS SCS Lag  (L={_lag_L:,.0f} ft, Y={_lag_Y:.2f}%, CN={CN:.1f})  →  {_dem_tc * 60:.1f} min"
                ] = _dem_tc
                _kirpich_tc = tc_kirpich(_lag_L, _lag_Y)
                _tc_options[
                    f"Kirpich  (L={_lag_L:,.0f} ft, S={_lag_Y / 100.0:.4f} ft/ft)  →  {_kirpich_tc * 60:.1f} min"
                ] = _kirpich_tc
            _tc_options["Manual input"] = None

            _tc_label = st.selectbox(
                "Time of Concentration (Tc) source",
                list(_tc_options.keys()),
                help=(
                    "**NRCS SCS Lag** uses the TR-55 lag equation: "
                    "Tc = (L^0.8 × (S+1)^0.7) / (1440 × Y^0.5), "
                    "where L is the longest D8 flow path from 3DEP and Y is the mean slope from 3DEP. "
                    "**Kirpich** uses: Tc(min) = 0.0078 × L^0.77 × S^-0.385, "
                    "where S is slope in ft/ft (Y/100). "
                    "**Manual** lets you enter a value directly in minutes."
                ),
            )
            if _tc_options[_tc_label] is None:
                tc_min_input = st.number_input(
                    "Tc (minutes)", min_value=1.0,
                    value=float((st.session_state.get("tc_hr") or 0.5) * 60), step=1.0,
                )
                tc = tc_min_input / 60.0
            else:
                tc = _tc_options[_tc_label]
            st.session_state["tc_hr"] = tc
            st.caption(f"**Tc = {tc * 60:.1f} min**  used for peak-flow calculations below.")

            # Storm duration selector — CN method
            storm_duration_hr = st.number_input(
                "Storm Duration (hours)",
                min_value=0.5,
                max_value=72.0,
                value=float(st.session_state.get("storm_duration_hr") or 24.0),
                step=0.5,
                help=(
                    "Duration of the design storm in hours. Atlas 14 depth is interpolated "
                    "for this exact duration. Standard SCS TR-55 uses 24 hr."
                ),
                key="storm_duration_input",
            )
            st.session_state["storm_duration_hr"] = storm_duration_hr


            # Land use × HSG breakdown
            lu_cn_df = _build_landuse_cn_table(intersection, soil_pct, landuse_pct, area_sqmi)
            if not lu_cn_df.empty:
                with st.expander("Land Use × Soil CN Breakdown", expanded=True):
                    if intersection:
                        st.caption("Each row is an exact spatial (land use, HSG) combination from the NLCD × SSURGO pixel intersection.")
                    else:
                        st.caption("Spatial intersection unavailable — CN shown is the soil-weighted composite per land use class.")
                    st.dataframe(lu_cn_df, hide_index=True, use_container_width=True)

            # Build results per return period — three separate DataFrames
            usgs_by_rp = {r["return_period"]: r["flow_cfs"] for r in usgs_flows}

            _depth_col  = f"Atlas 14 {storm_duration_hr}-hr Depth (in)"
            _intens_col = f"Atlas 14 {tc * 60:.0f}-min Intensity (in/hr)"

            cn_rows, rational_rows, combined_rows = [], [], []
            for rp in RETURN_PERIODS:
                depth_D      = atlas14.depth(storm_duration_hr, rp)
                intensity_tc = atlas14.intensity(tc, rp)

                q_cn       = scs_uh_peak_flow(CN, depth_D, area_sqmi, tc, storm_duration_hr)
                q_rational = rational_peak_flow(C, intensity_tc, area_acres)

                # Runoff depth from the SCS storm table (last row)
                _tbl = build_storm_table(depth_D, storm_duration_hr, CN)
                q_runoff = _tbl[-1]["Accumulated Effective Runoff (in)"]

                cn_rows.append({
                    "Return Period (yr)": rp,
                    _depth_col:          round(depth_D, 2),
                    "Runoff Depth (in)": round(q_runoff, 2),
                    "CN Peak Q (cfs)":   q_cn,
                })
                rational_rows.append({
                    "Return Period (yr)": rp,
                    _intens_col:          round(intensity_tc, 3),
                    "Rational Peak Q (cfs)": q_rational,
                })
                combined_row = {
                    "Return Period (yr)":     rp,
                    "CN Peak Q (cfs)":        q_cn,
                    "Rational Peak Q (cfs)":  q_rational,
                }
                if rp in usgs_by_rp:
                    combined_row["USGS Regression Q (cfs)"] = round(usgs_by_rp[rp], 1)
                combined_rows.append(combined_row)

            cn_df       = pd.DataFrame(cn_rows)
            rational_df = pd.DataFrame(rational_rows)
            results_df  = pd.DataFrame(combined_rows)

            st.session_state["cn_df"]       = cn_df
            st.session_state["rational_df"] = rational_df
            st.session_state["results_df"]  = results_df

            st.success("Calculations complete.")

            st.markdown("**CN Method — Peak Discharge by Return Period**")
            st.dataframe(cn_df, hide_index=True, use_container_width=True)

            with st.expander("SCS Storm Analysis", expanded=False):
                _rp_sel = st.selectbox("Return Period", RETURN_PERIODS, key="storm_analysis_rp")
                _P_D_sa = atlas14.depth(storm_duration_hr, _rp_sel)
                _sa     = scs_uh_hydrograph(CN, _P_D_sa, area_sqmi, tc, storm_duration_hr)

                tab_tbl, tab_hyd = st.tabs(["Storm Table", "Hydrograph"])

                with tab_tbl:
                    st.caption(
                        f"SCS Type II storm — central {storm_duration_hr}-hr window, "
                        f"P = {_P_D_sa:.2f} in, CN = {CN:.1f}"
                    )
                    st.dataframe(
                        pd.DataFrame(_sa["storm_table"]),
                        hide_index=True, use_container_width=True,
                    )

                with tab_hyd:
                    _fig_hyd = go.Figure()
                    _fig_hyd.add_trace(
                        go.Scatter(
                            x=_sa["drh_times"], y=_sa["drh_flow"],
                            name="Direct Runoff (cfs)",
                            line=dict(color="#1f77b4", width=2),
                        )
                    )
                    _fig_hyd.add_vline(
                        x=_sa["peak_time"],
                        line=dict(color="gray", dash="dot", width=1),
                        annotation_text=f"Peak {_sa['peak_flow']:.0f} cfs @ {_sa['peak_time']:.2f} hr",
                        annotation_position="top right",
                    )
                    _fig_hyd.update_layout(
                        height=320,
                        xaxis_title="Time from Storm Start (hr)",
                        yaxis_title="Direct Runoff (cfs)",
                        margin=dict(l=50, r=60, t=50, b=40),
                        showlegend=False,
                    )
                    st.plotly_chart(_fig_hyd, use_container_width=True)
                    st.caption(
                        f"tp = {_sa['tp']:.2f} hr  ·  "
                        f"Peak = {_sa['peak_flow']:.0f} cfs @ {_sa['peak_time']:.2f} hr from storm start"
                    )

            st.markdown("**Rational Method — Peak Discharge by Return Period**")
            st.dataframe(rational_df, hide_index=True, use_container_width=True)

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

            area_sqmi = st.session_state.get("area_sqmi_used") or watershed.get("area_sqmi") or basin_chars.get("DRNAREA", 0)
            tc = st.session_state.get("tc_hr", 1.0)
            CN = composite_cn_from_intersection(intersection) if intersection else composite_cn(soil_pct, landuse_pct)
            C  = composite_c(landuse_pct)

            # Summary metrics
            _dem_feats_s5 = st.session_state.get("dem_features") or {}
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Watershed Area", f"{area_sqmi:.3f} mi²")
            m2.metric("Tc", f"{tc * 60:.1f} min")
            m3.metric("Composite CN", f"{CN:.1f}")
            m4.metric("Composite C", f"{C:.3f}")
            if _dem_feats_s5:
                d1, d2, d3, d4 = st.columns(4)
                d1.metric("Flow Length (L)", f"{_dem_feats_s5['flow_length_ft']:,.0f} ft" if _dem_feats_s5.get('flow_length_ft') is not None else "N/A")
                d2.metric("Mean Slope (Y)", f"{_dem_feats_s5['mean_slope_pct']:.2f}%")
                d3.metric("Min Elevation", f"{_dem_feats_s5['elev_min_m']:.1f} m")
                d4.metric("Max Elevation", f"{_dem_feats_s5['elev_max_m']:.1f} m")

            _cn_raw        = st.session_state.get("cn_df")
            cn_df_s5       = _cn_raw if _cn_raw is not None else pd.DataFrame()
            _rat_raw       = st.session_state.get("rational_df")
            rational_df_s5 = _rat_raw if _rat_raw is not None else pd.DataFrame()
            storm_dur_s5   = st.session_state.get("storm_duration_hr", 24)
            area_acres_s5  = sqmi_to_acres(area_sqmi)

            rp_labels = [str(rp) for rp in RETURN_PERIODS]

            def _plotly_grouped_bar(title, series):
                fig = go.Figure()
                for label, values in series.items():
                    fig.add_trace(go.Bar(name=label, x=rp_labels, y=values))
                fig.update_layout(
                    barmode="group",
                    title=title,
                    xaxis=dict(title="Return Period (yr)", type="category"),
                    yaxis_title="Peak Discharge (cfs)",
                    height=260,
                    margin=dict(l=50, r=20, t=40, b=40),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                )
                return fig

            st.markdown("### Peak Discharge by Return Period")
            tab_combined, tab_cn, tab_rational = st.tabs(["Combined", "CN Method", "Rational Method"])

            with tab_cn:
                st.caption(
                    f"Storm Duration: **{storm_dur_s5} hr**  |  "
                    f"Composite CN: **{CN:.1f}**  |  "
                    f"Area: **{area_sqmi:.3f} mi²**  |  "
                    f"Tc: **{tc * 60:.1f} min**"
                )
                if not cn_df_s5.empty:
                    st.dataframe(cn_df_s5, hide_index=True, use_container_width=True)
                    st.plotly_chart(
                        _plotly_grouped_bar(
                            f"CN Method — {storm_dur_s5}-hr Storm",
                            {"CN Peak Q (cfs)": cn_df_s5["CN Peak Q (cfs)"].tolist()},
                        ),
                        use_container_width=True,
                    )

            with tab_rational:
                st.caption(
                    f"Tc: **{tc * 60:.1f} min**  |  "
                    f"Composite C: **{C:.3f}**  |  "
                    f"Area: **{area_acres_s5:.1f} ac**"
                )
                if not rational_df_s5.empty:
                    st.dataframe(rational_df_s5, hide_index=True, use_container_width=True)
                    st.plotly_chart(
                        _plotly_grouped_bar(
                            "Rational Method",
                            {"Rational Peak Q (cfs)": rational_df_s5["Rational Peak Q (cfs)"].tolist()},
                        ),
                        use_container_width=True,
                    )

            with tab_combined:
                st.dataframe(results_df, hide_index=True, use_container_width=True)
                _q_cols = [c for c in results_df.columns if "Q (cfs)" in c]
                st.plotly_chart(
                    _plotly_grouped_bar(
                        "Peak Discharge — All Methods",
                        {col: results_df[col].tolist() for col in _q_cols},
                    ),
                    use_container_width=True,
                )
                csv_bytes = results_df.to_csv(index=False).encode()
                st.download_button(
                    label="Download Results as CSV",
                    data=csv_bytes,
                    file_name="peak_runoff_results.csv",
                    mime="text/csv",
                )

            with st.expander("Watershed Details"):
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

            st.markdown("---")
            st.markdown("### Download Report")
            st.caption("Generates a self-contained HTML report with all figures and tables. Open in any browser and print to PDF.")
            if st.button("Generate Report", type="primary"):
                with st.spinner("Building report…"):
                    report_bytes = _generate_report_html()
                st.download_button(
                    label="Download HTML Report",
                    data=report_bytes,
                    file_name="peak_runoff_report.html",
                    mime="text/html",
                )
