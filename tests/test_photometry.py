from datetime import datetime, timezone
import math

import numpy as np
import pytest

from app.astronomy import CelestialCalculator, SatellitePosition, TargetPosition
from app.geo import Geodetic
from app.photometry import estimate_magnitude, load_photometry_catalog


def estimate(name="SAT", distance=1000, phase=90, **params):
    return estimate_magnitude(name, distance, phase, sunlit=True, **params)


def test_stellarium_starlink_reference_and_distance_scaling():
    assert estimate("STARLINK-123").magnitude == pytest.approx(5.93)
    assert estimate("STARLINK-123", distance=100).magnitude == pytest.approx(0.93)
    # This empirical model intentionally does not use standard magnitude or phase.
    assert estimate("STARLINK-123", phase=None, standard_magnitude=-10).model == "stellarium-starlink"
    assert estimate("STARLINK DARKSAT").magnitude == pytest.approx(4.6254)


def test_standard_magnitude_normalization_phase_and_source_precedence():
    half = estimate(standard_magnitude=2)
    assert half.magnitude == pytest.approx(2.002574989159953)
    full = estimate(phase=0, standard_magnitude=2)
    assert full.magnitude == pytest.approx(1.25)
    assert half.magnitude > full.magnitude
    assert estimate(distance=100, standard_magnitude=2).magnitude == pytest.approx(half.magnitude - 5)
    assert estimate(phase=180, standard_magnitude=2).magnitude == pytest.approx(16.25)
    assert estimate(standard_magnitude=2, rcs_m2=100).model == "stellarium-standard"


def test_rcs_approximation_is_finite_near_solar_transits():
    full = estimate(phase=0, rcs_m2=1)
    assert full.magnitude == pytest.approx(6.690527840214589)
    assert estimate(phase=0, rcs_m2=10).magnitude == pytest.approx(full.magnitude - 2.5)
    assert estimate(distance=100, phase=0, rcs_m2=1).magnitude == pytest.approx(full.magnitude - 5)
    for phase in (25, 60, 100, 150, 179.9, 179.999999):
        result = estimate(phase=phase, rcs_m2=1)
        assert result.model == "stellarium-rcs"
        assert math.isfinite(result.magnitude)
    assert estimate(phase=179.999999, rcs_m2=1).magnitude > estimate(phase=179.9, rcs_m2=1).magnitude
    assert estimate(phase=180, rcs_m2=1) is None


def test_missing_parameters_and_shadow_never_get_placeholder_magnitudes():
    assert estimate() is None
    assert estimate(standard_magnitude=99) is None
    assert estimate(phase=None, standard_magnitude=2) is None
    assert estimate(phase=181, rcs_m2=1) is None
    assert estimate(rcs_m2=0) is None
    for sunlight in (False, None):
        assert estimate_magnitude("STARLINK-123", 1000, 90, sunlit=sunlight) is None
    for distance in (0, -1, float("nan"), float("inf")):
        assert estimate("STARLINK-123", distance=distance) is None


def test_catalog_retains_reference_ids_and_zero_magnitude_is_a_valid_input():
    catalog = load_photometry_catalog()
    assert catalog["25544"]["standard_magnitude"] == -2.5
    assert catalog["20580"]["standard_magnitude"] == 1.5
    assert catalog["48229"]["standard_magnitude"] == 7.51
    assert estimate(phase=0, standard_magnitude=0).magnitude == pytest.approx(-0.75)
    assert all(set(row) <= {"standard_magnitude", "rcs_m2"} for row in catalog.values())


def test_phase_angle_uses_the_satellite_as_vertex():
    class Geometry(CelestialCalculator):
        def __init__(self):
            pass

        def target_at(self, target, observer, value):
            return TargetPosition(0, 0, 100_000_000, 960)

    calculator = Geometry()
    def phase(vector):
        position = SatellitePosition(0, 0, 1000, 0, 0, 0, np.asarray(vector, dtype=float))
        return calculator.satellite_phase_angle(Geodetic(0, 0), datetime.now(timezone.utc), position)
    assert phase([0, 1, 0]) == pytest.approx(180)  # Satellite between Sun and observer.
    assert phase([0, -1, 0]) == pytest.approx(0)  # Sun behind observer.
    assert phase([1, 0, 0]) == pytest.approx(90, abs=.001)
