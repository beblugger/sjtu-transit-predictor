from __future__ import annotations

import math

import numpy as np
import pytest

from app.geo import (
    angular_diameter_arcsec,
    ecef_to_enu,
    geodetic_to_ecef,
)


def test_wgs84_ecef_and_local_frame():
    # Equator at longitude zero has a known ECEF position and local axes.
    np.testing.assert_allclose(geodetic_to_ecef(0, 0, 12), [6_378_149, 0, 0], atol=1e-7)
    origin = geodetic_to_ecef(0, 0, 12)
    np.testing.assert_allclose(ecef_to_enu(origin, 0, 0, 12), [0, 0, 0], atol=1e-7)
    np.testing.assert_allclose(ecef_to_enu(origin + [20, 100, -250], 0, 0, 12),
                               [100, -250, 20], atol=1e-7)


def test_angular_diameter_limits():
    assert math.isclose(angular_diameter_arcsec(1.0, 206_264.806), 1.0, rel_tol=1e-8)
    iss = angular_diameter_arcsec(109.0, 400_000.0)
    assert 55.0 < iss < 58.0
    for diameter, distance in ((float('nan'), 400000), (1, float('inf')), (0, 1), (1, 0)):
        with pytest.raises(ValueError):
            angular_diameter_arcsec(diameter, distance)
