# Integrations And Data

## Overview

The project has two data classes:

- public network services used at runtime
- local static data shipped with the repository

The Peak Runoff tool depends most heavily on live services. BRC and PP use only optional geocoding and site soil lookup. RWH uses only local data.

## External Services

### USGS StreamStats

Used by:

- `delineate_watershed(...)`
- `get_basin_characteristics(...)`
- `get_peak_flow_regression(...)`

Purpose:

- watershed boundary delineation from a pour point
- basin area and TLAG retrieval
- regression-flow reference values

Behavior:

- delineation is required when the user starts from a point
- basin characteristics are best-effort and can return an empty dict
- regression flows are best-effort and can return an empty list
- uploaded watershed boundaries bypass delineation and therefore cannot provide a usable StreamStats workspace for regression queries

### NOAA Atlas 14

Used by:

- `fetch_atlas14(...)` in `api_clients.py`
- `fetch_idf(...)` and `IDF` in `noaa_atlas14.py`

Purpose:

- design-storm depths and intensities for the Peak Runoff tool

Behavior:

- treated as required for Peak Runoff
- returns an `IDF` object and a `live` flag in the wrapper layer
- current wrapper raises a runtime error on fetch failure rather than returning a local fallback dataset

### USDA SDA / SSURGO

Used by:

- watershed-scale soil composition
- watershed-scale surface texture composition
- site-scale soil lookup for BRC and PP
- clipped soil polygons for mapping

Purpose:

- hydrologic soil groups
- surface soil texture
- supporting geometry for map displays

Behavior:

- watershed soil composition is required for Peak Runoff and raises on failure
- watershed soil texture is optional for display and returns an empty result on failure
- BRC/PP site lookup is optional and non-blocking
- dual HSG values are simplified to the first class

### MRLC NLCD WCS

Used by:

- watershed land-use composition
- NLCD raster display
- pixel-level NLCD x SSURGO intersection

Purpose:

- convert NLCD land-cover pixels into the project-specific land-use categories used for `C` and CN calculations

Behavior:

- land-use composition is required for Peak Runoff and raises on failure
- NLCD raster display is optional and returns `(None, False)` on failure
- the exact land-use x soil intersection is preferred but optional

### USGS 3DEP

Used by:

- `fetch_dem_features(...)`

Purpose:

- DEM-backed slope, elevation, and flow-path metrics
- masked elevation array for map and report output

Behavior:

- optional and failure-tolerant
- primary path uses `py3dep.get_dem(...)`
- fallback path tries direct DEM download endpoints
- failures return a structured error dict instead of stopping the overall Peak Runoff workflow

### U.S. Census Geocoder

Used by:

- `geocode_address(...)`

Purpose:

- convert a typed address into coordinates for BRC and PP site-scale soil lookup

Behavior:

- completely optional
- returns `({}, False)` when no match is found or the request fails

## External-Service Dependency Matrix

| Service | Required for tool completion? | Fallback behavior |
| --- | --- | --- |
| StreamStats delineation | Required only when user starts Peak Runoff from a point | user can upload a watershed instead |
| StreamStats basin characteristics | No | empty dict |
| StreamStats regression flows | No | empty list |
| NOAA Atlas 14 | Yes for Peak Runoff | no local fallback |
| SSURGO soil composition | Yes for Peak Runoff | no local fallback |
| SSURGO soil texture | No | empty dict |
| NLCD land-use composition | Yes for Peak Runoff | no local fallback |
| NLCD x SSURGO intersection | No | separate soil and land-use fetches |
| DEM / 3DEP | No | warning only; Tc options reduced |
| Census geocoder | No | manual coordinates or map click |

## Bundled Repository Data

### `reference_data.py`

This file is the central static data source for the Peak Runoff tool. It includes:

- soil hydrologic group labels
- land-use categories used by the app
- Rational Method coefficients `c_coeff`
- CN values by land use and HSG
- NLCD-to-land-use mapping
- return periods
- SCS `qu` table and its lookup axes
- SCS Type II cumulative mass curve
- dimensionless unit hydrograph ordinates

Operationally, this means:

- changes to CN or `C` values affect all Peak Runoff results
- NLCD remapping changes both display composition and hydrologic outputs
- method documentation must stay aligned with this file

### `tanks_rwh.csv`

This file is used only by the Rainwater Harvesting page.

Important columns used by the current code:

- `name`
- `capacity_gal`
- `diameter_in`
- `length_in`
- `width_in`
- `height_in`
- `sku`
- `price`
- `material`
- `brand`
- `url`

Selection behavior:

- the loader normalizes numeric fields
- the auto-selector considers only entries with usable circular geometry
- the selected record is the smallest adequate qualifying tank by capacity
- when no qualifying tank is adequate, the selector falls back to the largest qualifying circular tank

## Geospatial Runtime Assumptions

The repository assumes the local environment can support:

- GeoPandas geometry reads and clipping
- PyProj coordinate transforms
- Rasterio-backed raster reads and masks
- RioXarray operations in the DEM workflow
- Pysheds flow-routing calculations

Important implementation details:

- `app_peak.py` tries to set `PROJ_DATA` before geospatial imports
- the NLCD WCS path treats the raster as EPSG:5070 even if the returned CRS metadata is unhelpful
- the DEM workflow contains NumPy compatibility patches for older geospatial dependencies and `pysheds`

## Data Quality And Interpretation Notes

- SSURGO texture results are area-weighted by clipped polygon area.
- Site-scale texture lookup for BRC and PP uses a small buffered polygon, not a single-point query.
- NLCD class percentages exclude unclassified or filtered values.
- Watershed area may come from uploaded geometry area, StreamStats geometry properties, or basin characteristics depending on the path used.
- Report consumers should treat USGS regression flows as reference values, not as the sole modeled answer from the app.
