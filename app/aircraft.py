from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

import numpy as np
from scipy.optimize import minimize_scalar

from app.astronomy import CelestialCalculator
from app.campus import Campus
from app.config import Settings
from app.geo import (
    Geodetic,
    angular_separation_arcsec,
    ecef_to_enu,
    geodetic_to_ecef,
)
from app.models import (
    EventType,
    Target,
    TransitEvent,
)
from app.providers.variflight import AircraftDataProvider, AircraftObservation
from app.store import DatabaseStore
from app.weather import WeatherService


@dataclass(frozen=True)
class AircraftPrediction:
    seconds: float
    east_m: float
    north_m: float
    up_m: float
    sigma_m: float


@dataclass
class AircraftTrajectory:
    observation: AircraftObservation
    positions: list[AircraftPrediction]
    model_used: str

    def position_at(self, seconds: float) -> AircraftPrediction:
        if not self.positions:
            raise ValueError("empty aircraft trajectory")
        if seconds <= self.positions[0].seconds:
            point = self.positions[0]
            return AircraftPrediction(seconds, point.east_m, point.north_m, point.up_m, point.sigma_m)
        if seconds >= self.positions[-1].seconds:
            point = self.positions[-1]
            return AircraftPrediction(seconds, point.east_m, point.north_m, point.up_m, point.sigma_m)
        lo = int(math.floor(seconds))
        hi = min(len(self.positions) - 1, lo + 1)
        a = self.positions[lo]
        b = self.positions[hi]
        fraction = (seconds - a.seconds) / max(1e-9, b.seconds - a.seconds)
        return AircraftPrediction(
            seconds,
            _lerp(a.east_m, b.east_m, fraction),
            _lerp(a.north_m, b.north_m, fraction),
            _lerp(a.up_m, b.up_m, fraction),
            _lerp(a.sigma_m, b.sigma_m, fraction),
        )


class AircraftPredictor:
    def __init__(self, settings: Settings, campus: Campus):
        self.settings = settings
        self.campus = campus

    def predict(
        self,
        observations: list[AircraftObservation],
        *,
        now: datetime,
    ) -> AircraftTrajectory | None:
        valid = _valid_observations(observations, now, self.settings.aircraft_track_retention_seconds)
        if len(valid) < self.settings.aircraft_min_samples:
            return None
        samples = _to_enu(valid, self.campus.center)
        times = np.asarray([item[0] for item in samples], dtype=float)
        positions = np.asarray([item[1] for item in samples], dtype=float)
        current_offset = (now - valid[-1].observed_at.astimezone(timezone.utc)).total_seconds()
        horizon = self.settings.aircraft_horizon_seconds
        horizon_offsets = np.arange(0, horizon + 1, dtype=float) + current_offset
        candidates: list[tuple[str, np.ndarray, float]] = []

        cubic, cubic_error = _fit_cubic(times, positions)
        if cubic is not None:
            candidates.append(("robust_cubic", _evaluate_cubic(cubic, times, horizon_offsets), cubic_error))

        cv, cv_error = _fit_constant_velocity(times, positions)
        if cv is not None:
            candidates.append(("constant_velocity", _evaluate_cv(cv, times, horizon_offsets), cv_error))

        turn, turn_error = _fit_turn(times, positions, valid)
        if turn is not None:
            candidates.append(("coordinated_turn", _evaluate_turn(turn, times, horizon_offsets), turn_error))

        if not candidates:
            return None
        name, output, rmse = min(candidates, key=lambda item: item[2])
        if name == "robust_cubic":
            # Cubic extrapolation is intentionally damped after three minutes.
            cv_output = next((values for n, values, _ in candidates if n == "constant_velocity"), output)
            blend = np.clip((horizon_offsets - 180.0) / 180.0, 0.0, 1.0)
            output = output * (1.0 - blend[:, None]) + cv_output * blend[:, None]
            name = "robust_cubic_damped_to_cv"
        sigma_base = max(20.0, rmse)
        sigma = sigma_base + 2.5 * np.maximum(horizon_offsets, 0.0) ** 1.25
        predictions = [
            AircraftPrediction(
                seconds=float(offset),
                east_m=float(position[0]),
                north_m=float(position[1]),
                up_m=float(position[2]),
                sigma_m=float(sigma_value),
            )
            for offset, position, sigma_value in zip(horizon_offsets, output, sigma)
        ]
        return AircraftTrajectory(valid[-1], predictions, name)


class AircraftService:
    def __init__(
        self,
        settings: Settings,
        campus: Campus,
        astronomy: CelestialCalculator,
        weather: WeatherService,
        provider: AircraftDataProvider,
        store: DatabaseStore | None = None,
    ):
        self.settings = settings
        self.campus = campus
        self.astronomy = astronomy
        self.weather = weather
        self.provider = provider
        self.store = store
        self.predictor = AircraftPredictor(settings, campus)
        self.tracks: dict[str, list[AircraftObservation]] = {}
        self.last_error: str | None = None
        self.updated_at: datetime | None = None
        self._lock = Lock()
        self._observer_offsets = _observer_offsets(campus, settings.campus_grid_meters)
        if self.store is not None:
            now = datetime.now(timezone.utc)
            since = now - timedelta(seconds=settings.aircraft_track_retention_seconds)
            self.tracks = self.store.load_observations(since)

    async def poll_once(self) -> int:
        if not self.provider.enabled:
            return 0
        try:
            observations = await self.provider.fetch_around(
                self.campus.center.latitude_deg,
                self.campus.center.longitude_deg,
                self.settings.aircraft_query_radius_km,
            )
            now = datetime.now(timezone.utc)
            with self._lock:
                for observation in observations:
                    self.tracks.setdefault(observation.identifier, []).append(observation)
                self._prune(now)
                self.updated_at = now
                self.last_error = None
            if self.store is not None:
                self.store.save_observations(observations)
                self.store.prune_observations(
                    now - timedelta(seconds=self.settings.aircraft_track_retention_seconds * 2)
                )
            return len(observations)
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
            return 0

    async def events(self, now: datetime | None = None) -> list[TransitEvent]:
        if not self.provider.enabled:
            return []
        now = _utc(now or datetime.now(timezone.utc))
        horizon_seconds = self.settings.aircraft_horizon_seconds
        times = [now + timedelta(seconds=value) for value in range(0, horizon_seconds + 1, 1)]
        center = Geodetic(self.campus.center.latitude_deg, self.campus.center.longitude_deg, 0.0)
        events: list[TransitEvent] = []
        with self._lock:
            track_values = {key: list(value) for key, value in self.tracks.items()}
        target_cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        for target in (Target.SUN, Target.MOON):
            target_cache[target.value] = self.astronomy.targets_at(target.value, center, times)
        for identifier, observations in track_values.items():
            trajectory = self.predictor.predict(observations, now=now)
            if trajectory is None:
                continue
            for target in (Target.SUN, Target.MOON):
                event = self._detect_transit(
                    trajectory,
                    target,
                    times,
                    target_cache[target.value],
                    now,
                )
                if event is not None:
                    events.append(event)
        return sorted(events, key=lambda item: item.minimum_time)

    def _detect_transit(
        self,
        trajectory: AircraftTrajectory,
        target: Target,
        times: list[datetime],
        target_arrays: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        now: datetime,
    ) -> TransitEvent | None:
        target_az, target_alt, _, target_radius_arcsec = target_arrays
        predicted = np.asarray(
            [[p.east_m, p.north_m, p.up_m] for p in trajectory.positions],
            dtype=float,
        )
        obs_e = np.asarray([item[0] for item in self._observer_offsets], dtype=float)
        obs_n = np.asarray([item[1] for item in self._observer_offsets], dtype=float)
        obs_u = np.asarray([item[2] for item in self._observer_offsets], dtype=float)
        points = [item[3] for item in self._observer_offsets]

        east = predicted[:, 0][None, :] - obs_e[:, None]
        north = predicted[:, 1][None, :] - obs_n[:, None]
        up = predicted[:, 2][None, :] - obs_u[:, None]
        horizontal = np.hypot(east, north)
        altitude = np.degrees(np.arctan2(up, horizontal))
        vectors = np.stack([east, north, up], axis=2)
        ranges = np.linalg.norm(vectors, axis=2)
        vectors = vectors / np.maximum(ranges[:, :, None], 1.0)
        target_vectors = np.column_stack(
            [np.cos(np.radians(target_alt)) * np.sin(np.radians(target_az)),
             np.cos(np.radians(target_alt)) * np.cos(np.radians(target_az)),
             np.sin(np.radians(target_alt))]
        )
        dots = np.einsum("otk,tk->ot", vectors, target_vectors)
        separations = np.degrees(np.arccos(np.clip(dots, -1.0, 1.0))) * 3600.0
        visible = (altitude[:, 0] > 0) if altitude.ndim > 1 else altitude > 0
        # altitude has shape observers x time
        visible = altitude > 0.0
        valid_target = target_alt > 0.0
        separations = np.where(visible[:, valid_target], separations[:, valid_target], np.inf)
        if not np.isfinite(separations).any():
            return None
        best_flat = int(np.nanargmin(separations))
        observer_index, time_index = np.unravel_index(best_flat, separations.shape)
        valid_time_indices = np.flatnonzero(valid_target)
        coarse_time_index = int(valid_time_indices[time_index])
        observer, offset_e, offset_n, offset_u = self._observer_offsets[observer_index]
        target_radius = float(target_radius_arcsec[coarse_time_index])
        if separations[observer_index, time_index] > target_radius + 5.0:
            return None

        def separation_at(seconds: float) -> float:
            point = trajectory.position_at(max(0.0, seconds))
            sample_time = now + timedelta(seconds=seconds)
            target_position = self.astronomy.target_at(target.value, Geodetic(
                observer.latitude, observer.longitude, observer.elevation_m
            ), sample_time)
            relative = np.array(
                [point.east_m - offset_e, point.north_m - offset_n, point.up_m - offset_u]
            )
            azimuth, altitude, _ = _enu_az_alt(relative)
            return angular_separation_arcsec(
                azimuth, altitude, target_position.azimuth_deg, target_position.altitude_deg
            )

        coarse_seconds = float(valid_time_indices[time_index])
        result = minimize_scalar(
            separation_at,
            bounds=(max(0.0, coarse_seconds - 5.0), min(horizon_seconds, coarse_seconds + 5.0)),
            method="bounded",
            options={"xatol": 0.01},
        )
        minimum_seconds = float(result.x)
        minimum_sep = separation_at(minimum_seconds)
        target_position = self.astronomy.target_at(
            target.value,
            Geodetic(observer.latitude, observer.longitude, observer.elevation_m),
            now + timedelta(seconds=minimum_seconds),
        )
        threshold = target_position.angular_radius_arcsec
        if minimum_sep > threshold:
            return None
        start_seconds = _find_crossing(
            separation_at, minimum_seconds, threshold, direction=-1, lower=0.0
        )
        end_seconds = _find_crossing(
            separation_at,
            minimum_seconds,
            threshold,
            direction=1,
            upper=float(self.settings.aircraft_horizon_seconds),
        )
        weather = self.weather.evaluate(
            target.value,
            now + timedelta(seconds=minimum_seconds),
            now,
        )
        point = trajectory.position_at(minimum_seconds)
        relative = np.array([point.east_m - offset_e, point.north_m - offset_n, point.up_m - offset_u])
        azimuth, altitude, range_m = _enu_az_alt(relative)
        reasons = list(weather.evidence.reasons)
        if minimum_sep > threshold:
            return None
        return TransitEvent(
            event_type=EventType.AIRCRAFT,
            target=target,
            name=trajectory.observation.callsign or trajectory.observation.identifier,
            identifier=trajectory.observation.identifier,
            start_time=now + timedelta(seconds=start_seconds),
            minimum_time=now + timedelta(seconds=minimum_seconds),
            end_time=now + timedelta(seconds=end_seconds),
            duration_ms=max(0, int((end_seconds - start_seconds) * 1000)),
            min_separation_arcsec=minimum_sep,
            target_angular_radius_arcsec=target_position.angular_radius_arcsec,
            object_angular_diameter_arcsec=None,
            physical_size_m=None,
            size_unknown=True,
            range_km=range_m / 1000.0,
            observer=observer,
            azimuth_deg=azimuth,
            altitude_deg=altitude,
            target_azimuth_deg=target_position.azimuth_deg,
            target_altitude_deg=target_position.altitude_deg,
            weather=weather.evidence,
            confidence=max(0.0, min(1.0, weather.evidence.score * math.exp(-minimum_seconds / 900.0))),
            reason_codes=reasons + ["aircraft_size_unknown_centerline_only"],
            callsign=trajectory.observation.callsign,
            icao24=trajectory.observation.icao24,
            aircraft_type=trajectory.observation.aircraft_type,
            model_used=trajectory.model_used,
            position_sigma_m=point.sigma_m,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.settings.aircraft_track_retention_seconds)
        for key in list(self.tracks):
            self.tracks[key] = [
                observation
                for observation in self.tracks[key]
                if observation.observed_at >= cutoff
            ]
            if len(self.tracks[key]) < 2:
                self.tracks.pop(key, None)


def _valid_observations(
    observations: list[AircraftObservation],
    now: datetime,
    retention_seconds: int,
) -> list[AircraftObservation]:
    cutoff = now - timedelta(seconds=retention_seconds)
    values = [
        observation
        for observation in observations
        if observation.observed_at >= cutoff
        and observation.observed_at <= now + timedelta(seconds=10)
    ]
    unique = {item.observed_at: item for item in values}
    return [unique[key] for key in sorted(unique)]


def _to_enu(
    observations: list[AircraftObservation],
    center: Geodetic,
) -> list[tuple[float, np.ndarray]]:
    origin_time = observations[0].observed_at.astimezone(timezone.utc)
    origin_ecef = geodetic_to_ecef(center.latitude_deg, center.longitude_deg, center.altitude_m)
    values: list[tuple[float, np.ndarray]] = []
    for observation in observations:
        position = observation.position
        ecef = geodetic_to_ecef(
            position.latitude_deg,
            position.longitude_deg,
            position.altitude_m,
        )
        enu = ecef_to_enu(
            ecef,
            center.latitude_deg,
            center.longitude_deg,
            center.altitude_m,
        )
        seconds = (observation.observed_at.astimezone(timezone.utc) - origin_time).total_seconds()
        values.append((seconds, enu))
    return values


def _fit_cubic(times: np.ndarray, positions: np.ndarray):
    if len(times) < 4:
        return None, float("inf")
    scale = max(1.0, float(np.max(times) - np.min(times)))
    x = times / scale
    design = np.column_stack([np.ones_like(x), x, x**2, x**3])
    weights = np.exp((x - x[-1]) / 0.5)
    coefficients = np.linalg.lstsq(design * weights[:, None], positions * weights[:, None], rcond=None)[0]
    for _ in range(3):
        residual = positions - design @ coefficients
        norm = np.linalg.norm(residual, axis=1)
        robust = 1.0 / np.maximum(1.0, norm / max(10.0, float(np.median(norm) * 2.0)))
        combined = weights * robust
        coefficients = np.linalg.lstsq(
            design * combined[:, None], positions * combined[:, None], rcond=None
        )[0]
    residual = positions - design @ coefficients
    return (coefficients, scale), float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))


def _evaluate_cubic(model, times: np.ndarray, seconds: np.ndarray) -> np.ndarray:
    coefficients, scale = model
    x = (float(times[-1]) + seconds) / scale
    return coefficients[0] + np.outer(x, coefficients[1]) + np.outer(x**2, coefficients[2]) + np.outer(x**3, coefficients[3])


def _fit_constant_velocity(times: np.ndarray, positions: np.ndarray):
    if len(times) < 2:
        return None, float("inf")
    x = np.column_stack([np.ones_like(times), times - times[-1]])
    coefficients = np.linalg.lstsq(x, positions, rcond=None)[0]
    residual = positions - x @ coefficients
    return coefficients, float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))


def _evaluate_cv(model, times: np.ndarray, seconds: np.ndarray) -> np.ndarray:
    coefficients = model
    return coefficients[0] + np.outer(seconds, coefficients[1])


def _fit_turn(times: np.ndarray, positions: np.ndarray, observations: list[AircraftObservation]):
    if len(times) < 3:
        return None, float("inf")
    window = max(3, min(len(times), max(3, int(30 / max(np.median(np.diff(times)), 1e-3)))))
    recent = positions[-window:]
    recent_times = times[-window:]
    cv, cv_error = _fit_constant_velocity(recent_times, recent)
    if cv is None:
        return None, float("inf")
    velocity = cv[1]
    speed = float(np.linalg.norm(velocity[:2]))
    if speed < 10.0:
        return None, float("inf")
    heading = math.atan2(velocity[0], velocity[1])
    headings = []
    for first, last in zip(recent[:-1], recent[1:]):
        delta = last[:2] - first[:2]
        if np.linalg.norm(delta) > 1.0:
            headings.append(math.atan2(delta[0], delta[1]))
    omega = 0.0
    if len(headings) >= 2 and (recent_times[-1] - recent_times[0]) > 0:
        omega = _wrap_turn_rate((headings[-1] - headings[0]) / (recent_times[-1] - recent_times[0]))
    omega = float(np.clip(omega, math.radians(-3.0), math.radians(3.0)))
    if observations:
        vertical = observations[-1].vertical_rate_mps
    else:
        vertical = None
    vertical = vertical if vertical is not None else float(velocity[2])
    model = (cv[0], speed, heading, omega, vertical)
    predicted = _evaluate_turn(model, recent_times, recent_times - times[-1])
    residual = recent - predicted
    turn_error = float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))
    return model, turn_error


def _evaluate_turn(model, times: np.ndarray, seconds: np.ndarray) -> np.ndarray:
    base, speed, heading, omega, vertical = model
    values = []
    for offset in seconds:
        if abs(omega) < 1e-8:
            east = base[0] + speed * math.sin(heading) * offset
            north = base[1] + speed * math.cos(heading) * offset
        else:
            east = base[0] + speed / omega * (math.cos(heading) - math.cos(heading + omega * offset))
            north = base[1] + speed / omega * (math.sin(heading + omega * offset) - math.sin(heading))
        up = base[2] + vertical * offset
        values.append([east, north, up])
    return np.asarray(values)


def _observer_offsets(campus: Campus, spacing_m: float):
    center = campus.center
    values = []
    for point in campus.grid(spacing_m):
        ecef = geodetic_to_ecef(point.latitude, point.longitude, point.elevation_m)
        enu = ecef_to_enu(ecef, center.latitude_deg, center.longitude_deg, center.altitude_m)
        values.append((point, float(enu[0]), float(enu[1]), float(enu[2])))
    return values


def _enu_az_alt(vector: np.ndarray) -> tuple[float, float, float]:
    east, north, up = (float(value) for value in vector)
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    horizontal = math.hypot(east, north)
    altitude = math.degrees(math.atan2(up, horizontal))
    return azimuth, altitude, float(np.linalg.norm(vector))


def _find_crossing(
    function,
    minimum: float,
    threshold: float,
    direction: int,
    *,
    lower: float | None = None,
    upper: float | None = None,
) -> float:
    step = 0.01
    previous = minimum
    previous_value = function(previous) - threshold
    boundary = upper if direction > 0 else lower
    while step <= 20.0:
        current = minimum + direction * step
        if boundary is not None and (
            (direction > 0 and current >= boundary)
            or (direction < 0 and current <= boundary)
        ):
            return boundary
        current_value = function(current) - threshold
        if current_value >= 0 and previous_value < 0:
            from scipy.optimize import brentq

            return brentq(
                lambda value: function(value) - threshold,
                min(previous, current),
                max(previous, current),
                xtol=1e-4,
            )
        previous = current
        previous_value = current_value
        step *= 2.0
    result = minimum + direction * 20.0
    if lower is not None:
        result = max(lower, result)
    if upper is not None:
        result = min(upper, result)
    return result


def _wrap_turn_rate(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _lerp(a: float, b: float, fraction: float) -> float:
    return a + (b - a) * fraction


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
