from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

WGS84_A = 6_378_137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
ARCSEC_PER_RADIAN = 206_264.80624709636


@dataclass(frozen=True)
class Geodetic:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float = 0.0


def geodetic_to_ecef(latitude_deg: float, longitude_deg: float, altitude_m: float = 0.0) -> np.ndarray:
    lat = math.radians(latitude_deg)
    lon = math.radians(longitude_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    return np.array(
        [
            (n + altitude_m) * cos_lat * math.cos(lon),
            (n + altitude_m) * cos_lat * math.sin(lon),
            (n * (1.0 - WGS84_E2) + altitude_m) * sin_lat,
        ],
        dtype=float,
    )


def enu_rotation(latitude_deg: float, longitude_deg: float) -> np.ndarray:
    lat = math.radians(latitude_deg)
    lon = math.radians(longitude_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    return np.array(
        [
            [-so, co, 0.0],
            [-sl * co, -sl * so, cl],
            [cl * co, cl * so, sl],
        ]
    )


def ecef_to_enu(
    ecef: np.ndarray,
    observer_latitude: float,
    observer_longitude: float,
    observer_altitude: float = 0.0,
) -> np.ndarray:
    origin = geodetic_to_ecef(observer_latitude, observer_longitude, observer_altitude)
    return enu_rotation(observer_latitude, observer_longitude) @ (ecef - origin)


def enu_az_alt_range(east_north_up: np.ndarray) -> tuple[float, float, float]:
    east, north, up = (float(v) for v in east_north_up)
    horizontal = math.hypot(east, north)
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    altitude = math.degrees(math.atan2(up, horizontal))
    return azimuth, altitude, float(np.linalg.norm(east_north_up))


def unit_from_az_alt(azimuth_deg: float, altitude_deg: float) -> np.ndarray:
    az = math.radians(azimuth_deg)
    alt = math.radians(altitude_deg)
    return np.array(
        [math.cos(alt) * math.sin(az), math.cos(alt) * math.cos(az), math.sin(alt)],
        dtype=float,
    )


def angular_separation_arcsec(a_az_deg: float, a_alt_deg: float, b_az_deg: float, b_alt_deg: float) -> float:
    a = unit_from_az_alt(a_az_deg, a_alt_deg)
    b = unit_from_az_alt(b_az_deg, b_alt_deg)
    dot = float(np.clip(np.dot(a, b), -1.0, 1.0))
    return math.acos(dot) * ARCSEC_PER_RADIAN


def angular_diameter_arcsec(diameter_m: float, range_m: float) -> float:
    if not math.isfinite(diameter_m) or not math.isfinite(range_m) or diameter_m <= 0.0 or range_m <= 0.0:
        raise ValueError("diameter and range must be finite and positive")
    return 2.0 * math.atan(diameter_m / (2.0 * range_m)) * ARCSEC_PER_RADIAN
