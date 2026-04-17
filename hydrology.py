"""
hydrology.py — shared hydrology calculations for the Peak Runoff workflow.

The functions in this module are deliberately side-effect free and unit-explicit.
They translate already-fetched precipitation, soil, land-use, and watershed
metrics into CN, Rational Method, and hydrograph outputs without doing any I/O.
"""

import bisect
from reference_data import LANDUSE_TYPES, QU_TABLE, QU_TC_VALUES, QU_IAP_VALUES, SCS_TYPE_II_MASS_CURVE, SCS_DUH


# ---------------------------------------------------------------------------
# Composite CN and C
# ---------------------------------------------------------------------------

def _cn_for_soil(landuse_key: str, soil_group: str) -> float:
    """Return CN for a specific landuse + soil hydrologic group."""
    lu = LANDUSE_TYPES[landuse_key]
    return float(lu[f"cn_{soil_group.lower()}"])


def composite_cn(
    soil_pct: dict[str, float],
    landuse_pct: dict[str, float],
) -> float:
    """
    Area-weighted composite curve number using marginal percentages.

    soil_pct:    {"A": 15.0, "B": 45.0, "C": 30.0, "D": 10.0}  (sums to 100)
    landuse_pct: {"Row Crops": 60.0, "Pasture/Meadow": 40.0}     (sums to 100)

    Returns composite CN (dimensionless).
    This path assumes land use and soil group are statistically independent.
    """
    cn = 0.0
    soil_fracs = {g: pct / 100.0 for g, pct in soil_pct.items()}
    lu_fracs = {lu: pct / 100.0 for lu, pct in landuse_pct.items() if lu in LANDUSE_TYPES}

    for lu_key, lu_frac in lu_fracs.items():
        for soil_group, soil_frac in soil_fracs.items():
            cn += _cn_for_soil(lu_key, soil_group) * soil_frac * lu_frac

    return round(cn, 2)


def composite_cn_from_intersection(intersection_pct: dict) -> float:
    """
    Compute composite CN from a spatial (land use, HSG) intersection.

    intersection_pct: {(lu_key, hsg): area_pct} from fetch_landuse_soil_intersection()
    Values should sum to ~100.

    Each (lu, hsg) pair contributes its exact CN weighted by its true area fraction —
    no statistical-independence assumption between soils and land use.
    """
    cn = 0.0
    for (lu_key, hsg), pct in intersection_pct.items():
        if lu_key in LANDUSE_TYPES:
            cn += _cn_for_soil(lu_key, hsg) * (pct / 100.0)
    return round(cn, 2)


def composite_c(landuse_pct: dict[str, float]) -> float:
    """
    Area-weighted composite rational method runoff coefficient C.

    landuse_pct: {"Row Crops": 60.0, "Pasture/Meadow": 40.0}  (sums to 100)

    Returns composite C (dimensionless, 0–1).
    """
    c = 0.0
    for lu_key, pct in landuse_pct.items():
        if lu_key in LANDUSE_TYPES:
            c += LANDUSE_TYPES[lu_key]["c_coeff"] * (pct / 100.0)
    return round(c, 3)


# ---------------------------------------------------------------------------
# CN Method — NRCS TR-55
# ---------------------------------------------------------------------------

def cn_runoff_depth(CN: float, P_24hr_in: float) -> float:
    """
    Compute runoff depth Q (inches) using NRCS TR-55 curve number method.

    CN:         composite curve number (dimensionless)
    P_24hr_in:  24-hour rainfall depth (inches)

    Returns runoff depth in inches. Returns 0 if P <= Ia.
    """
    if CN <= 0 or CN >= 100:
        raise ValueError(f"CN must be between 0 and 100, got {CN}")
    S = (1000.0 / CN) - 10.0          # potential max retention (inches)
    Ia = 0.2 * S                       # initial abstraction (inches)
    if P_24hr_in <= Ia:
        return 0.0
    return (P_24hr_in - Ia) ** 2 / (P_24hr_in + 0.8 * S)


def _interpolate_qu(tc_hr: float, ia_p: float) -> float:
    """
    Bilinear interpolation of qu from QU_TABLE (SCS Exhibit 4-II).

    tc_hr:  time of concentration (hours), clamped to [0.1, 10.0]
    ia_p:   Ia/P ratio, clamped to [0.10, 0.50]

    Returns qu in cfs/mi^2/in.
    """
    # Clamp inputs to table bounds
    tc_hr = max(0.1, min(10.0, tc_hr))
    ia_p = max(0.10, min(0.50, ia_p))

    # Find bracketing Tc values
    tc_vals = QU_TC_VALUES
    if tc_hr <= tc_vals[0]:
        tc_lo = tc_hi = tc_vals[0]
    elif tc_hr >= tc_vals[-1]:
        tc_lo = tc_hi = tc_vals[-1]
    else:
        idx = bisect.bisect_right(tc_vals, tc_hr)
        tc_lo, tc_hi = tc_vals[idx - 1], tc_vals[idx]

    # Find bracketing Ia/P values
    iap_vals = QU_IAP_VALUES
    if ia_p <= iap_vals[0]:
        iap_lo = iap_hi = iap_vals[0]
    elif ia_p >= iap_vals[-1]:
        iap_lo = iap_hi = iap_vals[-1]
    else:
        idx = bisect.bisect_right(iap_vals, ia_p)
        iap_lo, iap_hi = iap_vals[idx - 1], iap_vals[idx]

    # Bilinear interpolation
    def get(tc, iap):
        return QU_TABLE[tc][iap]

    if tc_lo == tc_hi and iap_lo == iap_hi:
        return get(tc_lo, iap_lo)

    if tc_lo == tc_hi:
        # Interpolate only on Ia/P
        q_lo = get(tc_lo, iap_lo)
        q_hi = get(tc_lo, iap_hi)
        t = (ia_p - iap_lo) / (iap_hi - iap_lo) if iap_hi != iap_lo else 0.0
        return q_lo + t * (q_hi - q_lo)

    if iap_lo == iap_hi:
        # Interpolate only on Tc
        q_lo = get(tc_lo, iap_lo)
        q_hi = get(tc_hi, iap_lo)
        t = (tc_hr - tc_lo) / (tc_hi - tc_lo) if tc_hi != tc_lo else 0.0
        return q_lo + t * (q_hi - q_lo)

    # Full bilinear interpolation
    t_tc = (tc_hr - tc_lo) / (tc_hi - tc_lo)
    t_iap = (ia_p - iap_lo) / (iap_hi - iap_lo)
    q11 = get(tc_lo, iap_lo)
    q12 = get(tc_lo, iap_hi)
    q21 = get(tc_hi, iap_lo)
    q22 = get(tc_hi, iap_hi)
    return (
        q11 * (1 - t_tc) * (1 - t_iap)
        + q12 * (1 - t_tc) * t_iap
        + q21 * t_tc * (1 - t_iap)
        + q22 * t_tc * t_iap
    )


# ---------------------------------------------------------------------------
# SCS Type II mass curve helpers
# ---------------------------------------------------------------------------

def _interp_mass_curve(t_hr: float) -> float:
    """Cumulative fraction F(t) from SCS Type II mass curve via linear interpolation."""
    times = [t for t, _ in SCS_TYPE_II_MASS_CURVE]
    fracs = [f for _, f in SCS_TYPE_II_MASS_CURVE]
    t = max(times[0], min(times[-1], t_hr))
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return fracs[0]
    if idx >= len(times):
        return fracs[-1]
    t0, t1 = times[idx - 1], times[idx]
    f0, f1 = fracs[idx - 1], fracs[idx]
    return f0 if t1 == t0 else f0 + (f1 - f0) * (t - t0) / (t1 - t0)


def build_storm_table(
    P_D: float,
    duration_hr: float,
    CN: float,
    dt: float = 0.25,
) -> list[dict]:
    """
    Build incremental SCS runoff table for a design storm.

    P_D         : total storm depth for this duration (inches) — from NOAA Atlas 14
                  directly for the selected duration and return period
    duration_hr : storm duration (hours); window = [12-D/2, 12+D/2] of Type II curve
    CN          : composite curve number
    dt          : time step (hours, default 0.25)

    The SCS Type II temporal pattern is normalized within the storm window so that
    cumulative depth reaches exactly P_D at t_end.

    Returns list of dicts with keys:
        Time (hr), Cumul. Fraction, Depth (in), Incremental Depth (in),
        Accumulated Effective Runoff (in), Incremental Effective Runoff (in)
    """
    S  = (1000.0 / CN) - 10.0
    Ia = 0.2 * S

    t_start  = 12.0 - duration_hr / 2.0
    t_end    = 12.0 + duration_hr / 2.0
    F_start  = _interp_mass_curve(t_start)
    F_end    = _interp_mass_curve(t_end)
    F_range  = F_end - F_start          # fraction of 24-hr rain in this window

    rows = []
    prev_cum_depth  = 0.0
    prev_cum_runoff = 0.0

    t = t_start
    while t <= t_end + 1e-9:
        F           = _interp_mass_curve(t)
        norm_frac   = (F - F_start) / F_range   # 0 at t_start → 1 at t_end
        cum_depth   = norm_frac * P_D
        incr_depth  = cum_depth - prev_cum_depth

        if cum_depth > Ia:
            cum_runoff = (cum_depth - Ia) ** 2 / (cum_depth + 0.8 * S)
        else:
            cum_runoff = 0.0
        incr_runoff = cum_runoff - prev_cum_runoff

        rows.append({
            "Time (hr)":                         round(t, 2),
            "Cumul. Fraction":                   round(norm_frac, 3),
            "Depth (in)":                        round(cum_depth, 2),
            "Incremental Depth (in)":            round(incr_depth, 2),
            "Accumulated Effective Runoff (in)": round(cum_runoff, 2),
            "Incremental Effective Runoff (in)": round(incr_runoff, 2),
        })
        prev_cum_depth  = cum_depth
        prev_cum_runoff = cum_runoff
        t = round(t + dt, 10)

    return rows


def cn_peak_flow(
    CN: float,
    P_D: float,
    A_sqmi: float,
    Tc_hr: float,
    storm_duration_hr: float = 24,
) -> float:
    """
    Peak discharge (cfs) via SCS TR-55 method: qp = qu x A x Q

    CN:                composite curve number
    P_D:               storm depth (inches) for the design duration — from Atlas 14
    A_sqmi:            watershed area (square miles)
    Tc_hr:             time of concentration (hours)
    storm_duration_hr: design storm duration (hours, default 24)

    Returns peak discharge in cfs. Returns 0 if no runoff occurs.
    """
    Q = cn_runoff_depth(CN, P_D)
    if Q == 0.0:
        return 0.0

    S    = (1000.0 / CN) - 10.0
    Ia_P = max(0.10, min(0.50, (0.2 * S) / P_D))

    qu = _interpolate_qu(Tc_hr, Ia_P)
    return round(qu * A_sqmi * Q, 1)


# ---------------------------------------------------------------------------
# SCS Unit Hydrograph — convolution-based peak flow (any storm duration)
# ---------------------------------------------------------------------------

def _interp_duh(t_tp: float) -> float:
    """Linear interpolation of SCS dimensionless unit hydrograph q/qp for t/tp."""
    times  = [t for t, _ in SCS_DUH]
    ratios = [q for _, q in SCS_DUH]
    t = max(times[0], min(times[-1], t_tp))
    idx = bisect.bisect_right(times, t)
    if idx == 0:
        return ratios[0]
    if idx >= len(times):
        return ratios[-1]
    t0, t1 = times[idx - 1], times[idx]
    q0, q1 = ratios[idx - 1], ratios[idx]
    return q0 if t1 == t0 else q0 + (q1 - q0) * (t - t0) / (t1 - t0)


def scs_uh_peak_flow(
    CN: float,
    P_D: float,
    A_sqmi: float,
    Tc_hr: float,
    duration_hr: float,
    dt: float = 0.25,
) -> float:
    """
    Peak discharge (cfs) via SCS unit hydrograph convolution.

    Uses the central duration_hr window of the SCS Type II mass curve scaled
    to P_D (Atlas 14 depth for the design duration) to generate incremental
    runoff depths, then convolves with the SCS dimensionless unit hydrograph.

    CN          : composite curve number
    P_D         : storm depth (inches) for the design duration
    A_sqmi      : watershed area (square miles)
    Tc_hr       : time of concentration (hours)
    duration_hr : design storm duration (hours)
    dt          : time step (hours, default 0.25)

    This is the primary CN-based peak-flow path used by ``app_peak.py``.
    """
    tbl = build_storm_table(P_D, duration_hr, CN, dt)
    incr_runoff = [row["Incremental Effective Runoff (in)"] for row in tbl]

    if sum(incr_runoff) == 0.0:
        return 0.0

    tlag = 0.6 * Tc_hr
    tp   = dt / 2.0 + tlag
    qp   = 484.0 * A_sqmi / tp

    n_uh = max(int(5.0 * tp / dt) + 1, 20)
    uh   = [_interp_duh((i * dt) / tp) * qp for i in range(n_uh)]

    n_out = len(incr_runoff) + n_uh - 1
    flow  = [0.0] * n_out
    for i, q in enumerate(incr_runoff):
        if q > 0.0:
            for j, u in enumerate(uh):
                flow[i + j] += q * u

    return round(max(flow), 1)


def scs_uh_hydrograph(
    CN: float,
    P_D: float,
    A_sqmi: float,
    Tc_hr: float,
    duration_hr: float,
    dt: float = 0.25,
) -> dict:
    """
    Run the SCS UH convolution and return all intermediate arrays for display.

    Returns a dict with keys:
      "storm_table"   — list[dict] from build_storm_table()
      "uh_times"      — list[float] hours from UH start [0, dt, 2dt, ...]
      "uh_ordinates"  — list[float] UH ordinates in cfs/in
      "drh_times"     — list[float] hours from storm start [0, dt, 2dt, ...]
      "drh_flow"      — list[float] direct runoff hydrograph in cfs
      "tp"            — float, time to peak of UH (hr)
      "peak_flow"     — float, max(drh_flow) in cfs
      "peak_time"     — float, time at peak_flow (hr)
    """
    tbl         = build_storm_table(P_D, duration_hr, CN, dt)
    incr_runoff = [row["Incremental Effective Runoff (in)"] for row in tbl]

    tlag = 0.6 * Tc_hr
    tp   = dt / 2.0 + tlag
    qp   = 484.0 * A_sqmi / tp

    n_uh = max(int(5.0 * tp / dt) + 1, 20)
    uh   = [_interp_duh((i * dt) / tp) * qp for i in range(n_uh)]

    n_out = len(incr_runoff) + n_uh - 1
    flow  = [0.0] * n_out
    if sum(incr_runoff) > 0.0:
        for i, q in enumerate(incr_runoff):
            if q > 0.0:
                for j, u in enumerate(uh):
                    flow[i + j] += q * u

    peak_flow = max(flow) if flow else 0.0
    peak_idx  = flow.index(peak_flow) if peak_flow > 0 else 0

    t_start = tbl[0]["Time (hr)"]

    return {
        "storm_table":  tbl,
        "uh_times":     [round(i * dt, 4) for i in range(n_uh)],
        "uh_ordinates": [round(v, 3) for v in uh],
        "drh_times":    [round(t_start + i * dt, 4) for i in range(n_out)],
        "drh_flow":     [round(v, 2) for v in flow],
        "tp":           round(tp, 3),
        "peak_flow":    round(peak_flow, 1),
        "peak_time":    round(t_start + peak_idx * dt, 3),
    }


# ---------------------------------------------------------------------------
# Rational Method
# ---------------------------------------------------------------------------

def rational_peak_flow(C: float, I_inhr: float, A_acres: float) -> float:
    """
    Peak discharge (cfs) via Rational Method: Q = C x I x A

    C:       composite runoff coefficient (dimensionless, 0-1)
    I_inhr:  design rainfall intensity (in/hr) — use 1-hr Atlas 14 value
    A_acres: watershed area in ACRES (not sq miles)

    Returns peak discharge in cfs.
    """
    return round(C * I_inhr * A_acres, 1)


# ---------------------------------------------------------------------------
# Utility conversions
# ---------------------------------------------------------------------------

def sqmi_to_acres(area_sqmi: float) -> float:
    return area_sqmi * 640.0


def acres_to_sqmi(area_acres: float) -> float:
    return area_acres / 640.0


def tc_scs_lag(L_ft: float, Y_pct: float, CN: float) -> float:
    """
    Time of concentration (hours) via the SCS lag equation:
        Tc = (L^0.8 × (S+1)^0.7) / (1440 × Y^0.5)

    L_ft  : hydraulic flow length (feet)
    Y_pct : average watershed slope (percent)
    CN    : composite curve number (used to compute S = 1000/CN − 10)
    """
    S = (1000.0 / CN) - 10.0
    return (L_ft ** 0.8 * (S + 1) ** 0.7) / (1440.0 * (Y_pct ** 0.5))


def tc_kirpich(L_ft: float, Y_pct: float) -> float:
    """
    Time of concentration (hours) via the Kirpich equation (U.S. customary):
        Tc(min) = 0.0078 * L^0.77 * S^-0.385

    L_ft  : hydraulic flow length (feet)
    Y_pct : average watershed slope (percent), converted to ft/ft as S = Y_pct / 100

    Returns Tc in hours.
    """
    if L_ft <= 0:
        raise ValueError(f"L_ft must be positive, got {L_ft}")
    if Y_pct <= 0:
        raise ValueError(f"Y_pct must be positive, got {Y_pct}")

    slope_ftft = Y_pct / 100.0
    tc_min = 0.0078 * (L_ft ** 0.77) * (slope_ftft ** -0.385)
    return tc_min / 60.0


def tlag_to_tc(tlag_hr: float) -> float:
    """Convert SCS lag time (TLAG) to time of concentration: Tc = TLAG / 0.6"""
    return tlag_hr / 0.6
