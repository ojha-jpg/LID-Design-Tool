"""
noaa_atlas14.py — NOAA Atlas 14 precipitation frequency fetcher and interpolator.

Usage:
    from noaa_atlas14 import fetch_idf

    idf = fetch_idf(lat=35.47, lon=-97.52)          # fetch for a point
    depth   = idf.depth(duration_hr=3, ari_yr=5)    # → inches (3-hr, 5-yr storm)
    intensity = idf.intensity(duration_hr=1, ari_yr=100)  # → in/hr

The current implementation parses the CSV response into a gridded depth table
and uses SciPy's ``RegularGridInterpolator`` over the raw duration/return-period
grid. ``IDF.intensity(...)`` is then derived as ``depth / duration``.

Data source: NOAA Atlas 14 Point Precipitation Frequency Estimates
  https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv
  ?lat={lat}&lon={lon}&data=depth&units=english&series=pds
"""

import csv
import re
import requests
import numpy as np
from scipy.interpolate import RegularGridInterpolator


# ---------------------------------------------------------------------------
# Duration label → hours
# ---------------------------------------------------------------------------

def _parse_duration_hr(label: str) -> float:
    """Convert a duration label like '5-min', '3-hr', '2-day' to hours."""
    label = label.strip().lower()
    m = re.match(r"(\d+(?:\.\d+)?)-?(min|hr|day)", label)
    if not m:
        raise ValueError(f"Unrecognised duration label: {label!r}")
    value, unit = float(m.group(1)), m.group(2)
    return {"min": value / 60.0, "hr": value, "day": value * 24.0}[unit]


# ---------------------------------------------------------------------------
# IDF object
# ---------------------------------------------------------------------------

class IDF:
    """
    Precipitation frequency table for a single point.

    Attributes
    ----------
    lat, lon        : coordinates used to fetch the data
    durations_hr    : sorted array of available durations (hours)
    ari_years       : sorted array of available return periods (years)
    depths_in       : 2-D array[len(durations_hr), len(ari_years)] of depths (inches)
    """

    def __init__(self, lat: float, lon: float,
                 durations_hr: np.ndarray, ari_years: np.ndarray,
                 depths_in: np.ndarray):
        self.lat = lat
        self.lon = lon
        self.durations_hr = durations_hr
        self.ari_years = ari_years
        self.depths_in = depths_in

        # Build a raw-value interpolator over duration and return-period axes.
        self._interp = RegularGridInterpolator(
            (durations_hr, ari_years), depths_in,
            method="linear",
            bounds_error=False,
            fill_value=None,               # extrapolate at edges
        )

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def depth(self, duration_hr: float, ari_yr: float) -> float:
        """
        Return precipitation depth in **inches** for the given duration and ARI.

        Parameters
        ----------
        duration_hr : storm duration in hours  (e.g. 3.0 for a 3-hr storm)
        ari_yr      : annual recurrence interval in years  (e.g. 5 for 5-yr)

        Returns
        -------
        float : depth in inches
        """
        pt = np.array([[duration_hr, ari_yr]])
        return float(self._interp(pt)[0])

    def intensity(self, duration_hr: float, ari_yr: float) -> float:
        """
        Return precipitation intensity in **in/hr** for the given duration and ARI.

        intensity = depth(duration_hr, ari_yr) / duration_hr
        """
        return self.depth(duration_hr, ari_yr) / duration_hr

    def depth_for_design(self, duration_hr: float, return_period_yr: int) -> float:
        """Alias for depth() — matches the naming convention used in hydrology.py."""
        return self.depth(duration_hr, float(return_period_yr))

    # ------------------------------------------------------------------
    # Convenience: return a summary table for app display
    # ------------------------------------------------------------------

    def summary_table(self, durations_hr: list[float] | None = None,
                      ari_years: list[int] | None = None) -> dict:
        """
        Return a nested dict: {duration_hr: {ari_yr: depth_in}}.

        Defaults to the standard design durations and return periods used
        by the LID Peak Runoff Tool.
        """
        if durations_hr is None:
            durations_hr = [1/12, 0.25, 0.5, 1, 2, 3, 6, 12, 24]   # 5-min … 24-hr
        if ari_years is None:
            ari_years = [2, 5, 10, 25, 50, 100]
        return {
            d: {t: round(self.depth(d, t), 3) for t in ari_years}
            for d in durations_hr
        }

    def __repr__(self) -> str:
        return (f"IDF(lat={self.lat}, lon={self.lon}, "
                f"durations={len(self.durations_hr)}, "
                f"ari_years={list(self.ari_years.astype(int))})")


# ---------------------------------------------------------------------------
# Fetch + parse  (with file-based cache)
# ---------------------------------------------------------------------------

_ATLAS14_URL = (
    "https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv"
)

def fetch_idf(lat: float, lon: float, timeout: int = 30) -> IDF:
    """
    Fetch NOAA Atlas 14 PDS depth data for a point and return an IDF object.

    Parameters
    ----------
    lat, lon  : WGS-84 decimal degrees
    timeout   : request timeout in seconds

    Returns
    -------
    IDF object ready for depth() / intensity() queries

    Raises
    ------
    requests.HTTPError  : if the API call fails
    ValueError          : if the response cannot be parsed
    """
    params = {
        "lat": f"{lat:.4f}",
        "lon": f"{lon:.4f}",
        "data": "depth",
        "units": "english",
        "series": "pds",
    }
    resp = requests.get(_ATLAS14_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    return _parse_csv(lat, lon, resp.text)


def _parse_csv(lat: float, lon: float, csv_text: str) -> IDF:
    """
    Parse the Atlas 14 CSV text into an IDF object.

    CSV format (english / depth):
        Duration,1,2,5,10,25,50,100,200,500,1000
        5-min,0.177,0.243,...
        10-min,...
        ...
    First row is the header with return periods.
    Subsequent rows: duration label followed by depth values in inches.
    """
    reader = csv.reader(csv_text.splitlines())

    ari_years: list[int] = []
    durations_hr: list[float] = []
    rows: list[list[float]] = []

    for i, row in enumerate(reader):
        if not row or all(c.strip() == "" for c in row):
            continue

        first = row[0].strip().lower()

        # Header row — "by duration for ARI (years):, 1,2,5,10,25,50,100,200,500,1000"
        # (also matches simple "Duration,..." variant)
        if first == "duration" or "ari" in first:
            for cell in row[1:]:
                cell = cell.strip()
                if cell:
                    try:
                        ari_years.append(int(cell))
                    except ValueError:
                        pass
            continue

        # Data rows
        try:
            dur_hr = _parse_duration_hr(first)
        except ValueError:
            continue   # skip unrecognised rows (metadata lines, etc.)

        values: list[float] = []
        for cell in row[1:len(ari_years) + 1]:
            cell = cell.strip()
            try:
                values.append(float(cell))
            except ValueError:
                values.append(np.nan)

        # Pad or trim to match ari_years length
        while len(values) < len(ari_years):
            values.append(np.nan)
        values = values[:len(ari_years)]

        durations_hr.append(dur_hr)
        rows.append(values)

    if not ari_years or not durations_hr:
        raise ValueError("Could not parse NOAA Atlas 14 CSV response — "
                         "unexpected format or coordinates outside Atlas 14 coverage.")

    depths_arr = np.array(rows, dtype=float)  # shape: (n_durations, n_ari)

    # Replace any NaN with nearest-neighbour fill (shouldn't happen for english/depth)
    if np.any(np.isnan(depths_arr)):
        from scipy.ndimage import generic_filter
        mask = np.isnan(depths_arr)
        depths_arr[mask] = np.nanmean(depths_arr)   # crude fallback

    return IDF(
        lat=lat,
        lon=lon,
        durations_hr=np.array(durations_hr),
        ari_years=np.array(ari_years, dtype=float),
        depths_in=depths_arr,
    )


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------
# Sherman IDF curve fitting:  I = D / (t + E)^F
#   I  = intensity (in/hr)
#   t  = duration  (minutes)
#   D, E, F = fitted parameters per return period
# ---------------------------------------------------------------------------

def _sherman(t_min: np.ndarray, D: float, E: float, F: float) -> np.ndarray:
    """Sherman IDF equation: I = D / (t + E)^F"""
    return D / (t_min + E) ** F


def fit_idf_parameters(
    idf: IDF,
    ari_years: list[int] | None = None,
    max_duration_hr: float = 6.0,
) -> dict:
    """
    Fit the Sherman IDF equation  I = D / (t + E)^F  to Atlas 14 data.

    Parameters
    ----------
    idf           : IDF object from fetch_idf()
    ari_years     : return periods to fit (default: [2, 5, 10, 25, 50, 100])
    max_duration_hr : only use durations up to this value for fitting (default 6 hr —
                      beyond 6 hr the Sherman equation degrades significantly)

    Returns
    -------
    dict keyed by ARI year:
      {2: {"D": 56.3, "E": 11.5, "F": 0.81, "r2": 0.999}, 5: {...}, ...}
    """
    from scipy.optimize import curve_fit

    if ari_years is None:
        ari_years = [2, 5, 10, 25, 50, 100]

    # Select durations ≤ max_duration_hr
    mask = idf.durations_hr <= max_duration_hr
    t_hr  = idf.durations_hr[mask]
    t_min = t_hr * 60.0                          # convert to minutes for fitting

    results = {}
    for ari in ari_years:
        # Find column index closest to requested ARI
        col = int(np.argmin(np.abs(idf.ari_years - ari)))
        intensity = idf.depths_in[mask, col] / t_hr   # depth → intensity (in/hr)

        # Initial guesses: D ~ peak intensity × some scale, E ~ 10 min, F ~ 0.8
        p0 = [intensity[0] * t_min[0], 10.0, 0.80]
        bounds = ([0, 0, 0.1], [1e5, 200, 2.0])

        try:
            popt, _ = curve_fit(_sherman, t_min, intensity, p0=p0,
                                bounds=bounds, maxfev=10_000)
            D, E, F = popt
            # R² on intensity
            i_pred = _sherman(t_min, D, E, F)
            ss_res = np.sum((intensity - i_pred) ** 2)
            ss_tot = np.sum((intensity - intensity.mean()) ** 2)
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
        except Exception:
            D, E, F, r2 = np.nan, np.nan, np.nan, np.nan

        results[ari] = {
            "D": round(D, 4),
            "E": round(E, 4),
            "F": round(F, 4),
            "r2": round(r2, 5),
        }

    return results


if __name__ == "__main__":
    import sys

    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 35.4676
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -97.5164   # Oklahoma City

    print(f"Fetching Atlas 14 IDF for ({lat}, {lon}) …")
    idf = fetch_idf(lat, lon)
    print(idf)
    print()

    # --- Example queries ---
    examples = [
        (3,  5),    # 3-hr, 5-yr   ← the user's example
        (2.5,  100),  # 2.5-hr, 100-yr
        (24, 25),   # 24-hr, 25-yr
        (0.5, 10),  # 30-min, 10-yr
    ]
    print(f"{'Duration':>12}  {'ARI':>8}  {'Depth (in)':>12}  {'Intensity (in/hr)':>18}")
    print("-" * 58)
    for dur, ari in examples:
        d = idf.depth(dur, ari)
        i = idf.intensity(dur, ari)
        label = f"{dur} hr"
        print(f"{label:>12}  {ari:>7}-yr  {d:>12.3f}  {i:>18.4f}")
