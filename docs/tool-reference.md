# Tool Reference

## Tool Summary

| Tool | Inputs | Main outputs | Downloads |
| --- | --- | --- | --- |
| BRC | drainage area, soil/infiltration, ponding/media depth, cell area, underdrain settings | storage, loading ratio, surface drawdown, total drawdown, optional outlet sizing | PDF |
| PP | drainage area, placement, soil/infiltration, pavement type, storage depth, underdrain settings | SWV, storage capacity, drawdown, loading ratio, optional outlet sizing | PDF |
| RWH | catchment area, irrigation demand, tank geometry/catalog, first-flush pipe size | storage requirement, tank verification, orifice size, detention time | PDF |
| Peak Runoff | point or watershed, storm duration, Tc source | CN/Rational peak flows, charts, watershed summaries, maps | CSV, HTML |

## Bioretention Cell (`app_brc.py`)

### Purpose

Implements the Chapter 101 bioretention design workflow for Tulsa-oriented LID sizing. The page combines local formula evaluation with optional live site-scale soil lookup.

### Inputs

- contributing impervious and pervious area
- BRC placement relative to the contributing drainage area
- native soil type and infiltration rate
- optional engineered media toggle and engineered infiltration rate
- design precipitation depth
- ponding depth and, when applicable, media depth
- cell area
- underdrain diameter and length when engineered media is used

### Workflow

1. Optional address or map-based site selection for USDA soil texture lookup.
2. Contributing area and placement selection.
3. Soil type and infiltration selection.
4. Decision between native-soil-only design and engineered-media-plus-underdrain design.
5. Manual sizing of ponding depth, media depth, and cell area.
6. Storage, loading ratio, and drawdown evaluation.
7. Optional underdrain outlet sizing when the design is valid and includes an underdrain.

### Design Checks

- loading ratio target of 3 percent
- storage capacity at least equal to required stormwater volume
- surface drawdown within the Chapter 101 limit
- total drawdown within the Chapter 101 limit
- ponding and media depth warnings when user-entered values exceed equation-derived maxima
- optional outlet detention-time check after rounding the orifice size

### Outputs

- summary metrics for impervious area, pervious area, infiltration rate, depths, and underdrain usage
- storage required vs storage provided
- surface drawdown and total drawdown metrics
- underdrain sizing and optional outlet sizing details
- pass/fail banner with human-readable corrective guidance

### Soil Lookup And Manual Fallback

- Live lookup uses address geocoding or clicked coordinates.
- Lookup retrieves USDA texture composition for a small sampled polygon rather than a watershed.
- The result is mapped into the design-soil categories used by the BRC infiltration table.
- Users can still override the inferred soil category and infiltration rate manually.

### Report Output

- one-page PDF report
- generated on demand from the current form state
- intended as a compact design summary rather than a full calculation workbook

## Permeable Pavement (`app_pp.py`)

### Purpose

Implements the Chapter 103 permeable pavement design workflow, including storage sizing, underdrain determination, loading-ratio evaluation, and optional outlet sizing.

### Inputs

- PP placement mode relative to the contributing drainage area
- impervious and pervious drainage area
- native soil type and infiltration rate
- subbase porosity
- pavement system type
- underdrain toggle or auto-required underdrain path
- underdrain diameter and length when used
- design precipitation depth
- selected pavement footprint area
- selected aggregate storage depth

### Workflow

1. Optional address or map-based USDA soil lookup.
2. Placement selection that changes how contributing impervious area is interpreted.
3. Infiltration selection and subbase porosity selection.
4. Maximum storage depth calculation to decide whether underdrain is required.
5. Manual adjustment of PP area and storage depth.
6. SWV, storage, drawdown, and loading-ratio evaluation.
7. Optional slow-release outlet sizing when the underdrain design is valid.

### Design Checks

- storage capacity vs required SWV
- storage depth vs the no-underdrain depth limit
- total drawdown vs 48-hour criterion
- loading ratio vs 3 percent target
- underdrain detention verification after rounding the outlet size

### Outputs

- total drainage area, PP area, infiltration rate, storage depth, and underdrain state
- SWV required and storage capacity
- total drawdown
- contributing impervious area and loading ratio
- inline validation messages with concrete redesign guidance

### Soil Lookup And Manual Fallback

- Uses the same site-scale soil lookup pattern as BRC.
- Inferred soil type only seeds the default infiltration rate.
- Users can always override soil type and infiltration values manually.

### Report Output

- one-page PDF report
- generated every render and exposed through a download button

## Rainwater Harvesting (`app_rwh.py`)

### Purpose

Implements the Section 104 rainwater harvesting sizing workflow for storage volume, tank verification, outlet sizing, and first-flush diverter sizing.

### Inputs

- catchment area
- optional irrigation or other non-stormwater demand inputs
- auto-filled or manually edited tank capacity, diameter, and usable height
- selected first-flush diverter pipe size

### Workflow

1. Enter catchment area.
2. Optionally add irrigation demand for the design month.
3. Compute stormwater volume, first-flush volume, optional irrigation volume, and total required volume.
4. Load the bundled tank catalog and preselect the smallest adequate cylindrical tank when possible.
5. Verify the storage height against the selected tank geometry.
6. Compute slow-release orifice size and detention time.
7. Size the first-flush diverter pipe by selected Schedule 40 pipe size.

### Design Checks

- storage height required must fit within the selected tank height
- computed usable head must be positive
- detention time must fall inside the configured target range
- rounded orifice size must not fall below the minimum allowed threshold

### Outputs

- volume summary metrics
- tank area, required storage height, and computed offset/orifice head
- calculated and rounded orifice diameter
- actual detention time and pass/fail state
- first-flush volume and diverter pipe selection
- explicit pass/fail banner with corrective guidance

### Tank Catalog Behavior

- Tank selection uses `tanks_rwh.csv`.
- Only rows with usable cylindrical dimensions are eligible for automatic sizing.
- The auto-selected record pre-fills the sidebar inputs but does not lock them.
- If no adequate tank exists, the app falls back to the largest qualifying circular tank or asks the user to enter dimensions manually.

### Report Output

- PDF generated only when the user presses the report button
- file name includes the current date

## Peak Runoff Analysis (`app_peak.py`)

### Purpose

Implements a five-step hydrology workflow that combines watershed delineation, public geospatial data collection, TR-55-style runoff calculations, Rational Method peak-flow calculations, and downloadable reporting.

### Step 1: Select A Stream Point Or Upload A Watershed

Inputs:

- uploaded watershed boundary file in GeoJSON, KML, or KMZ
- manually typed latitude/longitude
- clicked point on an interactive map

Behavior:

- uploaded boundaries bypass StreamStats delineation
- uploaded files are converted into a watershed object compatible with the rest of the app
- clicked points are stored in session state and used later for delineation

### Step 2: Delineate Watershed

Inputs:

- selected latitude and longitude, unless an uploaded watershed was already accepted

Behavior:

- calls USGS StreamStats to delineate the watershed
- tries to retrieve basin characteristics for the resulting workspace
- calculates area and derives a default Tc from TLAG when available
- displays the watershed boundary and pour point on a map

### Step 3: Collect Data

Data collected:

- NOAA Atlas 14 precipitation
- SSURGO soil composition
- SSURGO surface soil texture
- NLCD land-cover composition
- pixel-level NLCD x SSURGO intersection when available
- soil polygons for map rendering
- NLCD raster for map rendering
- USGS regression peak flows when a usable StreamStats workspace exists
- 3DEP-derived DEM features when available

Behavior:

- expensive calls are cached in `st.session_state`
- the app prefers the exact NLCD x SSURGO intersection when it can build it
- if the intersection is unavailable, soil and land-use percentages are fetched separately
- DEM failures are non-fatal; the rest of the workflow can continue

### Step 4: Calculate

Inputs:

- storm duration
- selected Tc source
- collected precipitation, soil, land-use, and watershed area data

Calculations:

- composite CN
- composite Rational coefficient `C`
- CN-method peak flow via SCS unit hydrograph convolution
- Rational Method peak flow using Atlas 14 intensity at Tc
- optional storm-table and hydrograph display for a selected return period

Tc source options:

- NRCS SCS lag equation when DEM flow length and slope exist
- Kirpich equation when DEM flow length and slope exist
- manual Tc entry

### Step 5: Results

Outputs:

- combined comparison table of CN, Rational, and optional USGS regression flows
- dedicated CN and Rational tables and charts
- watershed details tables for soil, texture, land use, and CN/C breakdown
- CSV export of the combined results
- HTML report export that embeds figures and tables

### Error Handling And Fallback

- uploaded watershed files can bypass StreamStats entirely
- soil and land-use data collection can fall back from pixel-level intersection to marginal compositions
- DEM analysis is optional and failure-tolerant
- regression flows are omitted when the StreamStats workspace is unavailable
- some displays show warnings rather than blocking progression

## Cross-Tool Notes

- BRC and PP share the same high-level pattern for address/map-based site soil lookup.
- RWH is fully local except for whatever product URLs are stored in the bundled CSV.
- Peak Runoff is the only tool with multi-step state progression, map-heavy rendering, and multiple download formats.
