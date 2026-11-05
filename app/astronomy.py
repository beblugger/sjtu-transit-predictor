from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable

import numpy as np
from sgp4.api import Satrec, jday
from skyfield.api import load, load_file, wgs84

from app.geo import (
    Geodetic,
    ecef_to_enu,
    enu_az_alt_range,
    unit_from_az_alt,
)


@dataclass(frozen=True)
class TargetPosition:
    azimuth_deg: float
    altitude_deg: float
    distance_m: float
    angular_radius_arcsec: float


@dataclass(frozen=True)
class SatellitePosition:
    azimuth_deg: float
    altitude_deg: float
    range_m: float
    east_m: float
    north_m: float
    up_m: float
    vector_enu: np.ndarray


class CelestialCalculator:
    """Skyfield-based Sun/Moon astrometry and SGP4 satellite propagation."""

    _load_lock = Lock()

    def __init__(self, ephemeris_path: Path):
        self.ephemeris_path = ephemeris_path
        self.ephemeris_path.parent.mkdir(parents=True, exist_ok=True)
        self.ts = load.timescale()
        with self._load_lock:
            if self.ephemeris_path.exists():
                self.eph = load_file(str(self.ephemeris_path))
            else:
                self.eph = None
        self.earth = self.eph["earth"] if self.eph else None
        self.sun = self.eph["sun"] if self.eph else None
        self.moon = self.eph["moon"] if self.eph else None
        self._vector_context = None

    @property
    def ephemeris_backend(self) -> str:
        return "skyfield-de421" if self.eph is not None else "meeus-analytical"

    def times(self, datetimes: Iterable[datetime]):
        values = []
        for value in datetimes:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            values.append(value.astimezone(timezone.utc))
        return self.ts.from_datetimes(values)

    def time(self, value: datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return self.ts.from_datetime(value.astimezone(timezone.utc))

    def target_at(self, target: str, observer: Geodetic, value: datetime) -> TargetPosition:
        t = self.time(value)
        if self.eph is None:
            return _analytical_target_at(target, observer, value)
        observer_topos = wgs84.latlon(
            observer.latitude_deg,
            observer.longitude_deg,
            elevation_m=observer.altitude_m,
        )
        satellite = self.earth + observer_topos
        body = self.sun if target.lower() == "sun" else self.moon
        apparent = satellite.at(t).observe(body).apparent()
        alt, az, distance = apparent.altaz()
        distance_m = float(distance.m)
        if target.lower() == "sun":
            physical_radius_m = 695_700_000.0
        else:
            physical_radius_m = 1_737_400.0
        angular_radius = 2.0 * math.atan(physical_radius_m / (2.0 * distance_m))
        return TargetPosition(
            azimuth_deg=float(az.degrees),
            altitude_deg=float(alt.degrees),
            distance_m=distance_m,
            angular_radius_arcsec=math.degrees(angular_radius) * 3600.0,
        )

    def transit_diagram(self, satrec, target: str, observer: Geodetic, value: datetime) -> dict:
        """Local tangent plane in arcseconds: celestial north up, east left."""
        body = self.target_at(target, observer, value)
        center = unit_from_az_alt(body.azimuth_deg, body.altitude_deg)
        latitude = math.radians(observer.latitude_deg)
        pole = np.array([0., math.cos(latitude), math.sin(latitude)])
        north = pole - float(pole @ center) * center
        north /= np.linalg.norm(north)
        east = np.cross(north, center)

        def project(vector):
            denominator = float(vector @ center)
            return [math.degrees(math.atan2(float(vector @ east), denominator)) * 3600,
                    math.degrees(math.atan2(float(vector @ north), denominator)) * 3600]

        points = []
        for offset in (-1., 0., 1.):
            instant = value + timedelta(seconds=offset)
            sat = self.satellite_position(satrec, observer, instant)
            moving_body = self.target_at(target, observer, instant)
            relative = np.array(project(sat.vector_enu)) - np.array(project(
                unit_from_az_alt(moving_body.azimuth_deg, moving_body.altitude_deg)))
            points.append(relative.tolist())
        result = {"path_arcsec": points, "radius_arcsec": body.angular_radius_arcsec}
        if target == 'moon':
            sun = self.target_at('sun', observer, value)
            sun_vector = unit_from_az_alt(sun.azimuth_deg, sun.altitude_deg) * sun.distance_m
            moon_to_sun = sun_vector - center * body.distance_m
            moon_to_sun /= np.linalg.norm(moon_to_sun)
            result['illuminated_fraction'] = (1 - float(moon_to_sun @ center)) / 2
            result['bright_limb_angle_deg'] = math.degrees(math.atan2(float(moon_to_sun @ east), float(moon_to_sun @ north)))
        return result

    def targets_at(self, target: str, observer: Geodetic, values: Iterable[datetime]):
        datetimes = list(values)
        if self.eph is None:
            return _analytical_targets_at(target, observer, datetimes)
        t = self.times(datetimes)
        observer_topos = wgs84.latlon(
            observer.latitude_deg,
            observer.longitude_deg,
            elevation_m=observer.altitude_m,
        )
        satellite = self.earth + observer_topos
        body = self.sun if target.lower() == "sun" else self.moon
        apparent = satellite.at(t).observe(body).apparent()
        alt, az, distance = apparent.altaz()
        physical_radius_m = 695_700_000.0 if target.lower() == "sun" else 1_737_400.0
        radii = np.degrees(2.0 * np.arctan(physical_radius_m / (2.0 * distance.m))) * 3600.0
        return (
            np.asarray(az.degrees, dtype=float),
            np.asarray(alt.degrees, dtype=float),
            np.asarray(distance.m, dtype=float),
            np.asarray(radii, dtype=float),
        )

    def satellite_position(
        self,
        satellite: Satrec,
        observer: Geodetic,
        value: datetime,
    ) -> SatellitePosition:
        t = self.time(value)
        utc = value.astimezone(timezone.utc)
        jd, fraction = jday(utc.year, utc.month, utc.day, utc.hour, utc.minute,
                            utc.second + utc.microsecond / 1e6)
        error, position, _ = satellite.sgp4(jd, fraction)
        if error:
            raise RuntimeError(f"SGP4 propagation failed with code {error}")
        ecef = teme_to_ecef(np.asarray(position, dtype=float), float(t.gmst) * math.pi / 12.0)
        enu = ecef_to_enu(
            ecef,
            observer.latitude_deg,
            observer.longitude_deg,
            observer.altitude_m,
        )
        az_deg, alt_deg, range_m = enu_az_alt_range(enu)
        return SatellitePosition(
            azimuth_deg=az_deg,
            altitude_deg=alt_deg,
            range_m=range_m,
            east_m=float(enu[0]),
            north_m=float(enu[1]),
            up_m=float(enu[2]),
            vector_enu=enu / np.linalg.norm(enu),
        )

    def satellite_vectors(
        self,
        satellite: Satrec,
        observer: Geodetic,
        values: Iterable[datetime],
    ) -> tuple[np.ndarray, np.ndarray]:
        if self._vector_context is None or self._vector_context[0] is not values:
            datetimes = list(values)
            t = self.times(datetimes)
            dates = [value.astimezone(timezone.utc) for value in datetimes]
            pairs = [jday(v.year, v.month, v.day, v.hour, v.minute, v.second + v.microsecond / 1e6) for v in dates]
            jd, fraction = np.asarray(pairs, dtype=float).T
            jd, fraction = np.ascontiguousarray(jd), np.ascontiguousarray(fraction)
            theta = np.asarray(t.gmst, dtype=float) * math.pi / 12.0
            self._vector_context = (values, jd, fraction, np.cos(theta), np.sin(theta))
        _, jd, fraction, cos_t, sin_t = self._vector_context
        error, position, _ = satellite.sgp4_array(jd, fraction)
        # A decayed sample must not erase otherwise usable times for an object.
        position[error != 0] = np.nan
        ecef = np.empty_like(position)
        ecef[:, 0] = cos_t * position[:, 0] + sin_t * position[:, 1]
        ecef[:, 1] = -sin_t * position[:, 0] + cos_t * position[:, 1]
        ecef[:, 2] = position[:, 2]
        ecef *= 1000.0
        origin = observer_to_ecef(observer)
        relative = ecef - origin
        lat = math.radians(observer.latitude_deg)
        lon = math.radians(observer.longitude_deg)
        sl, cl = math.sin(lat), math.cos(lat)
        so, co = math.sin(lon), math.cos(lon)
        east = -so * relative[:, 0] + co * relative[:, 1]
        north = -sl * co * relative[:, 0] - sl * so * relative[:, 1] + cl * relative[:, 2]
        up = cl * co * relative[:, 0] + cl * so * relative[:, 1] + sl * relative[:, 2]
        enu = np.column_stack([east, north, up])
        ranges = np.linalg.norm(enu, axis=1)
        vectors = enu / ranges[:, None]
        return vectors, ranges


def teme_to_ecef(position_km: np.ndarray, gmst_radians: float) -> np.ndarray:
    cos_t = math.cos(gmst_radians)
    sin_t = math.sin(gmst_radians)
    return np.array(
        [
            cos_t * position_km[0] + sin_t * position_km[1],
            -sin_t * position_km[0] + cos_t * position_km[1],
            position_km[2],
        ],
        dtype=float,
    ) * 1000.0


def observer_to_ecef(observer: Geodetic) -> np.ndarray:
    from app.geo import geodetic_to_ecef

    return geodetic_to_ecef(
        observer.latitude_deg,
        observer.longitude_deg,
        observer.altitude_m,
    )


def unit_vector_rows(azimuth_deg: np.ndarray, altitude_deg: np.ndarray) -> np.ndarray:
    az = np.radians(azimuth_deg)
    alt = np.radians(altitude_deg)
    return np.column_stack(
        [np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)]
    )


def angular_separation_rows(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    dots = np.clip(np.sum(a * b, axis=1), -1.0, 1.0)
    return np.arccos(dots)


def _julian_date(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    year = value.year
    month = value.month
    day = (
        value.day
        + (value.hour + (value.minute + (value.second + value.microsecond / 1e6) / 60.0) / 60.0)
        / 24.0
    )
    if month <= 2:
        year -= 1
        month += 12
    a = math.floor(year / 100)
    b = 2 - a + math.floor(a / 4)
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + day
        + b
        - 1524.5
    )


def _gmst_degrees(jd: float) -> float:
    t = (jd - 2451545.0) / 36525.0
    return (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - t * t * t / 38710000.0
    ) % 360.0


def _sun_geocentric(value: datetime) -> tuple[float, float, float]:
    jd = _julian_date(value)
    n = jd - 2451545.0
    mean_longitude = math.radians((280.460 + 0.9856474 * n) % 360.0)
    mean_anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic_lon = (
        mean_longitude
        + math.radians(1.915) * math.sin(mean_anomaly)
        + math.radians(0.020) * math.sin(2.0 * mean_anomaly)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)
    distance_au = (
        1.00014
        - 0.01671 * math.cos(mean_anomaly)
        - 0.00014 * math.cos(2.0 * mean_anomaly)
    )
    ra = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic_lon),
        math.cos(ecliptic_lon),
    )
    dec = math.asin(math.sin(obliquity) * math.sin(ecliptic_lon))
    return ra, dec, distance_au * 149_597_870_700.0


def _moon_geocentric(value: datetime) -> tuple[float, float, float]:
    jd = _julian_date(value)
    t = (jd - 2451545.0) / 36525.0
    lp = math.radians((218.3164477 + 481267.88123421 * t) % 360.0)
    d = math.radians((297.8501921 + 445267.1114034 * t) % 360.0)
    m = math.radians((357.5291092 + 35999.0502909 * t) % 360.0)
    mp = math.radians((134.9633964 + 477198.8675055 * t) % 360.0)
    f = math.radians((93.2720950 + 483202.0175233 * t) % 360.0)
    longitude = lp + math.radians(
        6.289 * math.sin(mp)
        + 1.274 * math.sin(2 * d - mp)
        + 0.658 * math.sin(2 * d)
        + 0.214 * math.sin(2 * mp)
        - 0.186 * math.sin(m)
        - 0.114 * math.sin(2 * f)
    )
    latitude = math.radians(
        5.128 * math.sin(f)
        + 0.280 * math.sin(mp + f)
        + 0.277 * math.sin(mp - f)
        + 0.173 * math.sin(2 * d - f)
    )
    distance_m = 1000.0 * (
        385000.56
        - 20905.0 * math.cos(mp)
        - 3699.0 * math.cos(2 * d - mp)
        - 2956.0 * math.cos(2 * d)
        - 570.0 * math.cos(2 * mp)
    )
    obliquity = math.radians(23.4392911 - 0.0130042 * t)
    ra = math.atan2(
        math.sin(longitude) * math.cos(obliquity) - math.tan(latitude) * math.sin(obliquity),
        math.cos(longitude),
    )
    dec = math.asin(
        math.sin(latitude) * math.cos(obliquity)
        + math.cos(latitude) * math.sin(obliquity) * math.sin(longitude)
    )
    return ra % (2.0 * math.pi), dec, distance_m


def _analytical_target_at(target: str, observer: Geodetic, value: datetime) -> TargetPosition:
    jd = _julian_date(value)
    theta = math.radians(_gmst_degrees(jd))
    if target.lower() == "sun":
        ra, dec, distance = _sun_geocentric(value)
        radius_m = 695_700_000.0
    else:
        ra, dec, distance = _moon_geocentric(value)
        radius_m = 1_737_400.0
    target_eci = distance * np.array(
        [math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)]
    )
    observer_ecef = observer_to_ecef(observer)
    observer_eci = np.array(
        [
            math.cos(theta) * observer_ecef[0] - math.sin(theta) * observer_ecef[1],
            math.sin(theta) * observer_ecef[0] + math.cos(theta) * observer_ecef[1],
            observer_ecef[2],
        ]
    )
    topocentric_eci = target_eci - observer_eci
    # Rotate inertial vector back into ECEF, then into the observer ENU frame.
    topocentric_ecef = np.array(
        [
            math.cos(theta) * topocentric_eci[0] + math.sin(theta) * topocentric_eci[1],
            -math.sin(theta) * topocentric_eci[0] + math.cos(theta) * topocentric_eci[1],
            topocentric_eci[2],
        ]
    )
    enu = ecef_to_enu(
        topocentric_ecef + observer_ecef,
        observer.latitude_deg,
        observer.longitude_deg,
        observer.altitude_m,
    )
    azimuth, altitude, topocentric_range = enu_az_alt_range(enu)
    angle = 2.0 * math.atan(radius_m / (2.0 * topocentric_range))
    return TargetPosition(
        azimuth_deg=azimuth,
        altitude_deg=altitude,
        distance_m=topocentric_range,
        angular_radius_arcsec=math.degrees(angle) * 3600.0,
    )


def _analytical_targets_at(
    target: str,
    observer: Geodetic,
    values: list[datetime],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rows = [_analytical_target_at(target, observer, value) for value in values]
    return (
        np.asarray([item.azimuth_deg for item in rows]),
        np.asarray([item.altitude_deg for item in rows]),
        np.asarray([item.distance_m for item in rows]),
        np.asarray([item.angular_radius_arcsec for item in rows]),
    )
