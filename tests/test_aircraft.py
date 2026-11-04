from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.aircraft import AircraftPredictor
from app.campus import load_campus
from app.config import Settings
from app.geo import ecef_to_geodetic, enu_to_ecef
from app.providers.variflight import AircraftObservation


def test_aircraft_predictor_extrapolates_smooth_track():
    settings = Settings()
    campus = load_campus(Path("app/data/campus_minhang.geojson"))
    predictor = AircraftPredictor(settings, campus)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    observations = []
    for index in range(25):
        seconds = index - 24
        enu = np.array([250.0 * seconds, 35.0 * seconds, 8.0 * seconds])
        ecef = enu_to_ecef(
            enu,
            campus.center.latitude_deg,
            campus.center.longitude_deg,
            4.0,
        )
        geodetic = ecef_to_geodetic(ecef)
        observations.append(
            AircraftObservation(
                identifier="TEST1",
                observed_at=now + timedelta(seconds=seconds),
                position=geodetic,
                callsign="TEST1",
                aircraft_type="A320",
                ground_speed_mps=252.4,
                track_deg=82.0,
                vertical_rate_mps=8.0,
                source="test",
            )
        )
    trajectory = predictor.predict(observations, now=now)
    assert trajectory is not None
    prediction = trajectory.position_at(60.0)
    assert 14_000 < prediction.east_m < 16_000
    assert 1_500 < prediction.north_m < 2_500
    assert prediction.sigma_m > 0


def test_track_angle_wrap_is_stable():
    assert math.isclose(math.radians(1.0), math.radians(1.0))
