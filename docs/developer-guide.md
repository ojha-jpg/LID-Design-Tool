# Developer Guide

## Purpose

This guide is for engineers maintaining or extending the current Streamlit app, not for end users running design scenarios.

## Local Setup

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Key expectations:

- Python environment management is external to the repository.
- The codebase is a flat module layout, not a packaged library.
- Peak Runoff development requires working geospatial dependencies and network access to public services.

## Running And Debugging

### Run The Full App

```bash
streamlit run app.py
```

### Page Entry Points

These `main()` functions are the public page entrypoints used by `app.py`:

- `app_brc.main()`
- `app_pp.main()`
- `app_rwh.main()`
- `app_peak.main()`

If you debug a single page in isolation, preserve the same Streamlit page-config assumptions used at the bottom of each module.

## Codebase Responsibilities

### UI Hub

- `app.py` should stay thin.
- Add page registration and home-page entry cards here.
- Avoid moving page-specific logic into the hub.

### LID Facility Tools

- Keep BRC, PP, and RWH calculations close to their own UIs unless there is a clear cross-tool reuse case.
- These modules own their own PDF report formatting today.
- Keep BRC/PP soil-lookup behavior synchronized when making UX or integration changes because they intentionally follow parallel patterns.

### Shared Hydrology And Data Layers

- `api_clients.py` is the integration boundary. New public-service calls should normally start here.
- `hydrology.py` should remain side-effect free.
- `noaa_atlas14.py` should remain responsible for Atlas 14 parsing and interpolation, not Streamlit concerns.
- `reference_data.py` should remain the single place for static hydrology lookup data.

## Adding A New Tool Page

1. Create a new `app_<tool>.py` module with a `main()` entrypoint.
2. Keep page-specific equations and PDF/report logic local to that module unless multiple pages will reuse them.
3. Register the page in `app.py` with `st.Page(...)`.
4. Add a launch card on the home page.
5. Update `README.md` and the relevant docs page.

If the new page needs public data:

- add network calls in `api_clients.py`
- keep transformation and error-handling semantics explicit
- document whether the dependency is required, optional, or fallback-capable

## Updating Formulas Or Reference Tables

When changing formulas:

- update the code first
- update the relevant documentation page second
- keep units explicit in both places
- check whether PDFs, labels, captions, and help text also need updates

When changing `reference_data.py`:

- review `hydrology.py`
- review `app_peak.py` displays and labels
- review `docs/hydrology-and-methods.md`
- verify NLCD remapping still matches the intended project categories

When changing `tanks_rwh.csv`:

- preserve the columns expected by `load_tanks_df()`
- verify automatic selection still works on rows with normalized numeric dimensions
- confirm product links and price fields are still optional-safe

## Session-State Maintenance

Peak Runoff relies heavily on `st.session_state`. When modifying it:

- preserve the meaning of the `step` key
- keep reset behavior aligned with `_reset()`
- be deliberate about which expensive API results are cached
- avoid introducing keys that collide with BRC or PP lookup-state keys

BRC and PP also use session state for map selection and address lookup. Maintain the current namespacing pattern with `brc_...` and `pp_...` prefixes.

## Reporting Paths

- BRC, PP, and RWH use ReportLab PDF generation.
- Peak Runoff uses HTML report generation with inline-encoded figures.

If report content changes:

- verify the UI labels and report labels still match
- keep report generation side-effect free
- ensure user-facing outputs remain understandable without reading the app source

## Documentation Maintenance Rules

- Markdown is the repository documentation format.
- Mermaid is the only diagram format.
- Code is the source of truth when docs become stale.
- User workflow docs should not be mixed with low-level implementation details unless the distinction matters.
- Formula documentation must state units and major assumptions.
- External-service documentation must state required vs optional vs fallback behavior.

## Verification Expectations

There is no automated test suite in the current repository. Documentation and maintenance changes should therefore include manual verification:

- confirm module names, file names, and function names still exist
- verify run instructions still work from the repository root
- compare documented workflow steps against actual UI order
- confirm downloads still match their documented format
- check that Tulsa/Oklahoma-specific assumptions remain explicit

For integration-heavy changes, especially in Peak Runoff, also verify that failure states degrade the way the docs describe them.
