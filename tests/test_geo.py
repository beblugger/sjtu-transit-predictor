from __future__ import annotations

import math

import numpy as np

from app.geo import (
    angular_diameter_arcsec,
    ecef_to_enu,
    ecef_to_geodetic,
    enu_to_ecef,
    geodetic_to_ecef,
)


def test_wgs84_ecef_round_trip():
    point = (31.02807, 121.42839, 12.5)
    result = ecef_to_geodetic(geodetic_to_ecef(*point))
    assert math.isclose(result.latitude_deg, point[0], abs_tol=1e-8)
    assert math.isclose(result.longitude_deg, point[1], abs_tol=1e-8)
    assert math.isclose(result.altitude_m, point[2], abs_tol=1e-5)


def test_enu_round_trip():
    lat, lon = 31.02807, 121.42839
    ecef = enu_to_ecef(np.array([100.0, -250.0, 20.0]), lat, lon, 5.0)
    enu = ecef_to_enu(ecef, lat, lon, 5.0)
    np.testing.assert_allclose(enu, [100.0, -250.0, 20.0], atol=1e-7)


def test_angular_diameter_limits():
    assert math.isclose(angular_diameter_arcsec(1.0, 206_264.806), 1.0, rel_tol=1e-8)
    iss = angular_diameter_arcsec(109.0, 400_000.0)
    assert 55.0 < iss < 58.0
