"""
reference_data.py — Single source of truth for LID Peak Runoff Tool.

CN values are aligned to NRCS TR-55 Table 2-2 categories.
Rational Method C values are representative screening defaults and should be
checked against the governing local drainage manual for final design use.
qu table from SCS Exhibit 4-II (Type II rainfall distribution, applicable to Oklahoma).
"""

# ---------------------------------------------------------------------------
# Soil hydrologic groups
# ---------------------------------------------------------------------------

SOIL_TYPES = {
    "A": "Sand, loamy sand, or sandy loam (low runoff potential)",
    "B": "Silt loam or loam",
    "C": "Sandy clay loam",
    "D": "Clay loam, silty clay loam, sandy clay, silty clay, or clay (high runoff potential)",
}

# ---------------------------------------------------------------------------
# Land use types — 16 categories
# Each entry: c_coeff (Rational Method), cn_a/b/c/d (NRCS TR-55 Table 2-2)
# Developed-residential categories follow the TR-55 average imperviousness bins.
# ---------------------------------------------------------------------------

LANDUSE_TYPES = {
    "Row Crops":                {"c_coeff": 0.60, "cn_a": 72, "cn_b": 81, "cn_c": 88, "cn_d": 91},
    "Small Grain":              {"c_coeff": 0.45, "cn_a": 65, "cn_b": 76, "cn_c": 84, "cn_d": 88},
    "Close-seeded Legumes":     {"c_coeff": 0.40, "cn_a": 66, "cn_b": 77, "cn_c": 85, "cn_d": 89},
    "Fallow/Bare Soil":         {"c_coeff": 0.50, "cn_a": 77, "cn_b": 86, "cn_c": 91, "cn_d": 94},
    "Pasture/Meadow":           {"c_coeff": 0.35, "cn_a": 49, "cn_b": 69, "cn_c": 79, "cn_d": 84},
    "Brush":                    {"c_coeff": 0.35, "cn_a": 35, "cn_b": 56, "cn_c": 70, "cn_d": 77},
    "Woods (Light)":            {"c_coeff": 0.25, "cn_a": 36, "cn_b": 60, "cn_c": 73, "cn_d": 79},
    "Woods (Dense)":            {"c_coeff": 0.15, "cn_a": 30, "cn_b": 55, "cn_c": 70, "cn_d": 77},
    "Farmsteads":               {"c_coeff": 0.55, "cn_a": 59, "cn_b": 74, "cn_c": 82, "cn_d": 86},
    "Residential (1/8 acre)":   {"c_coeff": 0.65, "cn_a": 77, "cn_b": 85, "cn_c": 90, "cn_d": 92},
    "Residential (1/4 acre)":   {"c_coeff": 0.55, "cn_a": 61, "cn_b": 75, "cn_c": 83, "cn_d": 87},
    "Residential (1/2 acre)":   {"c_coeff": 0.45, "cn_a": 54, "cn_b": 70, "cn_c": 80, "cn_d": 85},
    "Commercial/Business":      {"c_coeff": 0.85, "cn_a": 89, "cn_b": 92, "cn_c": 94, "cn_d": 95},
    "Industrial":               {"c_coeff": 0.75, "cn_a": 81, "cn_b": 88, "cn_c": 91, "cn_d": 93},
    "Open Space/Lawns":         {"c_coeff": 0.25, "cn_a": 49, "cn_b": 69, "cn_c": 79, "cn_d": 84},
    "Paved Roads/Parking":      {"c_coeff": 0.95, "cn_a": 98, "cn_b": 98, "cn_c": 98, "cn_d": 98},
}

# Map Annual NLCD pixel values to our land-use categories.
# NLCD developed classes are cross-walked primarily by published imperviousness
# ranges (<20%, 20-49%, 50-79%, 80-100%). Wetland classes remain approximate
# proxies because TR-55 does not provide a direct NLCD wetland equivalent.
# See: https://www.mrlc.gov/data/legends/national-land-cover-database-class-legend-and-description
NLCD_TO_LANDUSE = {
    11: None,                      # Open Water — exclude from landuse calculation
    12: None,                      # Perennial Ice/Snow
    21: "Open Space/Lawns",       # Developed, Open Space
    22: "Residential (1/4 acre)", # Developed, Low Intensity
    23: "Residential (1/8 acre)", # Developed, Medium Intensity
    24: "Commercial/Business",    # Developed, High Intensity
    31: "Fallow/Bare Soil",       # Barren Land
    41: "Woods (Dense)",          # Deciduous Forest
    42: "Woods (Dense)",          # Evergreen Forest
    43: "Woods (Dense)",          # Mixed Forest
    52: "Brush",                   # Shrub/Scrub
    71: "Pasture/Meadow",         # Grassland/Herbaceous
    81: "Pasture/Meadow",         # Pasture/Hay
    82: "Row Crops",              # Cultivated Crops
    90: "Woods (Light)",          # Woody Wetlands
    95: "Pasture/Meadow",         # Emergent Herbaceous Wetlands
}

# ---------------------------------------------------------------------------
# Return periods
# ---------------------------------------------------------------------------

RETURN_PERIODS = [2, 5, 10, 25, 50, 100]  # years

# ---------------------------------------------------------------------------
# SCS Exhibit 4-II — Unit peak discharge qu (cfs/mi^2/in)
# Type II rainfall distribution (Oklahoma standard)
#
# Tc values (hours): 0.1 to 10.0
# Ia/P ratios: 0.10 to 0.50
#
# Source: SCS National Engineering Handbook, Section 4, Exhibit 4-II
# ---------------------------------------------------------------------------

_IAP = [0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]

# qu values per Tc row: [iap=0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
_QU_ROWS = {
    0.1:  [1070, 770, 660, 543, 447, 361, 280, 207],
    0.2:  [890,  660, 567, 471, 390, 316, 246, 183],
    0.3:  [770,  580, 503, 420, 349, 284, 221, 166],
    0.4:  [686,  520, 452, 378, 314, 256, 200, 151],
    0.5:  [621,  472, 411, 345, 287, 234, 183, 138],
    0.75: [497,  381, 333, 280, 234, 191, 150, 114],
    1.0:  [407,  313, 274, 231, 194, 159, 125,  95],
    1.5:  [285,  220, 194, 164, 138, 114,  90,  68],
    2.0:  [212,  164, 145, 123, 104,  86,  68,  52],
    3.0:  [127,   99,  88,  75,  64,  53,  42,  32],
    4.0:  [83,    65,  58,  50,  42,  35,  28,  21],
    5.0:  [58,    46,  41,  35,  30,  25,  20,  15],
    6.0:  [42,    33,  30,  26,  22,  18,  14,  11],
    8.0:  [24,    19,  17,  15,  13,  10,   8,   6],
    10.0: [15,    12,  11,  10,   8,   7,   5,   4],
}

QU_TABLE = {tc: dict(zip(_IAP, vals)) for tc, vals in _QU_ROWS.items()}
QU_TC_VALUES = sorted(QU_TABLE.keys())
QU_IAP_VALUES = _IAP

# ---------------------------------------------------------------------------
# SCS Type II Dimensionless Mass Curve — TR-55 Table B-2
# (t_hr, cumulative_fraction_of_P24)
# Time in hours (0–24); peak intensity centered at hour 12
# ---------------------------------------------------------------------------

SCS_TYPE_II_MASS_CURVE: list[tuple[float, float]] = [
    (0.0,   0.000),
    (2.0,   0.022),
    (4.0,   0.048),
    (6.0,   0.080),
    (7.0,   0.098),
    (8.0,   0.120),
    (8.5,   0.133),
    (9.0,   0.147),
    (9.5,   0.163),
    (10.0,  0.181),
    (10.5,  0.204),
    (11.0,  0.235),
    (11.5,  0.283),
    (12.0,  0.663),
    (12.5,  0.735),
    (13.0,  0.772),
    (13.5,  0.799),
    (14.0,  0.820),
    (14.5,  0.838),
    (15.0,  0.854),
    (15.5,  0.868),
    (16.0,  0.880),
    (16.5,  0.892),
    (17.0,  0.903),
    (17.5,  0.913),
    (18.0,  0.922),
    (18.5,  0.930),
    (19.0,  0.938),
    (19.5,  0.944),
    (20.0,  0.950),
    (20.5,  0.955),
    (21.0,  0.960),
    (22.0,  0.968),
    (23.0,  0.978),
    (24.0,  1.000),
]

# ---------------------------------------------------------------------------
# SCS Dimensionless Unit Hydrograph — NEH Part 630, Chapter 16, Table 16-1
# Discharge-ratio ordinates stored as (t/Tp, q/qp) pairs; q/qp = 0 outside [0, 5·Tp]
# ---------------------------------------------------------------------------

SCS_DUH: list[tuple[float, float]] = [
    (0.0, 0.000),
    (0.1, 0.030),
    (0.2, 0.100),
    (0.3, 0.190),
    (0.4, 0.310),
    (0.5, 0.470),
    (0.6, 0.660),
    (0.7, 0.820),
    (0.8, 0.930),
    (0.9, 0.990),
    (1.0, 1.000),
    (1.1, 0.990),
    (1.2, 0.930),
    (1.3, 0.860),
    (1.4, 0.780),
    (1.5, 0.680),
    (1.6, 0.560),
    (1.8, 0.390),
    (2.0, 0.280),
    (2.2, 0.207),
    (2.4, 0.147),
    (2.6, 0.107),
    (2.8, 0.077),
    (3.0, 0.055),
    (3.5, 0.025),
    (4.0, 0.011),
    (4.5, 0.005),
    (5.0, 0.000),
]
