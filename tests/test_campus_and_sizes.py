from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone

import math

from app.astronomy import SatellitePosition, TargetPosition
from app.campus import load_campus
from app.config import Settings
from app.geo import Geodetic, unit_from_az_alt
from app.models import Announcement, ObserverPoint, Target, WeatherEvidence
from app.providers.celestrak import TLEEntry
from app.satellite_sizes import SatelliteSizeCatalog
from app.satellites import SatelliteRecord, SatelliteService, _crossing_offset
from app.weather import WeatherEvaluation


def test_campus_polygon_and_grid():
    campus = load_campus(Path("app/data/campus_minhang.geojson"))
    assert campus.contains(31.02807, 121.42839)
    assert not campus.contains(31.00000, 121.00000)
    points = campus.grid(100.0)
    assert campus.center.altitude_m == 10.0
    assert all(point.elevation_m == 10.0 for point in points)
    assert all(point.elevation_m == 10.0 for point in campus.refine(points[len(points)//2]))
    assert len(points) > 200
    assert all(campus.contains(point.latitude, point.longitude) for point in points)


def test_satellite_size_angular_range():
    catalog = SatelliteSizeCatalog(Path("app/data/satellite_sizes.yaml"))
    iss = catalog.get(25544)
    assert iss is not None
    diameter = iss.angular_diameter_arcsec(400_000.0)
    low, high = iss.angular_diameter_range_arcsec(400_000.0)
    assert low < diameter <= high
    assert catalog.get(999999) is None


class FixedAstronomy:
    def __init__(self, center_time, rate=0.2):
        self.center_time = center_time
        self.rate = rate

    def target_at(self, target, observer, value):
        return TargetPosition(0.0, 30.0, 380_000_000.0, 900.0)

    def satellite_position(self, satrec, observer, value):
        offset = (value - self.center_time).total_seconds()
        azimuth = (offset * self.rate) % 360.0
        return SatellitePosition(
            azimuth_deg=azimuth,
            altitude_deg=30.0,
            range_m=400_000.0,
            east_m=200_000.0,
            north_m=200_000.0,
            up_m=230_940.0,
            vector_enu=unit_from_az_alt(azimuth, 30.0),
        )


class AnnouncingWeather:
    def evaluate(self, target, event_time, now=None):
        return WeatherEvaluation(
            evidence=WeatherEvidence(
                decision=Announcement.ANNOUNCED,
                score=0.95,
                p0_clear=0.95,
                p3_clear=0.95,
                p12_clear=0.95,
                threshold=0.7,
            ),
            usable=True,
        )


def test_satellite_refinement_and_angular_diameter_field():
    campus = load_campus(Path("app/data/campus_minhang.geojson"))
    catalog = SatelliteSizeCatalog(Path("app/data/satellite_sizes.yaml"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    astronomy = FixedAstronomy(now)
    service = SatelliteService(
        Settings(),
        campus,
        astronomy,
        catalog,
        AnnouncingWeather(),
        provider=object(),
        fallback_provider=object(),
        store=None,
    )
    entry = TLEEntry("ISS", 25544, "", "", now)
    record = SatelliteRecord(entry, object(), catalog.get(25544))
    conjunction = service._refine_at_observer(
        record,
        Target.MOON,
        ObserverPoint(latitude=31.028, longitude=121.428),
        now,
    )
    assert conjunction is not None
    assert conjunction.minimum_separation_arcsec < 0.01
    assert conjunction.end_time > conjunction.start_time
    event = service._build_event(record, Target.MOON, conjunction, now)
    assert event.object_angular_diameter_arcsec is not None
    assert 55.0 < event.object_angular_diameter_arcsec < 58.0
    assert event.physical_size_m == 109.0
    assert event.size_unknown is False


def test_slow_transit_contacts_extend_past_coarse_search_window():
    campus = load_campus(Path("app/data/campus_minhang.geojson"))
    sizes = SatelliteSizeCatalog(Path("app/data/satellite_sizes.yaml"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    service = SatelliteService(Settings(), campus, FixedAstronomy(now, rate=0.002),
                               sizes, AnnouncingWeather(), provider=object(), fallback_provider=object())
    record = SatelliteRecord(TLEEntry("ISS", 25544, "", "", now), object(), sizes.get(25544))
    point = ObserverPoint(latitude=31.028, longitude=121.428)
    conjunction = service._refine_at_observer(record, Target.MOON, point, now)
    assert conjunction is not None
    assert (conjunction.end_time - conjunction.start_time).total_seconds() > 250
    for contact in (conjunction.start_time, conjunction.end_time):
        position = service.astronomy.satellite_position(record.satrec, Geodetic(31.028, 121.428), contact)
        from app.geo import angular_separation_arcsec
        separation = angular_separation_arcsec(position.azimuth_deg, position.altitude_deg, 0, 30)
        assert abs(separation - conjunction.threshold_arcsec) < 0.01
    # Being inside the disk at a search boundary is not a minimum.
    assert service._refine_at_observer(record, Target.MOON, point, now + timedelta(seconds=100)) is None


def test_contact_between_last_doubling_step_and_limit_is_not_truncated():
    def evaluate(offset):
        return (None, None, None, abs(offset))
    assert math.isclose(_crossing_offset(evaluate, 0, 60, 55, direction=1), 55)
    assert math.isclose(_crossing_offset(evaluate, 0, -60, 55, direction=-1), -55)
