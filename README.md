# LID Design Tools

**City of Tulsa Low Impact Development (LID) Manual (2026)**  
University of Oklahoma · Stormwater Engineering

A web-based suite of stormwater design tools built with Streamlit. All tools run entirely from public APIs — no local spatial data files required.

## Tools

| Tool | Reference | Description |
|------|-----------|-------------|
| **Bioretention Cell (BRC)** | Chapter 101 | 10-step design: site, SWV, ponding depth, media, underdrain, orifice, overflow |
| **Permeable Pavement (PP)** | Chapter 103 | Storage depth, subbase porosity, underdrain, orifice sizing |
| **Rainwater Harvesting (RWH)** | Section 104 | Catchment, first flush, tank selection, orifice, diverter pipe sizing |
| **Peak Runoff Analysis** | NRCS TR-55 | Watershed delineation, NOAA Atlas 14, SSURGO soils, NLCD land use, CN + Rational method |

## Run Locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Architecture

```
app.py              ← entry point: st.navigation() hub (4 pages)
app_peak.py         ← Peak Runoff tool (5-step wizard)
app_brc.py          ← Bioretention Cell design tool
app_pp.py           ← Permeable Pavement design tool
app_rwh.py          ← Rainwater Harvesting design tool
api_clients.py      ← all external API calls
hydrology.py        ← pure hydrology calculations (CN, Rational, SCS UH)
noaa_atlas14.py     ← NOAA Atlas 14 IDF fetcher + log-log interpolator
reference_data.py   ← constants, CN tables, NLCD class mappings
tanks_rwh.csv       ← commercial RWH tank database
```

## External APIs

The Peak Runoff tool uses these public APIs (no keys required):

| API | Purpose |
|-----|---------|
| USGS StreamStats | Watershed delineation, basin characteristics, regression peak flows |
| NOAA Atlas 14 (HDSC) | Precipitation frequency (IDF) data |
| USDA SDA (SSURGO) | Hydrologic soil groups, surface texture |
| MRLC WCS (NLCD 2024) | Land cover classification |
| USGS 3DEP | DEM elevation for slope and flow length |

