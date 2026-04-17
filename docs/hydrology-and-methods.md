# Hydrology And Methods

## Ownership Of Computational Logic

- `app_brc.py`, `app_pp.py`, and `app_rwh.py` contain their own chapter-specific design equations because those workflows are page-local and are not reused elsewhere.
- `hydrology.py` contains reusable hydrologic calculations for the Peak Runoff tool only.
- `noaa_atlas14.py` contains precipitation-frequency parsing and interpolation logic.
- `reference_data.py` holds the static tables needed by `hydrology.py` and `app_peak.py`.

This split is intentional: LID facility sizing lives with each design page, while watershed hydrology is centralized.

## Units And Conventions

The code is unit-explicit but not unit-generic. Common patterns are:

- precipitation depth in inches
- rainfall intensity in inches per hour
- flow in cubic feet per second
- watershed area in square miles for CN-based peak-flow work
- watershed area in acres for the Rational Method
- time of concentration in hours internally, with minutes shown in the UI where useful
- DEM elevation in meters
- flow length in feet

When documenting or extending formulas, keep the code's current units. Do not silently convert to SI-only formulations.

## Composite Curve Number And Runoff Coefficient

### Land Use And Soil Inputs

The Peak Runoff tool derives hydrologic inputs from:

- hydrologic soil group percentages or exact `(land use, HSG)` intersections
- land-use percentages mapped from NLCD classes into the app's project-specific land-use categories

### `composite_cn(...)`

Used when soil and land-use percentages are available only as separate marginal distributions.

Behavior:

- converts percentages to fractions
- multiplies every land-use fraction by every soil-group fraction
- assumes land use and soil group are statistically independent
- sums the corresponding CN values from `reference_data.py`

This is the fallback path, not the preferred path.

### `composite_cn_from_intersection(...)`

Used when `fetch_landuse_soil_intersection(...)` succeeds.

Behavior:

- consumes exact `(land use, HSG)` area percentages
- weights each CN directly by its true area share
- avoids the independence assumption

This is the preferred path because it preserves the actual spatial overlap between land cover and soil groups.

### `composite_c(...)`

Builds a single Rational Method coefficient `C` from land-use percentages only. Soil group does not enter the Rational coefficient calculation in this implementation.

## CN Runoff Depth

`cn_runoff_depth(CN, P_24hr_in)` implements the NRCS curve-number runoff equation:

- `S = 1000 / CN - 10`
- `Ia = 0.2 * S`
- runoff depth is zero when precipitation does not exceed initial abstraction

Important implementation note:

- despite the parameter name `P_24hr_in`, the function is also used with the selected design-duration depth, not only literal 24-hour depths
- the surrounding documentation should therefore refer to it as "the storm depth used by the method in this app" rather than asserting strict 24-hour-only usage

## SCS Type II Temporal Distribution

### Reference Data

`reference_data.py` contains:

- `SCS_TYPE_II_MASS_CURVE`
- `QU_TABLE`
- `SCS_DUH`

These arrays drive the time-distribution and unit-hydrograph logic.

### `build_storm_table(...)`

This function:

- takes a total design-storm depth `P_D`
- selects a centered window of the SCS Type II cumulative mass curve for the chosen storm duration
- renormalizes that window so cumulative depth ends exactly at `P_D`
- computes incremental rainfall and incremental effective runoff by time step

The result is a table suitable for display and for downstream convolution.

## Peak Flow Methods

### `cn_peak_flow(...)`

Implements a TR-55 style peak-flow estimate using:

- computed runoff depth `Q`
- watershed area in square miles
- unit peak discharge `qu`

`qu` is obtained by bilinear interpolation in the reference table through `_interpolate_qu(...)`.

This is present in the shared module, but the main Peak Runoff page currently uses the unit-hydrograph-based path for the displayed CN peak-flow results.

### `scs_uh_peak_flow(...)`

This is the main CN-method peak-flow path used by the UI.

Behavior:

- generates incremental effective runoff with `build_storm_table(...)`
- computes SCS lag as `0.6 * Tc`
- derives unit-hydrograph peak `qp = 484 * A / tp`
- builds a dimensionless unit hydrograph
- convolves incremental runoff depth with the unit hydrograph
- returns the maximum direct runoff hydrograph ordinate

This allows arbitrary storm durations instead of hardwiring a 24-hour event.

### `scs_uh_hydrograph(...)`

Uses the same logic as `scs_uh_peak_flow(...)` but returns:

- the storm table
- unit hydrograph ordinates
- direct runoff hydrograph values
- time-to-peak and peak-flow metadata

The UI uses this for the step-4 storm analysis display.

### `rational_peak_flow(...)`

Implements `Q = C * I * A` with:

- `C` from land use
- `I` from Atlas 14 intensity evaluated at `Tc`
- `A` in acres

The method is simple in code and intentionally separate from the CN path.

## Time Of Concentration

Three Tc paths exist in the current implementation:

### 1. StreamStats TLAG

- fetched in step 2 from basin characteristics
- converted using `tlag_to_tc(tlag_hr) = tlag_hr / 0.6`
- used as the first available default when present

### 2. NRCS SCS Lag Equation

`tc_scs_lag(L_ft, Y_pct, CN)` uses:

- DEM-derived flow length `L`
- DEM-derived mean slope `Y`
- composite CN

This path only appears when DEM metrics are available.

### 3. Kirpich

`tc_kirpich(L_ft, Y_pct)` uses:

- DEM-derived flow length
- DEM-derived slope converted from percent to ft/ft

This is exposed as an alternative Tc source when the DEM workflow succeeds.

### Manual Override

The UI always allows manual Tc entry. This is important because:

- DEM metrics can be unavailable
- StreamStats TLAG may be missing
- project judgment may call for a different Tc than any automatically derived value

## NOAA Atlas 14 Handling

`noaa_atlas14.py` does not just fetch a table and expose fixed durations. It creates an interpolated `IDF` object.

### Fetch Path

- `fetch_idf(lat, lon)` calls the Atlas 14 CSV endpoint for English-unit PDS depth data
- `_parse_csv(...)` parses duration rows and return-period columns into NumPy arrays
- the `IDF` constructor builds a `RegularGridInterpolator`

### Interpolation Behavior

Current code behavior:

- interpolation is linear on the raw depth grid
- extrapolation is allowed at the edges by the SciPy interpolator configuration
- `intensity(duration_hr, ari_yr)` is computed as `depth / duration`

Important documentation note:

- the module header mentions log-log interpolation, but the current constructor uses a linear interpolator on raw values
- docs should reflect the actual implementation, not the header description

## Soil And Land-Use Spatial Intersection

`fetch_landuse_soil_intersection(...)` is a key methodological improvement over multiplying separate percentages.

It works by:

1. downloading an NLCD tile
2. clipping SSURGO polygons to the watershed
3. attaching a dominant HSG to each polygon
4. rasterizing HSG polygons to the NLCD grid
5. tallying valid `(land use, HSG)` pairs inside the watershed mask

This produces a distribution of exact area percentages that can be used for:

- composite CN
- marginal soil percentages
- marginal land-use percentages
- breakdown tables shown in the UI and reports

If this path fails, the app still works by collecting soil and land use separately.

## DEM-Derived Terrain Metrics

`fetch_dem_features(...)` uses a two-stage strategy:

- primary path: `py3dep.get_dem(...)`
- fallback path: direct WCS/ImageServer download

Derived metrics include:

- longest D8 flow path
- mean watershed slope
- min and max elevation
- masked DEM array for map display

These metrics affect:

- whether DEM summary cards appear
- whether SCS lag and Kirpich Tc options are available
- whether the HTML report includes terrain figures and metrics

DEM failure is treated as non-fatal.

## Important Simplifications And Constraints

- The Peak Runoff workflow is Oklahoma-specific in service coverage and rainfall assumptions.
- NLCD classes are mapped into a smaller project-specific land-use taxonomy rather than preserved as raw NLCD classes.
- Dual HSG classes such as `A/D` are simplified to the drained class by taking the first letter.
- The Rational coefficient `C` is land-use-only in this implementation.
- Reported CN peak discharge is based on the SCS unit hydrograph convolution path, not only the simpler `qu * A * Q` path.
- BRC, PP, and RWH equations are implemented locally in their page modules and are not reused by the Peak Runoff tool.
