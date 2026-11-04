from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

import numpy as np
from scipy.optimize import brentq, minimize_scalar
from scipy.interpolate import CubicSpline
from sgp4.api import Satrec

from app.astronomy import (
    CelestialCalculator,
    SatellitePosition,
    TargetPosition,
    angular_separation_rows,
    unit_vector_rows,
)
from app.campus import Campus
from app.config import Settings
from app.geo import Geodetic, angular_separation_arcsec, geodetic_to_ecef, enu_rotation
from app.models import (
    EventType,
    ObserverPoint,
    Target,
    TransitEvent,
)
from app.providers.celestrak import (
    CatalogUnchanged,
    CelestrakProvider,
    SatNogsProvider,
    TLEEntry,
    tle_age_seconds,
)
from app.satellite_sizes import SatelliteSize, SatelliteSizeCatalog
from app.store import DatabaseStore
from app.weather import WeatherService

ARCSEC_PER_DEGREE = 3600.0


@dataclass
class SatelliteRecord:
    entry: TLEEntry
    satrec: Satrec
    size: SatelliteSize | None


@dataclass(frozen=True)
class CandidateWindow:
    target: Target
    center_time: datetime


@dataclass(frozen=True)
class RefinedConjunction:
    observer: ObserverPoint
    minimum_time: datetime
    minimum_separation_arcsec: float
    satellite: SatellitePosition
    target_position: TargetPosition
    threshold_arcsec: float
    start_time: datetime
    end_time: datetime


class SatelliteService:
    def __init__(
        self,
        settings: Settings,
        campus: Campus,
        astronomy: CelestialCalculator,
        sizes: SatelliteSizeCatalog,
        weather: WeatherService,
        provider: CelestrakProvider | None = None,
        fallback_provider: SatNogsProvider | None = None,
        store: DatabaseStore | None = None,
    ):
        self.settings = settings
        self.campus = campus
        self.astronomy = astronomy
        self.sizes = sizes
        self.weather = weather
        self.provider = provider or CelestrakProvider(
            settings.celestrak_active_url,
            timeout_seconds=settings.celestrak_timeout_seconds,
        )
        self.fallback_provider = fallback_provider or CelestrakProvider(settings.community_orbit_url)
        self.store = store
        self.records: list[SatelliteRecord] = []
        self.updated_at: datetime | None = None
        self.checked_at: datetime | None = None
        self.catalog_source = "none"
        self.last_error: str | None = None
        self._event_cache: list[TransitEvent] = []
        self._event_cache_key: tuple[datetime, datetime] | None = None
        self._lock = Lock()
        self._compute_lock = asyncio.Lock()
        self._grid = self.campus.grid(settings.campus_grid_meters)
        self._grid_ecef = np.asarray([
            geodetic_to_ecef(point.latitude, point.longitude, point.elevation_m) for point in self._grid
        ])
        if self.store is not None:
            self._restore_cached_catalog()

    async def refresh_catalog(self) -> None:
        now = datetime.now(timezone.utc)
        if self.store is not None:
            previous = self.store.get_cache("orbit_primary_attempt")
            if previous is not None and (now - previous[1]).total_seconds() < self.settings.tle_refresh_seconds:
                return
            # Persist attempts too: repeated failed builds must not hammer the primary.
            self.store.set_cache("orbit_primary_attempt", True, now)
        self.checked_at = now
        try:
            primary_error = None
            try:
                entries = await self.provider.fetch_active(
                    max_targets=self.settings.satellite_max_targets
                )
                source = "celestrak-active"
            except CatalogUnchanged as exc:
                with self._lock:
                    if self.records:
                        self.last_error = f"primary catalog unchanged: {exc}"
                        return
                entries = await self.fallback_provider.fetch_active(
                    max_targets=self.settings.satellite_max_targets
                )
                source = "community-mirror"
                primary_error = str(exc)
            except Exception as exc:
                primary_error = str(exc)
                entries = await self.fallback_provider.fetch_active(
                    max_targets=self.settings.satellite_max_targets
                )
                source = "community-mirror"
            if source == "community-mirror" and self.records:
                # A lagging mirror must not roll newer cached orbits back.
                merged = {record.entry.norad_id: record.entry for record in self.records}
                changed = False
                for entry in entries:
                    existing = merged.get(entry.norad_id)
                    if existing is None or entry.epoch > existing.epoch:
                        merged[entry.norad_id] = entry
                        changed = True
                if not changed:
                    self.last_error = f"primary unavailable; mirror has no newer elements; retained cached catalog: {primary_error}"
                    return
                entries = list(merged.values())
                source = "community-mirror+cache"
            records: list[SatelliteRecord] = []
            for entry in entries:
                try:
                    satrec = entry.to_satrec()
                    records.append(SatelliteRecord(entry, satrec, self.sizes.get(entry.norad_id)))
                except ValueError:
                    continue
            if not records:
                raise RuntimeError("Celestrak returned no usable TLE records")
            with self._lock:
                self.records = records
                self.updated_at = datetime.now(timezone.utc)
                self.catalog_source = source
                self.last_error = f"primary unavailable; using mirror: {primary_error}" if primary_error else None
                self._event_cache_key = None
            if self.store is not None:
                self.store.save_tles(entries, fetched_at=self.updated_at)
                self.store.set_cache("orbit_source", source, self.updated_at)
        except Exception as exc:
            self.last_error = str(exc)

    def invalidate_cache(self) -> None:
        with self._lock:
            self._event_cache_key = None
            self._event_cache = []

    def reapply_weather(self, now: datetime | None = None) -> None:
        now = _utc(now or datetime.now(timezone.utc))
        with self._lock:
            for index, event in enumerate(self._event_cache):
                evaluation = self.weather.evaluate(event.target.value, event.minimum_time, now)
                updated = event.model_copy(deep=True)
                updated.weather = evaluation.evidence
                updated.confidence = max(
                    0.0,
                    min(
                        1.0,
                        evaluation.evidence.score
                        * (1.05 if updated.physical_size_m is not None else 1.0),
                    ),
                )
                self._event_cache[index] = updated

    def _restore_cached_catalog(self) -> None:
        assert self.store is not None
        entries = self.store.load_tles(0)
        records: list[SatelliteRecord] = []
        for entry in entries:
            try:
                records.append(
                    SatelliteRecord(
                        entry=entry,
                        satrec=entry.to_satrec(),
                        size=self.sizes.get(entry.norad_id),
                    )
                )
            except ValueError:
                continue
        if records:
            self.records = records
            self.catalog_source = "sqlite-cache"
            source = self.store.get_cache("orbit_source")
            if source is not None:
                self.catalog_source = str(source[0])
            attempt = self.store.get_cache("orbit_primary_attempt")
            self.checked_at = attempt[1] if attempt else None
            cached = self.store.get_cache("tle_fetched_at")
            if cached:
                raw, _ = cached
                if isinstance(raw, str):
                    self.updated_at = datetime.fromisoformat(raw)

    def catalog_is_stale(self, now: datetime | None = None) -> bool:
        if self.updated_at is None:
            return True
        now = _utc(now or datetime.now(timezone.utc))
        return (now - self.updated_at).total_seconds() > self.settings.tle_stale_seconds

    async def events(
        self,
        start: datetime,
        end: datetime,
        *,
        now: datetime | None = None,
    ) -> list[TransitEvent]:
        start = _utc(start)
        end = _utc(end)
        now = _utc(now or datetime.now(timezone.utc))
        if end <= start:
            return []
        with self._lock:
            if self._event_cache_key == (start, end):
                return [event.model_copy(deep=True) for event in self._event_cache]
            records = list(self.records)
        if not records:
            return []
        async with self._compute_lock:
            with self._lock:
                if self._event_cache_key == (start, end):
                    return [event.model_copy(deep=True) for event in self._event_cache]
            return await asyncio.to_thread(self._compute_events, start, end, now, records)

    def _compute_events(
        self,
        start: datetime,
        end: datetime,
        now: datetime,
        records: list[SatelliteRecord],
    ) -> list[TransitEvent]:
        coarse_times = _time_grid(start, end, self.settings.satellite_coarse_step_seconds)
        center = Geodetic(
            self.campus.center.latitude_deg,
            self.campus.center.longitude_deg,
            0.0,
        )
        result: list[TransitEvent] = []
        for target in (Target.SUN, Target.MOON):
            target_az, target_alt, _, target_radius = self.astronomy.targets_at(
                target.value, center, coarse_times
            )
            target_vectors = unit_vector_rows(target_az, target_alt)
            for record in records:
                try:
                    windows = self._candidate_windows(
                        record,
                        target,
                        center,
                        coarse_times,
                        target_vectors,
                        target_alt,
                        target_radius,
                    )
                except RuntimeError:
                    continue
                for window in windows:
                    event = self._select_and_refine(record, target, window, now)
                    if event is not None:
                        result.append(event)
        result.sort(key=lambda item: item.minimum_time)
        result = [event for event in result if start <= event.minimum_time < end]
        result = _deduplicate_events(result)
        with self._lock:
            self._event_cache = [event.model_copy(deep=True) for event in result]
            self._event_cache_key = (start, end)
        return result

    def _candidate_windows(
        self,
        record: SatelliteRecord,
        target: Target,
        observer: Geodetic,
        times: list[datetime],
        target_vectors: np.ndarray,
        target_alt_deg: np.ndarray,
        target_radius_arcsec: np.ndarray,
    ) -> list[CandidateWindow]:
        satellite_vectors, ranges = self.astronomy.satellite_vectors(
            record.satrec, observer, times
        )
        sat_alt_deg = np.degrees(np.arcsin(np.clip(satellite_vectors[:, 2], -1.0, 1.0)))
        finite_ranges = ranges[np.isfinite(ranges) & (ranges > 0)]
        if finite_ranges.size == 0:
            return []
        sat_radius = (
            record.size.angular_diameter_arcsec(float(np.min(finite_ranges))) / 2.0
            if record.size
            else 0.0
        )
        max_radius_deg = (
            float(np.nanmax(target_radius_arcsec)) + sat_radius
        ) / ARCSEC_PER_DEGREE
        campus_radius_m = _campus_radius_m(self.campus)
        median_range_m = float(np.min(finite_ranges))
        parallax_deg = (
            math.degrees(math.atan2(campus_radius_m, max(median_range_m, 1.0)))
            if median_range_m > 0
            else 0.0
        )
        catch_radius_deg = min(3.0, max_radius_deg + parallax_deg + 0.08)

        separations = angular_separation_rows(satellite_vectors, target_vectors)
        seg_mid_target = target_vectors[:-1] + target_vectors[1:]
        norms = np.linalg.norm(seg_mid_target, axis=1)
        seg_mid_target = seg_mid_target / np.maximum(norms[:, None], 1e-15)
        u0 = satellite_vectors[:-1]
        u1 = satellite_vectors[1:]
        normals = np.cross(u0, u1)
        normal_lengths = np.linalg.norm(normals, axis=1)
        cross_track = np.full(len(normals), np.pi)
        valid = normal_lengths > 1e-10
        normal_unit = normals[valid] / normal_lengths[valid, None]
        cross_track[valid] = np.arcsin(
            np.clip(np.abs(np.sum(seg_mid_target[valid] * normal_unit, axis=1)), 0.0, 1.0)
        )
        # Distance to the finite minor arc, not the whole great circle.
        # Otherwise the opposite side of every orbit creates false candidates.
        projected = seg_mid_target - np.sum(seg_mid_target * normals, axis=1)[:, None] * normals / np.maximum(normal_lengths[:, None] ** 2, 1e-30)
        projected /= np.maximum(np.linalg.norm(projected, axis=1)[:, None], 1e-15)
        on_arc = (
            (np.sum(np.cross(u0, projected) * normals, axis=1) >= 0)
            & (np.sum(np.cross(projected, u1) * normals, axis=1) >= 0)
        )
        cross_track[~on_arc] = np.pi
        endpoint_min = np.minimum(separations[:-1], separations[1:])
        segment_min = np.minimum(cross_track, endpoint_min)
        target_visible = np.minimum(target_alt_deg[:-1], target_alt_deg[1:]) > -0.5
        sat_visible = np.maximum(sat_alt_deg[:-1], sat_alt_deg[1:]) > -0.5
        candidate_indices = np.flatnonzero(
            (segment_min <= math.radians(catch_radius_deg))
            & target_visible
            & sat_visible
        )
        if candidate_indices.size == 0:
            return []
        step = self.settings.satellite_coarse_step_seconds
        windows: list[CandidateWindow] = []
        for index in candidate_indices:
            left = times[int(index)]
            right = times[int(index) + 1]
            center_time = left + (right - left) / 2.0
            windows.append(CandidateWindow(target=target, center_time=center_time))
        return windows

    def _select_and_refine(
        self,
        record: SatelliteRecord,
        target: Target,
        window: CandidateWindow,
        now: datetime,
    ) -> TransitEvent | None:
        grid = self._grid
        target_at = self._target_interpolator(target, window.center_time)
        # Every campus grid point is screened across the candidate window.
        # The best points are then refined continuously in time.
        sample_offsets = (
            -self.settings.satellite_coarse_step_seconds / 2.0,
            0.0,
            self.settings.satellite_coarse_step_seconds / 2.0,
        )
        center = self.campus.center
        origin = geodetic_to_ecef(center.latitude_deg, center.longitude_deg, center.altitude_m)
        rotation = enu_rotation(center.latitude_deg, center.longitude_deg)
        best_separations = np.full(len(grid), np.inf)
        # Screen the whole grid in arrays. Only the best points need expensive
        # topocentric apparent-place/time refinement below.
        for offset in sample_offsets:
            sample_time = window.center_time + timedelta(seconds=offset)
            try:
                target_pos = target_at(center, sample_time)
                sat_pos = self.astronomy.satellite_position(record.satrec, center, sample_time)
            except RuntimeError:
                continue
            if target_pos.altitude_deg <= 0 or sat_pos.altitude_deg <= 0:
                continue
            target_ecef = origin + rotation.T @ (
                unit_vector_rows(np.asarray([target_pos.azimuth_deg]), np.asarray([target_pos.altitude_deg]))[0]
                * target_pos.distance_m
            )
            sat_ecef = origin + rotation.T @ (sat_pos.vector_enu * sat_pos.range_m)
            a = target_ecef - self._grid_ecef
            b = sat_ecef - self._grid_ecef
            a /= np.linalg.norm(a, axis=1)[:, None]
            b /= np.linalg.norm(b, axis=1)[:, None]
            separations = np.arccos(np.clip(np.sum(a * b, axis=1), -1, 1))
            best_separations = np.minimum(best_separations, separations)
        ranked = [(float(best_separations[index]), grid[index]) for index in np.argsort(best_separations)[:20]
                  if np.isfinite(best_separations[index])]
        if not ranked:
            return None
        candidates = ranked[: min(20, len(ranked))]
        refined: list[RefinedConjunction] = []
        for _, point in candidates:
            try:
                conjunction = self._refine_at_observer(
                    record, target, point, window.center_time, target_at=target_at
                )
            except (RuntimeError, ValueError):
                continue
            if conjunction and conjunction.minimum_separation_arcsec <= conjunction.threshold_arcsec:
                refined.append(conjunction)
        if not refined:
            return None
        best = min(refined, key=lambda item: item.minimum_separation_arcsec)
        for point in self.campus.refine(best.observer, self.settings.campus_refine_meters):
            conjunction = self._refine_at_observer(record, target, point, best.minimum_time, target_at=target_at)
            if conjunction and conjunction.minimum_separation_arcsec < best.minimum_separation_arcsec:
                best = conjunction
        # Final event/contact times always use exact apparent places, independently
        # of the interpolation used to rank and refine the observation grid.
        best = self._refine_at_observer(record, target, best.observer, best.minimum_time)
        return self._build_event(record, target, best, now) if best else None

    def _target_interpolator(self, target: Target, center_time: datetime):
        center = self.campus.center
        half_window = max(5.0, self.settings.satellite_coarse_step_seconds)
        offsets = np.arange(-half_window - 4, half_window + 8, 4.0)
        values = [center_time + timedelta(seconds=float(offset)) for offset in offsets]
        az, alt, distances, _ = self.astronomy.targets_at(target.value, center, values)
        origin = geodetic_to_ecef(center.latitude_deg, center.longitude_deg, center.altitude_m)
        rotation = enu_rotation(center.latitude_deg, center.longitude_deg)
        positions = origin + (unit_vector_rows(az, alt) * distances[:, None]) @ rotation
        spline = CubicSpline(offsets, positions, extrapolate=False)
        radius_m = 695_700_000.0 if target == Target.SUN else 1_737_400.0

        def target_at(observer: Geodetic, value: datetime) -> TargetPosition:
            offset = (value - center_time).total_seconds()
            if not offsets[0] <= offset <= offsets[-1]:
                return self.astronomy.target_at(target.value, observer, value)
            relative = spline(offset) - geodetic_to_ecef(observer.latitude_deg, observer.longitude_deg, observer.altitude_m)
            enu = enu_rotation(observer.latitude_deg, observer.longitude_deg) @ relative
            distance = float(np.linalg.norm(relative))
            return TargetPosition(
                math.degrees(math.atan2(enu[0], enu[1])) % 360,
                math.degrees(math.atan2(enu[2], math.hypot(enu[0], enu[1]))),
                distance, math.degrees(2 * math.atan(radius_m / (2 * distance))) * 3600,
            )
        return target_at

    def _refine_at_observer(
        self,
        record: SatelliteRecord,
        target: Target,
        point: ObserverPoint,
        center_time: datetime,
        *,
        target_at=None,
    ) -> RefinedConjunction | None:
        observer = geodetic_for_observer(point)
        half_window = max(5.0, self.settings.satellite_coarse_step_seconds)

        def evaluate(offset_seconds: float) -> tuple[datetime, SatellitePosition, TargetPosition, float]:
            value = center_time + timedelta(seconds=float(offset_seconds))
            target_position = (target_at(observer, value) if target_at else
                               self.astronomy.target_at(target.value, observer, value))
            satellite_position = self.astronomy.satellite_position(record.satrec, observer, value)
            separation = angular_separation_arcsec(
                satellite_position.azimuth_deg,
                satellite_position.altitude_deg,
                target_position.azimuth_deg,
                target_position.altitude_deg,
            )
            return value, satellite_position, target_position, separation

        opt = minimize_scalar(
            lambda offset: evaluate(offset)[3],
            bounds=(-half_window, half_window),
            method="bounded",
            options={"xatol": 0.01},
        )
        # A monotonic interval ending inside the disk does not locate a true
        # closest approach. The adjacent coarse interval will bracket it.
        if abs(float(opt.x)) >= half_window - 0.05:
            return None
        minimum_time, sat_pos, target_pos, min_sep = evaluate(float(opt.x))
        if target_pos.altitude_deg <= 0.0 or sat_pos.altitude_deg <= 0.0:
            return None
        sat_radius = record.size.angular_diameter_arcsec(sat_pos.range_m) / 2.0 if record.size else 0.0
        threshold = target_pos.angular_radius_arcsec + sat_radius
        if min_sep > threshold:
            return None

        start_s = _crossing_offset(
            evaluate,
            float(opt.x),
            float(opt.x) - 3600.0,
            threshold,
            direction=-1,
        )
        end_s = _crossing_offset(
            evaluate,
            float(opt.x),
            float(opt.x) + 3600.0,
            threshold,
            direction=1,
        )
        start_time = minimum_time + timedelta(seconds=start_s - float(opt.x))
        end_time = minimum_time + timedelta(seconds=end_s - float(opt.x))
        return RefinedConjunction(
            observer=point,
            minimum_time=minimum_time,
            minimum_separation_arcsec=min_sep,
            satellite=sat_pos,
            target_position=target_pos,
            threshold_arcsec=threshold,
            start_time=start_time,
            end_time=end_time,
        )

    def _build_event(
        self,
        record: SatelliteRecord,
        target: Target,
        conjunction: RefinedConjunction,
        now: datetime,
    ) -> TransitEvent:
        weather = self.weather.evaluate(target.value, conjunction.minimum_time, now)
        diameter_arcsec = None
        diameter_min_arcsec = None
        diameter_max_arcsec = None
        size_min = None
        size_max = None
        physical_size = None
        size_source = None
        size_confidence = None
        if record.size:
            physical_size = record.size.maximum_m
            size_min = record.size.minimum_m
            size_max = record.size.maximum_m
            diameter_min_arcsec, diameter_max_arcsec = (
                record.size.angular_diameter_range_arcsec(conjunction.satellite.range_m)
            )
            diameter_arcsec = diameter_max_arcsec
            size_source = record.size.source_url
            size_confidence = record.size.confidence
        reasons = list(weather.evidence.reasons)
        if record.size is None:
            reasons.append("object_size_unknown_centerline_only")
        tle_age = tle_age_seconds(record.entry, now)
        propagation_age = abs((conjunction.minimum_time - record.entry.epoch).total_seconds()) / 3600.0
        if tle_age > self.settings.tle_stale_seconds:
            reasons.append("orbit_epoch_old")
        if propagation_age > 24:
            reasons.append("long_orbit_extrapolation")
        confidence = weather.evidence.score
        if record.size is not None:
            confidence = min(1.0, confidence * 1.05)
        if tle_age > 3 * 3600:
            confidence *= 0.9
        return TransitEvent(
            event_type=EventType.SATELLITE,
            target=target,
            name=record.entry.name,
            identifier=str(record.entry.norad_id),
            start_time=conjunction.start_time,
            minimum_time=conjunction.minimum_time,
            end_time=conjunction.end_time,
            duration_ms=max(0, int((conjunction.end_time - conjunction.start_time).total_seconds() * 1000)),
            min_separation_arcsec=conjunction.minimum_separation_arcsec,
            target_angular_radius_arcsec=conjunction.target_position.angular_radius_arcsec,
            object_angular_diameter_arcsec=diameter_arcsec,
            object_angular_diameter_min_arcsec=diameter_min_arcsec,
            object_angular_diameter_max_arcsec=diameter_max_arcsec,
            physical_size_m=physical_size,
            physical_size_min_m=size_min,
            physical_size_max_m=size_max,
            physical_size_source=size_source,
            physical_size_confidence=size_confidence,
            angular_diameter_method="max_dimension" if record.size else "unknown",
            size_unknown=record.size is None,
            range_km=conjunction.satellite.range_m / 1000.0,
            observer=conjunction.observer,
            azimuth_deg=conjunction.satellite.azimuth_deg,
            altitude_deg=conjunction.satellite.altitude_deg,
            target_azimuth_deg=conjunction.target_position.azimuth_deg,
            target_altitude_deg=conjunction.target_position.altitude_deg,
            weather=weather.evidence,
            confidence=max(0.0, min(1.0, confidence)),
            orbit_epoch=record.entry.epoch,
            orbit_age_at_event_hours=propagation_age,
            reason_codes=reasons,
        )


def geodetic_for_observer(observer: ObserverPoint) -> Geodetic:
    return Geodetic(observer.latitude, observer.longitude, observer.elevation_m)


def _crossing_offset(
    evaluate,
    center: float,
    limit: float,
    threshold: float,
    *,
    direction: int,
) -> float:
    step = 0.01
    previous = center
    previous_value = evaluate(previous)[3] - threshold
    if abs(previous_value) < 1e-9:
        return center
    while previous != limit:
        current = center + direction * min(step, abs(limit - center))
        current_value = evaluate(current)[3] - threshold
        if current_value >= 0 and previous_value < 0:
            return brentq(
                lambda value: evaluate(value)[3] - threshold,
                min(previous, current),
                max(previous, current),
                xtol=1e-4,
            )
        previous = current
        previous_value = current_value
        step *= 2.0
    raise RuntimeError("Could not bracket transit contact within one hour")


def _time_grid(start: datetime, end: datetime, step_seconds: float) -> list[datetime]:
    count = int(math.floor((end - start).total_seconds() / step_seconds)) + 1
    return [start + timedelta(seconds=index * step_seconds) for index in range(count)]


def _group_indices(indices: np.ndarray, gap: int) -> list[np.ndarray]:
    if indices.size == 0:
        return []
    groups: list[list[int]] = [[int(indices[0])]]
    for raw in indices[1:]:
        value = int(raw)
        if value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [np.asarray(group, dtype=int) for group in groups]


def _campus_radius_m(campus: Campus) -> float:
    min_lon, min_lat, max_lon, max_lat = campus.bounds
    lat_m = (max_lat - min_lat) * 111_320.0
    lon_m = (max_lon - min_lon) * 111_320.0 * math.cos(math.radians(campus.center.latitude_deg))
    return 0.5 * math.hypot(lat_m, lon_m)


def _deduplicate_events(events: list[TransitEvent]) -> list[TransitEvent]:
    result: list[TransitEvent] = []
    for event in events:
        duplicate = next(
            (
                previous
                for previous in result
                if previous.identifier == event.identifier
                and previous.target == event.target
                and abs((previous.minimum_time - event.minimum_time).total_seconds()) < 2.0
            ),
            None,
        )
        if duplicate is None:
            result.append(event)
        elif event.min_separation_arcsec < duplicate.min_separation_arcsec:
            result[result.index(duplicate)] = event
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
