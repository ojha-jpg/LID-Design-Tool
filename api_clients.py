"""
api_clients.py — All external API integrations for the LID Peak Runoff Tool.

Functions:
  delineate_watershed()       — USGS StreamStats delineation
  get_basin_characteristics() — USGS StreamStats basin chars (area, Tc)
  get_peak_flow_regression()  — USGS NSS regression peak flows
  fetch_atlas14()             — NOAA Atlas 14 precipitation
  fetch_soil_composition()    — USDA SSURGO hydrologic soil groups (SDA API)
  fetch_soil_texture()        — USDA SSURGO surface soil texture (SDA API)
  fetch_landuse_composition() — NLCD 2024 land use within watershed (MRLC WCS API)
"""

import json
import csv
import os
import re
import tempfile
import requests
import numpy as np
from pyproj import Transformer
from shapely.geometry import Point, shape, mapping
from shapely.ops import transform as shp_transform

from reference_data import (
    RETURN_PERIODS,
    NLCD_TO_LANDUSE,
)
from noaa_atlas14 import fetch_idf, IDF


# ---------------------------------------------------------------------------
# USDA SDA API — WFS spatial service + tabular REST
# ---------------------------------------------------------------------------

_SDA_WFS_URL    = "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84Geographic.wfs"
_SDA_TABULAR_URL = "https://SDMDataAccess.sc.egov.usda.gov/Tabular/SDMTabularService/post.rest"
_WFS_BBOX_BUFFER = 0.005   # degrees; pads bbox so edge polygons are included
_SDA_CHUNK       = 400     # max mukeys per tabular IN (...) clause
_SITE_SOIL_SAMPLE_RADIUS_FT = 25.0


def _sda_tabular_query(sql: str) -> list:
    """POST a plain-SQL query to the SDA tabular REST endpoint; returns list of row dicts."""
    payload = {"query": sql, "FORMAT": "JSON+COLUMNNAME"}
    resp = requests.post(_SDA_TABULAR_URL, data=payload, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("Table", [])
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def _fetch_soil_gdf_wfs(geom):
    """
    Download SSURGO map-unit polygon geometries from the SDA WFS for the
    geometry's bounding box, clip to the exact geometry, and compute clipped
    area in acres (EPSG:5070 equal-area projection).

    Returns a GeoDataFrame (EPSG:4326) with columns:
      MUKEY, MUSYM, geometry, clip_area_acres
    Raises ValueError if no features are returned.
    """
    import geopandas as gpd

    minx, miny, maxx, maxy = geom.bounds
    minx -= _WFS_BBOX_BUFFER;  miny -= _WFS_BBOX_BUFFER
    maxx += _WFS_BBOX_BUFFER;  maxy += _WFS_BBOX_BUFFER

    wfs_url = (
        f"{_SDA_WFS_URL}?SERVICE=WFS&VERSION=1.0.0&REQUEST=GetFeature"
        f"&TYPENAME=MapunitPoly&BBOX={minx},{miny},{maxx},{maxy}"
    )
    gdf = gpd.read_file(wfs_url)

    if gdf.empty:
        raise ValueError("SDA WFS returned no features for watershed bounding box")

    # Ensure EPSG:4326
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    # Normalise column names to UPPER (WFS returns lowercase)
    gdf = gdf.rename(columns={c: c.upper() for c in gdf.columns if c != "geometry"})

    # Clip to exact watershed boundary
    ws_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    clipped = gpd.clip(gdf, ws_gdf)
    if clipped.empty:
        raise ValueError("No soil polygons found within watershed boundary (WFS)")

    clipped = clipped.copy()
    clipped["clip_area_acres"] = clipped.to_crs("EPSG:5070").geometry.area / 4046.856
    return clipped


def _fetch_hsg_for_mukeys(mukeys: list) -> dict:
    """
    Return {mukey: dominant_hsg} via SDA tabular API.
    Dominant = highest comppct_r major component with a valid hydgrp.
    Dual-class HSG (e.g. 'A/D') → drained class (first letter).
    """
    in_str = ", ".join(f"'{k}'" for k in mukeys)
    sql = f"""
    SELECT co.mukey, co.comppct_r, co.hydgrp
    FROM   component co
    WHERE  co.mukey       IN ({in_str})
      AND  co.majcompflag = 'Yes'
      AND  co.hydgrp      IS NOT NULL
    ORDER BY co.mukey, co.comppct_r DESC
    """
    rows = _sda_tabular_query(sql)

    best: dict = {}   # mukey → (comppct_r, hsg)
    for row in rows:
        mukey = str(row.get("mukey") or "").strip()
        try:
            pct = float(row.get("comppct_r") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        hsg = str(row.get("hydgrp") or "").strip().upper().split("/")[0]
        if mukey and (mukey not in best or pct > best[mukey][0]):
            best[mukey] = (pct, hsg)

    return {mk: hsg for mk, (_, hsg) in best.items()}


def _fetch_texture_for_mukeys(mukeys: list) -> dict:
    """
    Return {mukey: texdesc} via SDA tabular API.
    Surface horizon of the dominant major component (rv indicator = Yes).
    """
    in_str = ", ".join(f"'{k}'" for k in mukeys)
    sql = f"""
    SELECT co.mukey, co.comppct_r, ch.hzdept_r, chtg.texdesc
    FROM   component     co
    JOIN   chorizon      ch   ON ch.cokey   = co.cokey
    JOIN   chtexturegrp  chtg ON chtg.chkey = ch.chkey
                              AND chtg.rvindicator = 'Yes'
    WHERE  co.mukey       IN ({in_str})
      AND  co.majcompflag = 'Yes'
    ORDER BY co.mukey, co.comppct_r DESC, ch.hzdept_r ASC
    """
    rows = _sda_tabular_query(sql)

    best: dict = {}   # mukey → texdesc (first row per mukey wins due to ORDER BY)
    for row in rows:
        mukey = str(row.get("mukey") or "").strip()
        if mukey not in best:
            tex = str(row.get("texdesc") or "Unknown").strip()
            if not tex or tex.lower() in ("none", "null", ""):
                tex = "Unknown"
            best[mukey] = tex

    return best


def _fetch_soil_composition_api(watershed_geojson: dict) -> dict:
    """
    HSG area fractions via SDA WFS + tabular API (no local files required).

    Returns {"A": %, "B": %, "C": %, "D": %} summing to ~100.
    """
    geom    = _extract_geometry(watershed_geojson)
    clipped = _fetch_soil_gdf_wfs(geom)

    mukeys = clipped["MUKEY"].astype(str).unique().tolist()
    hsg_lookup: dict = {}
    for i in range(0, len(mukeys), _SDA_CHUNK):
        hsg_lookup.update(_fetch_hsg_for_mukeys(mukeys[i : i + _SDA_CHUNK]))

    totals     = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
    total_area = 0.0
    for _, row in clipped.iterrows():
        hsg  = hsg_lookup.get(str(row["MUKEY"]).strip(), "")
        area = float(row["clip_area_acres"])
        total_area += area
        if hsg in totals:
            totals[hsg] += area

    if total_area == 0:
        raise ValueError("No classifiable HSG data from SDA API")

    return {g: round(100.0 * a / total_area, 1) for g, a in totals.items()}


def _fetch_soil_texture_api(watershed_geojson: dict) -> dict:
    """
    Area-weighted surface soil texture via SDA WFS + tabular API.

    Returns {"Silt loam": %, ...} sorted by area descending.
    """
    geom    = _extract_geometry(watershed_geojson)
    clipped = _fetch_soil_gdf_wfs(geom)

    mukeys = clipped["MUKEY"].astype(str).unique().tolist()
    tex_lookup: dict = {}
    for i in range(0, len(mukeys), _SDA_CHUNK):
        tex_lookup.update(_fetch_texture_for_mukeys(mukeys[i : i + _SDA_CHUNK]))

    tex_areas: dict = {}
    total = 0.0
    for _, row in clipped.iterrows():
        tex  = tex_lookup.get(str(row["MUKEY"]).strip(), "Unknown")
        area = float(row["clip_area_acres"])
        tex_areas[tex] = tex_areas.get(tex, 0.0) + area
        total += area

    if total == 0:
        raise ValueError("Zero total area in texture calculation (API)")

    return {
        tex: round(100.0 * area / total, 1)
        for tex, area in sorted(tex_areas.items(), key=lambda x: -x[1])
    }


def _site_sample_geojson(lat: float, lon: float, sample_radius_ft: float = _SITE_SOIL_SAMPLE_RADIUS_FT) -> dict:
    """
    Return a small buffered polygon around a site point for point-scale SSURGO sampling.

    The soil texture API expects an area geometry, so PP/BRC site lookups sample a small
    circular polygon around the provided coordinates rather than a watershed.
    """
    if not (-90.0 <= lat <= 90.0):
        raise ValueError("Latitude must be between -90 and 90.")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError("Longitude must be between -180 and 180.")
    if sample_radius_ft <= 0:
        raise ValueError("Sample radius must be greater than zero.")

    to_5070 = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    to_4326 = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)

    point_4326 = Point(lon, lat)
    point_5070 = shp_transform(to_5070.transform, point_4326)
    sample_geom_5070 = point_5070.buffer(sample_radius_ft * 0.3048)
    sample_geom_4326 = shp_transform(to_4326.transform, sample_geom_5070)

    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": mapping(sample_geom_4326),
            "properties": {
                "lat": lat,
                "lon": lon,
                "sample_radius_ft": sample_radius_ft,
            },
        }],
    }


def infer_design_soil_type(texture: str) -> str | None:
    """
    Map a USDA texture description to the PP/BRC design-soil categories.

    Returns one of the table lookup keys used by the design tools, or None when
    the USDA texture cannot be mapped confidently.
    """
    normalized = re.sub(r"[^a-z\s]", " ", str(texture or "").lower())
    normalized = " ".join(normalized.split())
    if not normalized or normalized == "unknown":
        return None

    words = set(normalized.split())
    ordered_matches = (
        ("sandy clay loam", "Sandy Clay Loam"),
        ("clay loam", "Clay Loam"),
        ("silt loam", "Silt Loam"),
        ("sandy loam", "Sandy Loam"),
        ("loamy sand", "Loamy Sand"),
    )
    for needle, soil_type in ordered_matches:
        if needle in normalized:
            return soil_type

    # USDA textures such as "Loamy fine sand" or "Loamy very fine sand"
    # should still map to the loamy-sand design category.
    if "loamy" in words and "sand" in words:
        return "Loamy Sand"

    # USDA textures such as "Fine sandy loam" should map to sandy loam.
    if "sandy" in words and "loam" in words:
        return "Sandy Loam"

    if "sand" in words:
        return "Sand"
    if "silt" in words:
        return "Silt"
    if "loam" in words:
        return "Loam"
    if "clay" in words:
        return "Clay"
    return None


# ---------------------------------------------------------------------------
# USGS StreamStats
# ---------------------------------------------------------------------------

_SS_BASE = "https://streamstats.usgs.gov"
_TIMEOUT = 60  # seconds
_CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
_CENSUS_GEOGRAPHIES_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"


def reverse_geocode_state(lat: float, lon: float) -> tuple[dict, bool]:
    """
    Reverse-geocode a point to a US state/territory using the Census geocoder.

    Returns ({"state_abbrev": "KS", "state_name": "Kansas"}, is_live: bool).
    """
    try:
        resp = requests.get(
            _CENSUS_GEOGRAPHIES_URL,
            params={
                "x": f"{lon:.6f}",
                "y": f"{lat:.6f}",
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "layers": "States",
                "format": "json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        states = (
            resp.json()
            .get("result", {})
            .get("geographies", {})
            .get("States", [])
        )
        if not states:
            return {}, False

        state = states[0]
        abbrev = str(state.get("STUSAB") or "").strip().upper()
        name = str(state.get("BASENAME") or "").strip()
        if not abbrev:
            return {}, False

        return {
            "state_abbrev": abbrev,
            "state_name": name or abbrev,
        }, True
    except Exception:
        return {}, False


def infer_streamstats_region(lat: float, lon: float) -> str:
    """
    Infer the StreamStats region code for a point from its state/territory.
    """
    state, is_live = reverse_geocode_state(lat, lon)
    if not is_live or not state.get("state_abbrev"):
        raise RuntimeError("Could not determine the StreamStats state/region for the selected point.")
    return state["state_abbrev"]


def delineate_watershed(lat: float, lon: float, region: str | None = None) -> dict:
    """
    Delineate watershed using USGS StreamStats API.

    Returns dict with keys:
      "workspace_id": str
      "geojson": dict  (GeoJSON FeatureCollection of watershed boundary)
      "area_sqmi": float
    Raises RuntimeError on failure.
    """
    region = (region or infer_streamstats_region(lat, lon)).upper()
    url = f"{_SS_BASE}/ss-delineate/v1/delineate/sshydro/{region}"
    params = {
        "lat": lat,
        "lon": lon,
        "includeparameters": "true",
        "includeflowtypes": "true",
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
        "region": region,
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


def geocode_address(address: str) -> tuple[dict, bool]:
    """
    Geocode a US street address using the US Census geocoder.

    Returns ({"lat": ..., "lon": ..., "matched_address": ...}, is_live: bool).
    Returns ({}, False) when the address cannot be matched.
    """
    address = str(address or "").strip()
    if not address:
        return {}, False

    try:
        resp = requests.get(
            _CENSUS_GEOCODER_URL,
            params={
                "address": address,
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        matches = resp.json().get("result", {}).get("addressMatches", [])
        if not matches:
            return {}, False

        best = matches[0]
        coords = best.get("coordinates") or {}
        lat = coords.get("y")
        lon = coords.get("x")
        if lat is None or lon is None:
            return {}, False

        return {
            "lat": float(lat),
            "lon": float(lon),
            "matched_address": str(best.get("matchedAddress") or address),
        }, True
    except Exception:
        return {}, False


# ---------------------------------------------------------------------------
# USDA SSURGO — Soil hydrologic group composition
# ---------------------------------------------------------------------------

def fetch_soil_composition(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Return hydrologic soil group composition for the watershed via SDA API.

    Returns ({"A": %, "B": %, "C": %, "D": %}, is_live: bool).
    Raises RuntimeError on failure.
    """
    try:
        return _fetch_soil_composition_api(watershed_geojson), True
    except Exception as e:
        raise RuntimeError(f"Soil data (SSURGO API) failed: {e}") from e


# ---------------------------------------------------------------------------
# USDA SSURGO — Surface soil texture (API)
# ---------------------------------------------------------------------------

def fetch_soil_texture(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Return area-weighted surface soil texture composition via SDA API.

    Returns ({"Silt loam": %, "Fine sandy loam": %, ...}, is_live: bool).
    Returns ({}, False) on failure.
    """
    try:
        return _fetch_soil_texture_api(watershed_geojson), True
    except Exception:
        return {}, False


def fetch_site_soil_texture(
    lat: float,
    lon: float,
    sample_radius_ft: float = _SITE_SOIL_SAMPLE_RADIUS_FT,
) -> tuple[dict, bool]:
    """
    Return site-scale SSURGO texture data for PP/BRC tools.

    The result includes:
      textures         — area-weighted USDA textures within the sample area
      dominant_texture — highest-percentage texture
      soil_type        — mapped design soil category for PP/BRC tables, if inferred
      sample_radius_ft — radius used to build the sample polygon
    """
    try:
        sample_geojson = _site_sample_geojson(lat, lon, sample_radius_ft=sample_radius_ft)
        textures, is_live = fetch_soil_texture(sample_geojson)
        if not is_live or not textures:
            return {}, False

        dominant_texture = next(iter(textures))
        return {
            "textures": textures,
            "dominant_texture": dominant_texture,
            "soil_type": infer_design_soil_type(dominant_texture),
            "sample_radius_ft": float(sample_radius_ft),
        }, True
    except Exception:
        return {}, False


# ---------------------------------------------------------------------------
# NLCD — Land use composition via MRLC WCS API
# ---------------------------------------------------------------------------

_NLCD_WCS_BASE = (
    "https://dmsdata.cr.usgs.gov/geoserver/"
    "mrlc_Land-Cover-Native_conus_year_data/wcs"
)
_NLCD_COVERAGE = (
    "mrlc_Land-Cover-Native_conus_year_data:"
    "Land-Cover-Native_conus_year_data"
)
_NLCD_YEAR       = 2024
_NLCD_BBOX_BUF   = 0.01   # degrees — pads tile so boundary pixels are included


def _fetch_nlcd_tile_wcs(geom):
    """
    Download an NLCD land cover tile from the MRLC WCS for the watershed bbox.

    The WCS native CRS is EPSG:5070 (Albers Equal Area, 30 m pixels).
    GeoServer returns an unnamed LOCAL_CS, so EPSG:5070 is hardcoded for all
    geometry reprojection — do NOT read raster CRS from src.crs.

    Returns (data, win_transform, nodata, inside_mask) where:
      data         — 2D uint8 ndarray of NLCD codes (full tile, unmasked)
      win_transform— affine transform of the tile in EPSG:5070
      nodata       — scalar nodata value or None
      inside_mask  — boolean 2D ndarray, True for pixels inside the watershed
    """
    import rasterio
    from rasterio.features import geometry_mask

    # Expand bbox in WGS84, project to EPSG:5070
    minx, miny, maxx, maxy = geom.bounds
    minx -= _NLCD_BBOX_BUF; miny -= _NLCD_BBOX_BUF
    maxx += _NLCD_BBOX_BUF; maxy += _NLCD_BBOX_BUF

    t4326_5070 = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    x0, y0 = t4326_5070.transform(minx, miny)
    x1, y1 = t4326_5070.transform(maxx, maxy)
    bx0, by0, bx1, by1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    # Native resolution is 30 m
    px_w = max(10, round((bx1 - bx0) / 30))
    px_h = max(10, round((by1 - by0) / 30))

    resp = requests.get(
        _NLCD_WCS_BASE,
        params={
            "SERVICE":  "WCS",
            "VERSION":  "1.0.0",
            "REQUEST":  "GetCoverage",
            "COVERAGE": _NLCD_COVERAGE,
            "CRS":      "EPSG:5070",
            "BBOX":     f"{bx0},{by0},{bx1},{by1}",
            "WIDTH":    str(px_w),
            "HEIGHT":   str(px_h),
            "FORMAT":   "GeoTIFF",
            "TIME":     f"{_NLCD_YEAR}-01-01T00:00:00.000Z",
        },
        timeout=60,
        stream=True,
    )
    resp.raise_for_status()
    if "xml" in resp.headers.get("Content-Type", "") or \
       "html" in resp.headers.get("Content-Type", ""):
        raise RuntimeError(f"MRLC WCS error: {resp.text[:300]}")

    fd, tmp = tempfile.mkstemp(suffix="_nlcd_wcs.tif")
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in resp.iter_content(65536):
                fh.write(chunk)

        with rasterio.open(tmp) as src:
            data   = src.read(1, masked=False)
            win_tf = src.transform
            nodata = src.nodata

    finally:
        os.unlink(tmp)

    # Reproject watershed geometry to EPSG:5070 for the pixel mask
    geom_5070 = shp_transform(t4326_5070.transform, geom)
    inside = geometry_mask(
        [mapping(geom_5070)],
        out_shape=data.shape,
        transform=win_tf,
        invert=True,
        all_touched=False,
    )
    return data, win_tf, nodata, inside


def fetch_landuse_composition(watershed_geojson: dict) -> tuple[dict, bool]:
    """
    Fetch NLCD land cover from the MRLC WCS API and compute land use fractions.

    Returns ({"Pasture/Meadow": %, ...}, is_live: bool).
    Raises RuntimeError on failure.
    """
    try:
        geom = _extract_geometry(watershed_geojson)
        data, _, nodata, inside = _fetch_nlcd_tile_wcs(geom)

        pixel_values = data[inside]
        if nodata is not None:
            pixel_values = pixel_values[pixel_values != nodata]
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
    Clip SSURGO soil polygons to the watershed and attach HSG + texture labels via SDA API.

    Returns a GeoDataFrame (EPSG:4326) with columns:
      geometry, MUKEY, MUSYM, dominant_hsg, texdesc, area_acres
    and (is_live: bool).  Returns (None, False) on failure.
    """
    try:
        geom    = _extract_geometry(watershed_geojson)
        clipped = _fetch_soil_gdf_wfs(geom)

        mukeys = clipped["MUKEY"].astype(str).unique().tolist()
        hsg_lookup: dict = {}
        tex_lookup: dict = {}
        for i in range(0, len(mukeys), _SDA_CHUNK):
            chunk = mukeys[i : i + _SDA_CHUNK]
            hsg_lookup.update(_fetch_hsg_for_mukeys(chunk))
            tex_lookup.update(_fetch_texture_for_mukeys(chunk))

        clipped = clipped.copy()
        clipped["dominant_hsg"] = clipped["MUKEY"].astype(str).map(hsg_lookup).fillna("")
        clipped["texdesc"]      = clipped["MUKEY"].astype(str).map(tex_lookup).fillna("Unknown")
        clipped["area_acres"]   = clipped["clip_area_acres"]

        return clipped.to_crs("EPSG:4326"), True

    except Exception:
        return None, False


def fetch_nlcd_array(watershed_geojson: dict):
    """
    Fetch NLCD land cover from the MRLC WCS API and return the pixel array.

    Returns (2D np.ndarray of uint16 NLCD codes, is_live: bool).
    Pixels outside the watershed boundary are set to 0.
    Returns (None, False) on failure.
    """
    try:
        geom = _extract_geometry(watershed_geojson)
        data, _, nodata, inside = _fetch_nlcd_tile_wcs(geom)

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
        from rasterio.features import rasterize as rio_rasterize
        import geopandas as gpd

        geom = _extract_geometry(watershed_geojson)

        # --- Step 1: fetch NLCD tile via WCS ---
        nlcd_data, win_tf, nodata, ws_mask = _fetch_nlcd_tile_wcs(geom)
        # The tile is in EPSG:5070; hardcode this for soil reprojection below.
        _RASTER_CRS = "EPSG:5070"

        # --- Step 2: clip SSURGO polygons to watershed via SDA API, reproject to EPSG:5070 ---
        clipped_api = _fetch_soil_gdf_wfs(geom)
        mukeys_api  = clipped_api["MUKEY"].astype(str).unique().tolist()
        hsg_lkp_api: dict = {}
        for i in range(0, len(mukeys_api), _SDA_CHUNK):
            hsg_lkp_api.update(_fetch_hsg_for_mukeys(mukeys_api[i : i + _SDA_CHUNK]))
        clipped_api = clipped_api.copy()
        clipped_api["dominant_hsg"] = (
            clipped_api["MUKEY"].astype(str).map(hsg_lkp_api).fillna("")
        )
        clipped_soil_rcrs = clipped_api.to_crs(_RASTER_CRS)

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
# DEM-based watershed features (USGS 3DEP + pysheds)
# ---------------------------------------------------------------------------

_DEM_WCS = (
    "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/"
    "ImageServer/WCSServer"
)
_DEM_EXPORT_IMAGE = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/"
    "ImageServer/exportImage"
)
_DEM_WARMUP_URLS = (
    "https://apps.nationalmap.gov/viewer/",
    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
)
_DEM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "image/tiff,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://apps.nationalmap.gov/viewer/",
    "Origin": "https://apps.nationalmap.gov",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Module-level sentinel: reset to False whenever this module is (re)loaded.
# Using a module variable rather than a flag on np.can_cast means a Streamlit
# hot-reload of api_clients.py always re-applies the patch to the live numpy
# object, avoiding stale/buggy patches surviving across reloads.
_NUMPY_PATCHED: bool = False
_PY3DEP_RESOLUTION_M = 30
_DEM_BBOX_BUFFER_DEG = 0.02


def _patch_numpy_compat() -> None:
    """Restore NumPy aliases removed in 2.x that older geospatial deps still call."""
    if not hasattr(np, "in1d"):
        def _in1d(ar1, ar2, assume_unique=False, invert=False):
            return np.isin(
                np.asarray(ar1).ravel(),
                ar2,
                assume_unique=assume_unique,
                invert=invert,
            )
        np.in1d = _in1d  # type: ignore[attr-defined]


def _patch_numpy_for_pysheds():
    """
    Patch np.can_cast once per module-load for NumPy 2 / pysheds 0.4 (NEP-50).
    NumPy 2 raises TypeError when np.can_cast is called with a Python scalar;
    pysheds 0.4 does this internally. The patch falls back to a NaN-aware
    round-trip check so both NaN and finite nodata values are handled correctly.
    """
    global _NUMPY_PATCHED
    if _NUMPY_PATCHED:
        return
    _patch_numpy_compat()
    _orig = np.can_cast

    import math as _math

    def _safe(from_, to, casting="unsafe"):  # type: ignore[override]
        try:
            return _orig(from_, to, casting=casting)  # type: ignore[arg-type]
        except TypeError:
            # NumPy 2 NEP-50: Python scalars raise TypeError in can_cast.
            try:
                orig_v = float(from_)
                conv_v = float(np.array(from_, dtype=to))
                # NaN is representable in any float dtype, but nan != nan (IEEE 754)
                return (_math.isnan(orig_v) and _math.isnan(conv_v)) or (orig_v == conv_v)
            except (OverflowError, ValueError):
                return False

    np.can_cast = _safe
    _NUMPY_PATCHED = True


def _dataarray_to_2d_numpy(data_array, dtype=float) -> np.ndarray:
    """Convert a rioxarray-backed DataArray to a plain 2D NumPy array."""
    values = np.asarray(np.ma.filled(np.squeeze(data_array.values), np.nan), dtype=dtype)
    if values.ndim != 2:
        raise ValueError(f"Expected a 2D DEM raster, got shape {values.shape}")

    nodata = data_array.rio.nodata
    if nodata is not None and np.isfinite(nodata):
        values[np.isclose(values, float(nodata))] = np.nan
    return values


def _mask_geometry_to_raster(
    geom,
    geom_crs,
    raster_crs,
    raster_transform,
    out_shape: tuple[int, int],
) -> np.ndarray:
    """Return a boolean mask with True for cells inside the input geometry."""
    from rasterio.features import geometry_mask as _geom_mask

    raster_crs_text = str(raster_crs)
    geom_crs_text = str(geom_crs)
    geom_for_raster = geom
    if geom_crs_text != raster_crs_text:
        transformer = Transformer.from_crs(geom_crs, raster_crs, always_xy=True)
        geom_for_raster = shp_transform(transformer.transform, geom)

    return ~_geom_mask(
        [geom_for_raster.__geo_interface__],
        out_shape=out_shape,
        transform=raster_transform,
        all_touched=True,
    )


def _write_float_dem_raster(path: str, dem_data: np.ndarray, transform, crs, nodata: float) -> None:
    """Write a 2D float DEM to GeoTIFF for downstream pysheds processing."""
    import rasterio

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=dem_data.shape[0],
        width=dem_data.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=float(nodata),
    ) as dst:
        dst.write(dem_data.astype(np.float32), 1)


def _compute_dem_metrics(tmp_dem: str, ws_mask: np.ndarray, res_mx: float, res_my: float, nodata: float) -> tuple[dict, str | None]:
    """Run pysheds on a prepared DEM raster and summarize watershed metrics."""
    _patch_numpy_for_pysheds()
    from pysheds.grid import Grid

    grid = Grid.from_raster(tmp_dem)
    dem_r = grid.read_raster(tmp_dem)
    inflated = grid.resolve_flats(
        grid.fill_depressions(grid.fill_pits(dem_r))
    )
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)

    dem_full = np.array(grid.view(inflated), dtype=float)
    dem_full[dem_full == float(nodata)] = np.nan
    elev_ws = np.where(ws_mask, dem_full, np.nan)

    fdir_np = np.array(fdir, dtype=np.int32)
    acc_np = np.array(acc, dtype=float)
    dem_height, dem_width = ws_mask.shape
    d8_offsets = {
        64: (-1, 0), 128: (-1, 1),
        1: (0, 1), 2: (1, 1),
        4: (1, 0), 8: (1, -1),
        16: (0, -1), 32: (-1, -1),
    }
    ws_rows, ws_cols = np.where(ws_mask)
    exit_rows, exit_cols = [], []
    for r, c in zip(ws_rows.tolist(), ws_cols.tolist()):
        offset = d8_offsets.get(int(fdir_np[r, c]))
        if offset is None:
            continue
        nr, nc = r + offset[0], c + offset[1]
        if not (0 <= nr < dem_height and 0 <= nc < dem_width and ws_mask[nr, nc]):
            exit_rows.append(r)
            exit_cols.append(c)

    if exit_rows:
        best = int(np.argmax(acc_np[exit_rows, exit_cols]))
        row_out, col_out = exit_rows[best], exit_cols[best]
    else:
        acc_in_ws = np.where(ws_mask, acc_np, -np.inf)
        row_out, col_out = np.unravel_index(np.argmax(acc_in_ws), acc_in_ws.shape)

    flow_length_ft = None
    flow_warning = None
    try:
        dist_arr = grid.distance_to_outlet(
            x=col_out, y=row_out, fdir=fdir, xytype="index"
        )
        dist_np = np.array(dist_arr, dtype=float)
        if hasattr(dist_arr, "nodata") and dist_arr.nodata is not None:
            dist_np[dist_np == float(dist_arr.nodata)] = np.nan
        dist_np[~np.isfinite(dist_np)] = np.nan
        cell_size_m = np.sqrt(res_mx * res_my)
        ws_dist = dist_np[ws_mask]
        if ws_mask.any() and np.any(np.isfinite(ws_dist)):
            max_steps = float(np.nanmax(ws_dist))
            if np.isfinite(max_steps) and max_steps > 0:
                flow_length_ft = max_steps * cell_size_m * 3.28084
            else:
                flow_warning = f"D8 flow length = {max_steps} steps (degenerate)"
        else:
            flow_warning = "D8 routing produced no finite distances within watershed"
    except Exception as exc:
        flow_warning = str(exc)

    dy_e, dx_e = np.gradient(elev_ws, res_my, res_mx)
    slope_pct = np.sqrt(dx_e**2 + dy_e**2) * 100.0
    valid_mask = ws_mask & np.isfinite(slope_pct)
    mean_slope = float(np.nanmean(slope_pct[valid_mask])) if valid_mask.any() else 0.0

    elev_valid = elev_ws[np.isfinite(elev_ws)]
    elev_min_m = float(np.nanmin(elev_valid)) if elev_valid.size > 0 else 0.0
    elev_max_m = float(np.nanmax(elev_valid)) if elev_valid.size > 0 else 0.0

    metrics = {
        "flow_length_ft": round(flow_length_ft, 1) if flow_length_ft is not None else None,
        "mean_slope_pct": round(mean_slope, 2),
        "elev_min_m": round(elev_min_m, 1),
        "elev_max_m": round(elev_max_m, 1),
    }
    return metrics, flow_warning


def _download_dem_geotiff_bytes(
    bbox_w: float,
    bbox_s: float,
    bbox_e: float,
    bbox_n: float,
    width: int = 300,
    height: int = 300,
) -> tuple[bytes | None, str | None]:
    """Download DEM GeoTIFF bytes with endpoint fallback for hosted environments.
    
    Returns (bytes, source) on success, (None, error_msg) on any error (including 403).
    Does NOT raise exceptions — returns None bytes so calling code can handle gracefully.
    """
    errors: list[str] = []
    session = requests.Session()
    session.headers.update(_DEM_HEADERS)

    # Warm the session first; hosted environments sometimes get denied on the
    # direct raster request unless the service sees a browser-like navigation.
    for warmup_url in _DEM_WARMUP_URLS:
        try:
            session.get(warmup_url, timeout=20)
        except Exception:
            pass

    # Primary: WCS GetCoverage (works in many local setups).
    try:
        resp = session.get(
            _DEM_WCS,
            params={
                "SERVICE":  "WCS",
                "VERSION":  "1.0.0",
                "REQUEST":  "GetCoverage",
                "COVERAGE": "DEP3Elevation",
                "CRS":      "EPSG:4326",
                "BBOX":     f"{bbox_w},{bbox_s},{bbox_e},{bbox_n}",
                "WIDTH":    str(width),
                "HEIGHT":   str(height),
                "FORMAT":   "GeoTIFF",
            },
            timeout=90,
        )
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "image" in content_type or "tiff" in content_type or "octet-stream" in content_type:
            return resp.content, "WCS"
        errors.append(f"WCS unexpected content-type: {content_type or 'unknown'}")
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 403:
            errors.append("WCS 403 Forbidden (likely blocked by USGS for Streamlit hosting)")
        else:
            errors.append(f"WCS HTTP {exc.response.status_code}")
    except Exception as exc:
        errors.append(f"WCS failed: {type(exc).__name__}: {exc}")

    # Fallback: ArcGIS ImageServer exportImage (often works when WCS is blocked).
    try:
        resp = session.get(
            _DEM_EXPORT_IMAGE,
            params={
                "bbox": f"{bbox_w},{bbox_s},{bbox_e},{bbox_n}",
                "bboxSR": "4326",
                "imageSR": "4326",
                "size": f"{width},{height}",
                "format": "tiff",
                "pixelType": "F32",
                "noData": "-9999",
                "interpolation": "RSP_BilinearInterpolation",
                "f": "image",
            },
            timeout=90,
        )
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "image" in content_type or "tiff" in content_type or "octet-stream" in content_type:
            return resp.content, "exportImage"
        errors.append(f"exportImage unexpected content-type: {content_type or 'unknown'}")
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 403:
            errors.append("exportImage 403 Forbidden (likely blocked by USGS for Streamlit hosting)")
        else:
            errors.append(f"exportImage HTTP {exc.response.status_code}")
    except Exception as exc:
        errors.append(f"exportImage failed: {type(exc).__name__}: {exc}")

    # Return None instead of raising, so calling code can handle gracefully
    return None, "3DEP DEM unavailable; " + " | ".join(errors)


def fetch_dem_features(
    watershed_geojson: dict | None,
    pour_lat: float | None = None,
    pour_lon: float | None = None,
) -> tuple[dict, bool]:
    """
    Fetch a USGS 3DEP DEM and compute DEM-based watershed features.

    The primary path uses the HyRiver ``py3dep.get_dem`` workflow so hosted
    deployments are not coupled to the raw ImageServer/WCS request logic.
    The older direct-download path remains only as a fallback.

    Parameters
    ----------
    watershed_geojson : GeoJSON FeatureCollection from delineate_watershed()
    pour_lat / pour_lon : outlet / pour-point coordinates (required for stream snap)

    Returns (result_dict, is_live: bool).
    result_dict keys:
      flow_length_ft  — longest D8 flow-path (ft) within the StreamStats boundary
      mean_slope_pct  — mean basin slope in percent
      elev_min_m      — minimum elevation in metres within the watershed
      elev_max_m      — maximum elevation in metres within the watershed
      dem_array       — float64 ndarray, elevation (m); NaN outside watershed
      dem_bounds      — (west, south, east, north) of the downloaded DEM tile (WGS84)
      res_mx          — pixel width  in metres
      res_my          — pixel height in metres
    Falls back to ({"_error": ..., "_traceback": ...}, False) on any error.
    """
    import tempfile
    import os as _os
    import rasterio

    if watershed_geojson is None:
        return {"_error": "watershed_geojson is required"}, False

    tmp_raw = tmp_dem = None
    try:
        # --- Derive bbox from StreamStats GeoJSON boundary ---
        from shapely.geometry import shape as _sshape
        if watershed_geojson.get("type") == "FeatureCollection":
            ws_shape = _sshape(watershed_geojson["features"][0]["geometry"])
        else:
            ws_shape = _sshape(watershed_geojson.get("geometry", watershed_geojson))

        minx, miny, maxx, maxy = ws_shape.bounds
        buffer = _DEM_BBOX_BUFFER_DEG
        bbox_w = minx - buffer
        bbox_e = maxx + buffer
        bbox_s = miny - buffer
        bbox_n = maxy + buffer

        NODATA = np.float32(-9999.0)
        fd2, tmp_dem = tempfile.mkstemp(suffix="_dem.tif")
        _os.close(fd2)

        display_array = None
        dem_bounds = None
        dem_source = None
        res_mx = res_my = None
        ws_mask = None
        py3dep_errors: list[str] = []

        try:
            _patch_numpy_compat()
            import py3dep

            dem_da = py3dep.get_dem(
                geometry=(bbox_w, bbox_s, bbox_e, bbox_n),
                resolution=_PY3DEP_RESOLUTION_M,
                crs=4326,
            )
            dem_source = f"py3dep.get_dem ({_PY3DEP_RESOLUTION_M} m)"

            dem_analysis = dem_da.rio.reproject("EPSG:5070")
            dem_display = dem_da.rio.reproject("EPSG:4326")

            analysis_data = _dataarray_to_2d_numpy(dem_analysis, dtype=np.float32)
            display_data = _dataarray_to_2d_numpy(dem_display, dtype=float)
            analysis_transform = dem_analysis.rio.transform()
            display_transform = dem_display.rio.transform()
            analysis_crs = dem_analysis.rio.crs

            if analysis_crs is None:
                raise ValueError("py3dep DEM has no CRS")

            analysis_shape = analysis_data.shape
            display_shape = display_data.shape
            ws_mask = _mask_geometry_to_raster(
                ws_shape, "EPSG:4326", analysis_crs, analysis_transform, analysis_shape
            )
            display_mask = _mask_geometry_to_raster(
                ws_shape, "EPSG:4326", "EPSG:4326", display_transform, display_shape
            )

            display_array = np.where(display_mask, display_data, np.nan)
            dem_bounds = tuple(float(v) for v in dem_display.rio.bounds())
            res_x, res_y = dem_analysis.rio.resolution()
            res_mx, res_my = abs(float(res_x)), abs(float(res_y))

            analysis_data[~np.isfinite(analysis_data)] = NODATA
            _write_float_dem_raster(
                tmp_dem,
                analysis_data,
                analysis_transform,
                analysis_crs,
                float(NODATA),
            )
        except Exception as exc:
            py3dep_errors.append(f"py3dep failed: {type(exc).__name__}: {exc}")

        if ws_mask is None or display_array is None or dem_bounds is None or res_mx is None or res_my is None:
            centre_lat = (miny + maxy) / 2.0
            m_per_deg_lon = 111_320.0 * np.cos(np.radians(centre_lat))
            m_per_deg_lat = 111_320.0

            dem_bytes, fallback_source = _download_dem_geotiff_bytes(
                bbox_w=bbox_w,
                bbox_s=bbox_s,
                bbox_e=bbox_e,
                bbox_n=bbox_n,
                width=300,
                height=300,
            )
            if dem_bytes is None:
                errors = py3dep_errors + [fallback_source or "DEM download failed"]
                return {
                    "_error": "DEM features unavailable — " + " | ".join(errors),
                    "_note": "DEM data not available in this environment. Slope and flow path analysis skipped.",
                }, False

            fd, tmp_raw = tempfile.mkstemp(suffix="_dem_raw.tif")
            with _os.fdopen(fd, "wb") as fh:
                fh.write(dem_bytes)

            with rasterio.open(tmp_raw) as src:
                raw_data = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    raw_data[raw_data == src.nodata] = NODATA

                dem_transform = src.transform
                dem_shape = raw_data.shape
                ws_mask = _mask_geometry_to_raster(
                    ws_shape, "EPSG:4326", src.crs or "EPSG:4326", dem_transform, dem_shape
                )
                raw_display = raw_data.astype(float)
                raw_display[raw_display == float(NODATA)] = np.nan
                display_array = np.where(ws_mask, raw_display, np.nan)
                dem_bounds = (bbox_w, bbox_s, bbox_e, bbox_n)
                res_mx = abs(src.res[0]) * m_per_deg_lon
                res_my = abs(src.res[1]) * m_per_deg_lat

                _write_float_dem_raster(
                    tmp_dem,
                    raw_data,
                    dem_transform,
                    src.crs or "EPSG:4326",
                    float(NODATA),
                )

            dem_source = fallback_source

        metrics, _flow_err = _compute_dem_metrics(
            tmp_dem,
            ws_mask=ws_mask,
            res_mx=res_mx,
            res_my=res_my,
            nodata=float(NODATA),
        )

        result = {
            **metrics,
            "dem_array": display_array,
            "dem_bounds": dem_bounds,
            "res_mx": res_mx,
            "res_my": res_my,
            "dem_source": dem_source,
        }
        if _flow_err:
            result["_flow_length_warning"] = _flow_err
        return result, True

    except Exception as _exc:
        import traceback as _tb
        return {"_error": str(_exc), "_traceback": _tb.format_exc()}, False

    finally:
        for p in (tmp_raw, tmp_dem):
            if p and _os.path.exists(p):
                try:
                    _os.unlink(p)
                except Exception:
                    pass


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
