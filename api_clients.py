"""
api_clients.py — All external API integrations for the LID Peak Runoff Tool.

Functions:
  delineate_watershed()       — USGS StreamStats delineation
  get_basin_characteristics() — USGS StreamStats basin chars (area, Tc)
  get_peak_flow_regression()  — USGS NSS regression peak flows
  fetch_atlas14()             — NOAA Atlas 14 precipitation
  fetch_soil_composition()    — USDA SSURGO hydrologic soil groups (local parquet)
  fetch_soil_texture()        — USDA SSURGO surface soil texture (local gSSURGO GDB)
  fetch_landuse_composition() — NLCD 2024 land use within watershed (local raster)
"""

import json
import csv
import os
import requests
import numpy as np
from shapely.geometry import shape

from reference_data import (
    RETURN_PERIODS,
    NLCD_TO_LANDUSE,
)
from noaa_atlas14 import fetch_idf, IDF


# ---------------------------------------------------------------------------
# Local SSURGO soil data paths
# ---------------------------------------------------------------------------

_LOCAL_SOIL_PARQUET = "/Users/ashishojha/Documents/LID excels/ok_ssurgo_spatial/ok_soilmu.parquet"
_GSSURGO_GDB = "/Users/ashishojha/Documents/LID excels/soil data/gSSURGO_OK.gdb"

# Module-level caches — loaded once per process
_SOIL_GDF = None      # statewide soil polygons GeoDataFrame
_TEX_LOOKUP = None    # mukey (str) → texdesc (str)


# ---------------------------------------------------------------------------
# USGS StreamStats
# ---------------------------------------------------------------------------

_SS_BASE = "https://streamstats.usgs.gov"
_TIMEOUT = 60  # seconds


def delineate_watershed(lat: float, lon: float, region: str = "OK") -> dict:
    """
    Delineate watershed using USGS StreamStats API.

    Returns dict with keys:
      "workspace_id": str
      "geojson": dict  (GeoJSON FeatureCollection of watershed boundary)
      "area_sqmi": float
    Raises RuntimeError on failure.
    """
    url = f"{_SS_BASE}/ss-delineate/v1/delineate/sshydro/{region}"
    params = {
        "lat": lat,
        "lon": lon,
        "includeparameters": "true",
        "includeflowtypes": "false",
        "includefeatures": "true",
        "simplify": "true",
    }
    resp = requests.get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    # Actual response structure:
    # data["bcrequest"]["wsresp"]["workspace_id"]
    # data["bcrequest"]["wsresp"]["featurecollection"] → list of lists
    #   inner list items: {"name": "globalwatershed", "feature": GeoJSON FeatureCollection}
    wsresp = data.get("bcrequest", {}).get("wsresp", {})
    workspace_id = wsresp.get("workspace_id", "")

    boundary_geojson = None
    area_sqmi = None

    fc_outer = wsresp.get("featurecollection", [])
    # featurecollection is a list of lists; flatten one level
    feature_items = []
    for item in fc_outer:
        if isinstance(item, list):
            feature_items.extend(item)
        elif isinstance(item, dict):
            feature_items.append(item)

    for feat in feature_items:
        if feat.get("name") == "globalwatershed":
            boundary_geojson = feat.get("feature")
            # Extract area from geometry properties (Shape_Area in sq meters)
            features_list = boundary_geojson.get("features", []) if boundary_geojson else []
            if features_list:
                shape_area_sqm = features_list[0].get("properties", {}).get("Shape_Area", 0)
                if shape_area_sqm:
                    area_sqmi = shape_area_sqm / 2_589_988.11  # sq m → sq mi
            break

    if boundary_geojson is None:
        raise RuntimeError("StreamStats did not return a watershed boundary.")

    return {
        "workspace_id": workspace_id,
        "geojson": boundary_geojson,
        "area_sqmi": area_sqmi,
        "request_url": resp.url,
    }


def get_basin_characteristics(workspace_id: str, region: str = "OK") -> dict:
    """
    Retrieve basin characteristics from USGS StreamStats.

    Returns dict of {parameter_code: value}, e.g.:
      {"DRNAREA": 1.23, "TLAG": 0.75, "SLOPE": 0.02, ...}

    Returns empty dict if workspace_id is invalid ('N/A') or the call fails —
    the delineation endpoint does not always produce a usable workspace.
    """
    if not workspace_id or workspace_id == "N/A":
        return {}

    url = f"{_SS_BASE}/ss-hydro/v1/basin-characteristics/calculate"
    payload = {
        "regressionRegions": [],
        "workspaceID": workspace_id,
        "characteristicIDs": [],
    }
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return {}

    data = resp.json()
    result = {}
    for param in data.get("parameters", []):
        code = param.get("code", "")
        value = param.get("value")
        if code and value is not None:
            try:
                result[code.upper()] = float(value)
            except (TypeError, ValueError):
                pass
    return result


def get_peak_flow_regression(workspace_id: str, region: str = "OK") -> list[dict]:
    """
    Get USGS regression-based peak flow estimates via NSS.

    Returns list of dicts:
      [{"return_period": 2, "flow_cfs": 123.4, "lower_ci": 90.0, "upper_ci": 170.0}, ...]

    Returns empty list if workspace_id is invalid ('N/A') — the delineation endpoint
    does not produce a usable workspace for regression queries.
    """
    if not workspace_id or workspace_id == "N/A":
        return []

    # First get regression regions for this workspace
    url_regions = f"{_SS_BASE}/ss-delineate/v1/regression-regions/{region}"
    resp = requests.get(url_regions, params={"workspaceID": workspace_id}, timeout=_TIMEOUT)
    resp.raise_for_status()
    region_data = resp.json()
    region_ids = [r["id"] for r in region_data.get("regressionRegions", [])]

    if not region_ids:
        return []

    # Get scenarios
    url_scenarios = f"{_SS_BASE}/nssservices/scenarios/estimate"
    payload = {
        "workspaceID": workspace_id,
        "regressionRegions": [{"id": rid} for rid in region_ids],
    }
    resp = requests.post(url_scenarios, json=payload, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for scenario in data:
        for rr in scenario.get("regressionRegions", []):
            for stat in rr.get("results", []):
                name = stat.get("name", "")
                # Peak flows are named like "Peak discharge for 2-year recurrence interval"
                for rp in RETURN_PERIODS:
                    if f"{rp}-year" in name or f"{rp} year" in name:
                        try:
                            results.append({
                                "return_period": rp,
                                "flow_cfs": float(stat["value"]),
                                "lower_ci": float(stat.get("predictionInterval", {}).get("lower", 0)),
                                "upper_ci": float(stat.get("predictionInterval", {}).get("upper", 0)),
                            })
                        except (KeyError, TypeError, ValueError):
                            pass

    # Deduplicate and sort
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x["return_period"]):
        if r["return_period"] not in seen:
            seen.add(r["return_period"])
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# NOAA Atlas 14
# ---------------------------------------------------------------------------

def fetch_atlas14(lat: float, lon: float) -> tuple[IDF, bool]:
    """
    Fetch NOAA Atlas 14 precipitation frequency data.

    Returns (IDF object, live: bool).
    The IDF object supports:
      idf.intensity(duration_hr, ari_yr)  → in/hr   (use Tc for Rational Method)
      idf.depth(duration_hr, ari_yr)      → inches  (use 24 for CN method)

    Raises RuntimeError if the API call fails.
    """
    try:
        return fetch_idf(lat, lon), True
    except Exception as e:
        raise RuntimeError(f"NOAA Atlas 14 fetch failed: {e}") from e


# ---------------------------------------------------------------------------
# USDA SSURGO — Soil hydrologic group composition
# ---------------------------------------------------------------------------

def _load_soil_gdf():
    """
    Load the statewide Oklahoma soil GeoDataFrame from the local parquet cache.
    Cached in a module-level variable so it is only read from disk once per process.
    """
    global _SOIL_GDF
    if _SOIL_GDF is None:
        import geopandas as gpd
        _SOIL_GDF = gpd.read_parquet(_LOCAL_SOIL_PARQUET)
    return _SOIL_GDF


def _fetch_soil_composition_local(watershed_geojson: dict) -> dict:
    """
    Compute HSG area fractions by clipping the local ok_soilmu.parquet to the
    watershed boundary and summing clipped polygon areas per dominant_hsg group.

    Returns {"A": %, "B": %, "C": %, "D": %} with values summing to ~100.
    Raises ValueError if no HSG data is found within the watershed.
    """
    import geopandas as gpd

    geom = _extract_geometry(watershed_geojson)
    soil_gdf = _load_soil_gdf()

    # Build a single-row GeoDataFrame for the watershed in the soil data's CRS
    ws_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    if soil_gdf.crs is not None and soil_gdf.crs != ws_gdf.crs:
        ws_gdf = ws_gdf.to_crs(soil_gdf.crs)

    # Clip soil polygons to the watershed boundary
    clipped = gpd.clip(soil_gdf, ws_gdf)
    if clipped.empty:
        raise ValueError("No soil polygons found within watershed boundary")

    # Project to equal-area CRS (NAD83 / Conus Albers) for accurate area calculation
    clipped_ea = clipped.to_crs("EPSG:5070")
    clipped_ea = clipped_ea.copy()
    clipped_ea["_clip_area_m2"] = clipped_ea.geometry.area

    totals = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
    total_area = 0.0

    for _, row in clipped_ea.iterrows():
        hsg = str(row.get("dominant_hsg", "")).strip().upper()
        # Handle dual-class soils (e.g. "A/D") — use drained (first) class
        hsg = hsg.split("/")[0]
        if hsg in totals:
            area = float(row["_clip_area_m2"])
            totals[hsg] += area
            total_area += area

    if total_area == 0:
        raise ValueError("No classifiable HSG data in clipped soil polygons")

    return {g: round(100.0 * area / total_area, 1) for g, area in totals.items()}


def fetch_soil_composition(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Return hydrologic soil group composition for the watershed.

    Uses local ok_soilmu.parquet (spatial clip).

    Returns ({"A": %, "B": %, "C": %, "D": %}, is_live: bool).
    Raises RuntimeError if the local data is missing or the clip fails.
    """
    try:
        return _fetch_soil_composition_local(watershed_geojson), True
    except Exception as e:
        raise RuntimeError(f"Soil data (SSURGO) failed: {e}") from e


# ---------------------------------------------------------------------------
# USDA gSSURGO — Surface soil texture
# ---------------------------------------------------------------------------

def _load_tex_lookup() -> dict:
    """
    Build a mukey → texdesc dict from the local gSSURGO GDB. Cached.

    Join chain (mirrors soil_maps_imhoff.ipynb):
      component (dominant major comp) → chorizon (surface horizon) → chtexturegrp (rv)
    """
    global _TEX_LOOKUP
    if _TEX_LOOKUP is not None:
        return _TEX_LOOKUP

    import fiona
    import pandas as pd

    with fiona.open(_GSSURGO_GDB, layer="component") as src:
        comp = pd.DataFrame([f["properties"] for f in src])[
            ["mukey", "cokey", "comppct_r", "majcompflag"]
        ]
    major_comp = (
        comp[comp["majcompflag"] == "Yes"]
        .sort_values("comppct_r", ascending=False)
        .drop_duplicates("mukey", keep="first")
    )

    with fiona.open(_GSSURGO_GDB, layer="chorizon") as src:
        hor = pd.DataFrame([f["properties"] for f in src])[["cokey", "chkey", "hzdept_r"]]
    surface_hor = hor.sort_values("hzdept_r").drop_duplicates("cokey", keep="first")

    with fiona.open(_GSSURGO_GDB, layer="chtexturegrp") as src:
        tgrp = pd.DataFrame([f["properties"] for f in src])
    tgrp_rv = tgrp[tgrp["rvindicator"] == "Yes"][["chkey", "texdesc"]]

    lookup = (
        major_comp[["mukey", "cokey"]]
        .merge(surface_hor[["cokey", "chkey"]], on="cokey", how="left")
        .merge(tgrp_rv, on="chkey", how="left")
        .set_index("mukey")["texdesc"]
        .fillna("Unknown")
        .to_dict()
    )

    _TEX_LOOKUP = lookup
    return _TEX_LOOKUP


def fetch_soil_texture(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Return area-weighted surface soil texture composition for the watershed.

    Clips ok_soilmu.parquet to the watershed, looks up texdesc from gSSURGO_OK.gdb,
    and returns percentages sorted by area descending.

    Returns ({"Silt loam": %, "Fine sandy loam": %, ...}, is_live: bool).
    Falls back to ({}, False) if either local file is unavailable.
    """
    try:
        import geopandas as gpd

        geom = _extract_geometry(watershed_geojson)
        soil_gdf = _load_soil_gdf()

        ws_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
        if soil_gdf.crs is not None and soil_gdf.crs != ws_gdf.crs:
            ws_gdf = ws_gdf.to_crs(soil_gdf.crs)

        # Bbox pre-filter then precise intersection
        minx, miny, maxx, maxy = ws_gdf.total_bounds
        soil_bbox = soil_gdf.cx[minx:maxx, miny:maxy]
        clipped = gpd.overlay(soil_bbox, ws_gdf, how="intersection")

        if clipped.empty:
            raise ValueError("No soil polygons found within watershed boundary")

        # Area in acres (equal-area projection)
        clipped_ea = clipped.to_crs("EPSG:5070")
        clipped = clipped.copy()
        clipped["area_acres"] = clipped_ea.geometry.area / 4046.856
        clipped["MUKEY"] = clipped["MUKEY"].astype(str)

        # Join texture
        tex_lookup = _load_tex_lookup()
        clipped["texdesc"] = clipped["MUKEY"].map(tex_lookup).fillna("Unknown")

        tex_areas = clipped.groupby("texdesc")["area_acres"].sum()
        total = tex_areas.sum()
        if total == 0:
            raise ValueError("Zero total area in texture calculation")

        result = {
            tex: round(100.0 * area / total, 1)
            for tex, area in tex_areas.sort_values(ascending=False).items()
        }
        return result, True

    except Exception:
        return {}, False


# ---------------------------------------------------------------------------
# NLCD 2024 — Land use composition (local raster)
# ---------------------------------------------------------------------------

_LOCAL_NLCD_RASTER = (
    "/Users/ashishojha/Documents/LID excels/landuse /"
    "Annual_NLCD_LndCov_2024_CU_C1V1/Annual_NLCD_LndCov_2024_CU_C1V1.tif"
)


def fetch_landuse_composition(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Clip the local NLCD 2024 raster to the watershed and compute land use fractions.

    Uses a rasterio windowed read + geometry mask — no HTTP call required.

    Returns ({"Pasture/Meadow": %, ...}, is_live: bool).
    Raises RuntimeError if the raster is missing or the clip fails.
    """
    try:
        import rasterio
        from rasterio.features import geometry_mask
        from rasterio.windows import from_bounds
        from rasterio.warp import transform_bounds, transform_geom

        geom = _extract_geometry(watershed_geojson)
        minx, miny, maxx, maxy = geom.bounds

        with rasterio.open(_LOCAL_NLCD_RASTER) as src:
            # Convert WGS84 bounds to the raster's CRS for a windowed read
            win_bounds = transform_bounds(
                "EPSG:4326", src.crs, minx, miny, maxx, maxy, densify_pts=21
            )
            win = from_bounds(*win_bounds, src.transform).round_offsets().round_lengths()
            if win.width <= 0 or win.height <= 0:
                raise ValueError("Watershed extent too small for raster window")

            data = src.read(1, window=win, masked=False)
            win_transform = src.window_transform(win)

            # Reproject watershed geometry to raster CRS for masking
            geom_raster_crs = transform_geom(
                "EPSG:4326", src.crs, geom.__geo_interface__
            )
            inside = geometry_mask(
                [geom_raster_crs],
                out_shape=data.shape,
                transform=win_transform,
                invert=True,
                all_touched=False,
            )
            pixel_values = data[inside]

        # Exclude nodata and background zeros
        if src.nodata is not None:
            pixel_values = pixel_values[pixel_values != src.nodata]
        pixel_values = pixel_values[pixel_values > 0]

        return _nlcd_pixels_to_landuse(pixel_values), True

    except Exception as e:
        raise RuntimeError(f"Land use data (NLCD) failed: {e}") from e


def _nlcd_pixels_to_landuse(pixel_values: np.ndarray) -> dict:
    """Convert array of NLCD pixel values to landuse percentage dict."""
    totals: dict[str, int] = {}
    for pv in pixel_values:
        lu = NLCD_TO_LANDUSE.get(int(pv))
        if lu is not None:
            totals[lu] = totals.get(lu, 0) + 1

    total = sum(totals.values())
    if total == 0:
        raise ValueError("No classifiable NLCD pixels found in watershed")

    return {lu: round(100.0 * count / total, 1) for lu, count in totals.items()}


# ---------------------------------------------------------------------------
# Soil GeoDataFrame + NLCD array — for map visualisation
# ---------------------------------------------------------------------------

def fetch_soil_geodataframe(watershed_geojson: dict):
    """
    Clip SSURGO soil polygons to the watershed and attach texture labels.

    Returns a GeoDataFrame (EPSG:4326) with columns:
      geometry, MUKEY, MUSYM, dominant_hsg, texdesc, area_acres
    and (is_live: bool).  Returns (None, False) on failure.
    """
    try:
        import geopandas as gpd

        geom     = _extract_geometry(watershed_geojson)
        soil_gdf = _load_soil_gdf()

        ws_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
        if soil_gdf.crs is not None and soil_gdf.crs != ws_gdf.crs:
            ws_gdf = ws_gdf.to_crs(soil_gdf.crs)

        minx, miny, maxx, maxy = ws_gdf.total_bounds
        soil_bbox = soil_gdf.cx[minx:maxx, miny:maxy]
        clipped   = gpd.overlay(soil_bbox, ws_gdf, how="intersection")

        if clipped.empty:
            return None, False

        clipped = clipped.copy()
        clipped["MUKEY"] = clipped["MUKEY"].astype(str) if "MUKEY" in clipped.columns else ""
        clipped["area_acres"] = clipped.to_crs("EPSG:5070").geometry.area / 4046.856

        # Join texture description
        tex_lookup = _load_tex_lookup()            # mukey → texdesc
        clipped["texdesc"] = clipped["MUKEY"].map(tex_lookup).fillna("Unknown")

        return clipped.to_crs("EPSG:4326"), True

    except Exception:
        return None, False


def fetch_nlcd_array(watershed_geojson: dict):
    """
    Clip the NLCD 2024 raster to the watershed and return the pixel array.

    Returns (2D np.ndarray of uint8 NLCD codes, is_live: bool).
    Pixels outside the watershed boundary are set to 0.
    Returns (None, False) on failure.
    """
    try:
        import rasterio
        from rasterio.features import geometry_mask
        from rasterio.windows import from_bounds
        from rasterio.warp import transform_bounds, transform_geom

        geom = _extract_geometry(watershed_geojson)
        minx, miny, maxx, maxy = geom.bounds

        with rasterio.open(_LOCAL_NLCD_RASTER) as src:
            win_bounds = transform_bounds(
                "EPSG:4326", src.crs, minx, miny, maxx, maxy, densify_pts=21
            )
            win = from_bounds(*win_bounds, src.transform).round_offsets().round_lengths()
            if win.width <= 0 or win.height <= 0:
                raise ValueError("Extent too small")

            data      = src.read(1, window=win, masked=False)
            win_tf    = src.window_transform(win)
            nodata    = src.nodata

            geom_rcrs = transform_geom("EPSG:4326", src.crs, geom.__geo_interface__)
            inside = geometry_mask(
                [geom_rcrs], out_shape=data.shape,
                transform=win_tf, invert=True, all_touched=False,
            )

        arr = np.where(inside, data, 0).astype(np.uint16)
        if nodata is not None:
            arr = np.where(data == nodata, 0, arr)

        return arr, True

    except Exception:
        return None, False


# ---------------------------------------------------------------------------
# Spatial land use × soil intersection
# ---------------------------------------------------------------------------

_HSG_CODE   = {"A": 1, "B": 2, "C": 3, "D": 4}
_HSG_DECODE = {v: k for k, v in _HSG_CODE.items()}


def fetch_landuse_soil_intersection(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Pixel-level spatial intersection of NLCD land use and SSURGO HSG.

    For every NLCD pixel inside the watershed the function looks up which HSG
    polygon it falls within (by rasterising the SSURGO polygons onto the NLCD
    grid).  This avoids the statistical-independence assumption made when soil
    and land-use fractions are simply multiplied together.

    Returns ({(lu_key, hsg): area_pct}, is_live: bool) where area_pct values
    sum to ~100.  Falls back to ({}, False) if either local dataset fails.
    """
    try:
        import rasterio
        from rasterio.features import geometry_mask, rasterize as rio_rasterize
        from rasterio.windows import from_bounds
        from rasterio.warp import transform_bounds, transform_geom
        import geopandas as gpd

        geom = _extract_geometry(watershed_geojson)
        minx, miny, maxx, maxy = geom.bounds

        # --- Step 1: windowed NLCD read ---
        with rasterio.open(_LOCAL_NLCD_RASTER) as src:
            win_bounds = transform_bounds(
                "EPSG:4326", src.crs, minx, miny, maxx, maxy, densify_pts=21
            )
            win = from_bounds(*win_bounds, src.transform).round_offsets().round_lengths()
            if win.width <= 0 or win.height <= 0:
                raise ValueError("Watershed extent too small for raster window")

            nlcd_data   = src.read(1, window=win, masked=False)
            win_tf      = src.window_transform(win)
            raster_crs  = src.crs
            nodata      = src.nodata

            geom_rcrs = transform_geom("EPSG:4326", raster_crs, geom.__geo_interface__)
            ws_mask = geometry_mask(
                [geom_rcrs],
                out_shape=nlcd_data.shape,
                transform=win_tf,
                invert=True,
                all_touched=False,
            )

        # --- Step 2: clip SSURGO polygons to watershed, reproject to raster CRS ---
        soil_gdf = _load_soil_gdf()
        ws_gdf   = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
        if soil_gdf.crs is not None and soil_gdf.crs != ws_gdf.crs:
            ws_gdf = ws_gdf.to_crs(soil_gdf.crs)

        clipped_soil = gpd.clip(soil_gdf, ws_gdf)
        if clipped_soil.empty:
            raise ValueError("No SSURGO polygons in watershed")

        clipped_soil_rcrs = clipped_soil.to_crs(raster_crs)

        # --- Step 3: rasterise HSG onto the NLCD grid ---
        shapes = []
        for _, row in clipped_soil_rcrs.iterrows():
            hsg_raw = str(row.get("dominant_hsg", "")).strip().upper().split("/")[0]
            code    = _HSG_CODE.get(hsg_raw)
            geom_s  = row.geometry
            if code is not None and geom_s is not None and not geom_s.is_empty:
                shapes.append((geom_s.__geo_interface__, code))

        if not shapes:
            raise ValueError("No valid HSG shapes to rasterise")

        hsg_raster = rio_rasterize(
            shapes,
            out_shape=nlcd_data.shape,
            transform=win_tf,
            fill=0,        # 0 = unclassified / outside all polygons
            dtype=np.uint8,
        )

        # --- Step 4: tally (lu_key, hsg) pairs inside the watershed mask ---
        nlcd_flat = nlcd_data[ws_mask]
        hsg_flat  = hsg_raster[ws_mask]

        if nodata is not None:
            valid     = nlcd_flat != nodata
            nlcd_flat = nlcd_flat[valid]
            hsg_flat  = hsg_flat[valid]

        valid     = nlcd_flat > 0
        nlcd_flat = nlcd_flat[valid]
        hsg_flat  = hsg_flat[valid]

        totals: dict[tuple[str, str], int] = {}
        for nlcd_val, hsg_code in zip(nlcd_flat.tolist(), hsg_flat.tolist()):
            lu  = NLCD_TO_LANDUSE.get(int(nlcd_val))
            hsg = _HSG_DECODE.get(int(hsg_code))
            if lu is not None and hsg is not None:
                key = (lu, hsg)
                totals[key] = totals.get(key, 0) + 1

        total = sum(totals.values())
        if total == 0:
            raise ValueError("No valid (lu, hsg) intersections found")

        result = {key: round(100.0 * count / total, 2) for key, count in totals.items()}
        return result, True

    except Exception:
        return {}, False


def intersection_to_soil_pct(intersection_pct: dict) -> dict[str, float]:
    """Marginalise intersection dict → {"A": %, "B": %, "C": %, "D": %}."""
    totals: dict[str, float] = {}
    for (_, hsg), pct in intersection_pct.items():
        totals[hsg] = totals.get(hsg, 0.0) + pct
    return {hsg: round(v, 1) for hsg, v in sorted(totals.items())}


def intersection_to_landuse_pct(intersection_pct: dict) -> dict[str, float]:
    """Marginalise intersection dict → {"Pasture/Meadow": %, ...}."""
    totals: dict[str, float] = {}
    for (lu, _), pct in intersection_pct.items():
        totals[lu] = totals.get(lu, 0.0) + pct
    return {lu: round(v, 1) for lu, v in sorted(totals.items(), key=lambda x: -x[1])}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _extract_geometry(geojson: dict):
    """Extract a Shapely geometry from a GeoJSON Feature or FeatureCollection."""
    if geojson.get("type") == "FeatureCollection":
        features = geojson.get("features", [])
        if not features:
            raise ValueError("Empty FeatureCollection")
        geom = shape(features[0]["geometry"])
        for feat in features[1:]:
            geom = geom.union(shape(feat["geometry"]))
        return geom
    elif geojson.get("type") == "Feature":
        return shape(geojson["geometry"])
    else:
        return shape(geojson)
