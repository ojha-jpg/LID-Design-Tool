# LID Design Tools

Streamlit-based stormwater design and hydrology tools aligned to the City of Tulsa Low Impact Development (LID) Manual and Oklahoma-focused hydrologic datasets.

This repository is a single multi-page application with four engineering tools:

| Tool | Primary reference | What it does |
| --- | --- | --- |
| Bioretention Cell (`app_brc.py`) | Tulsa LID Manual Chapter 101 | Sizes a bioretention cell, checks storage and drawdown criteria, and can size an optional underdrain outlet. |
| Permeable Pavement (`app_pp.py`) | Tulsa LID Manual Chapter 103 | Sizes a permeable pavement system, evaluates underdrain requirements, and checks storage, drawdown, and loading ratio. |
| Rainwater Harvesting (`app_rwh.py`) | Tulsa LID Manual Section 104 | Computes storage needs, auto-selects a tank from the bundled catalog, sizes the slow-release orifice, and generates a PDF summary. |
| Peak Runoff Analysis (`app_peak.py`) | NRCS TR-55 + Rational Method | Delineates a watershed or accepts an uploaded boundary, gathers precipitation/soil/land-cover/elevation data, and calculates peak discharge. |

## Scope And Assumptions

- The codebase is Tulsa/Oklahoma-oriented today. The UI, formulas, defaults, rainfall assumptions, and external-service usage should be documented and interpreted that way.
- The Peak Runoff tool assumes Oklahoma coverage for StreamStats, Atlas 14 volume selection, and the standard Type II rainfall distribution used in this implementation.
- Code is the source of truth when README wording, inline comments, or manual text differ.
- Documentation is written directly in Markdown in this repository. There is no generated documentation site.

## Project Layout

```text
app.py              Streamlit entry point and page navigation hub
app_brc.py          Bioretention Cell design workflow and PDF export
app_pp.py           Permeable Pavement design workflow and PDF export
app_rwh.py          Rainwater Harvesting workflow, tank selection, and PDF export
app_peak.py         Peak Runoff wizard, maps, charts, CSV export, HTML report export
api_clients.py      External API integrations and geospatial data acquisition
hydrology.py        Pure hydrology calculations and Tc helpers
noaa_atlas14.py     NOAA Atlas 14 fetcher and interpolated IDF model
reference_data.py   Static lookup tables, land-use mappings, and hydrology reference arrays
tanks_rwh.csv       Bundled commercial rainwater tank catalog
docs/               Project, architecture, methods, integrations, and maintenance docs
```

## Run Locally

1. Create and activate a Python environment.
2. Install dependencies.
3. Launch Streamlit from the repository root.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The app entry point is [`app.py`](/Users/ashishojha/Documents/LID excels/app.py).

## Environment Notes

- Several dependencies are geospatial and may require native libraries or prebuilt wheels depending on your platform: `geopandas`, `rasterio`, `pyproj`, `rioxarray`, `contextily`, and `py3dep`.
- [`app_peak.py`](/Users/ashishojha/Documents/LID excels/app_peak.py) sets `PROJ_DATA` at import time if it can find a local PROJ data directory. This is important for coordinate transforms in the Peak Runoff workflow.
- The Peak Runoff tool depends on outbound HTTP access to public APIs. The other tools run locally except for optional address geocoding and site-scale soil lookup.
- No API keys are required by the current implementation.

## Shared Architecture

The application is organized around a thin UI hub and a few shared service modules:

- [`app.py`](/Users/ashishojha/Documents/LID excels/app.py) registers four pages with `st.Page(...)` and groups them into a single `st.navigation(...)` app shell.
- The three LID sizing tools each own their full UI, calculations, and one-page PDF export.
- [`app_peak.py`](/Users/ashishojha/Documents/LID excels/app_peak.py) is a stateful five-step wizard with map rendering, data collection, charts, CSV export, and a self-contained HTML report generator.
- [`api_clients.py`](/Users/ashishojha/Documents/LID excels/api_clients.py) is the external-service boundary for watershed delineation, precipitation, soils, land cover, geocoding, and DEM acquisition.
- [`hydrology.py`](/Users/ashishojha/Documents/LID excels/hydrology.py), [`noaa_atlas14.py`](/Users/ashishojha/Documents/LID excels/noaa_atlas14.py), and [`reference_data.py`](/Users/ashishojha/Documents/LID excels/reference_data.py) are the core computational and reference-data layers.

## External Data Sources

The current code integrates with these public services:

| Service | Used by | Purpose |
| --- | --- | --- |
| USGS StreamStats | Peak Runoff | Watershed delineation, basin characteristics, regression-flow reference values |
| NOAA Atlas 14 | Peak Runoff | Design storm depth and intensity tables |
| USDA SDA / SSURGO | Peak Runoff, BRC, PP | Hydrologic soil groups and soil texture lookups |
| MRLC NLCD WCS | Peak Runoff | Land-cover composition and map imagery |
| USGS 3DEP | Peak Runoff | DEM-backed flow length, slope, and elevation summaries |
| U.S. Census Geocoder | BRC, PP | Address-to-coordinate geocoding for site soil lookup |

## Bundled Data

- [`reference_data.py`](/Users/ashishojha/Documents/LID excels/reference_data.py) contains return periods, land-use mappings, runoff coefficients, CN tables, and hydrology lookup arrays used by the Peak Runoff calculations.
- [`tanks_rwh.csv`](/Users/ashishojha/Documents/LID excels/tanks_rwh.csv) contains the tank catalog used by the Rainwater Harvesting tool for automatic tank preselection.

## Documentation Map

- [Architecture](docs/architecture.md)
- [Tool Reference](docs/tool-reference.md)
- [Hydrology And Methods](docs/hydrology-and-methods.md)
- [Integrations And Data](docs/integrations-and-data.md)
- [Developer Guide](docs/developer-guide.md)

## Documentation Conventions

- Markdown is the source format for repository documentation.
- Mermaid is the only diagram format used in the docs bundle.
- Formula descriptions always state units and implementation assumptions.
- External integrations are described as required, optional, or fallback-capable based on actual code behavior.
- User-facing workflow descriptions are kept separate from implementation details where possible.
