from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.aircraft import AircraftService
from app.astronomy import CelestialCalculator
from app.campus import load_campus
from app.config import Settings, get_settings
from app.models import ProviderHealth
from app.providers.variflight import (
    AircraftDataProvider,
    UnavailableAircraftProvider,
    VariflightRadarProvider,
)
from app.satellite_sizes import SatelliteSizeCatalog
from app.satellites import SatelliteService
from app.store import DatabaseStore
from app.weather import WeatherService


@dataclass
class AppServices:
    settings: Settings
    campus: object
    store: DatabaseStore
    astronomy: CelestialCalculator
    sizes: SatelliteSizeCatalog
    weather: WeatherService
    satellites: SatelliteService
    aircraft_provider: AircraftDataProvider
    aircraft: AircraftService
    _tasks: list[asyncio.Task] | None = None

    async def start(self) -> None:
        self._tasks = []
        await asyncio.gather(
            self.weather.refresh_deterministic(),
            self.weather.refresh_ensemble(),
        )
        await self.satellites.refresh_catalog()
        self._tasks.extend(
            [
                asyncio.create_task(
                    _loop(
                        self.satellites.refresh_catalog,
                        self.settings.tle_refresh_seconds,
                        "tle-refresh",
                    )
                ),
                asyncio.create_task(
                    _loop(
                        self.weather.refresh_deterministic,
                        self.settings.weather_refresh_seconds,
                        "weather-refresh",
                        after=self.satellites.reapply_weather,
                    )
                ),
                asyncio.create_task(
                    _loop(
                        self.weather.refresh_ensemble,
                        self.settings.weather_ensemble_refresh_seconds,
                        "weather-ensemble-refresh",
                        after=self.satellites.reapply_weather,
                    )
                ),
                asyncio.create_task(
                    _satellite_prediction_loop(self)
                ),
            ]
        )
        if self.aircraft_provider.enabled:
            self._tasks.append(
                asyncio.create_task(
                    _loop(
                        self.aircraft.poll_once,
                        self.settings.aircraft_poll_seconds,
                        "aircraft-poll",
                    )
                )
            )

    async def stop(self) -> None:
        if self._tasks:
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.store.close()

    def provider_health(self, now: datetime | None = None) -> list[ProviderHealth]:
        now = now or datetime.now(timezone.utc)
        weather_status = self.weather.status(now)
        tle_healthy = bool(self.satellites.records) and not self.satellites.catalog_is_stale(now)
        aircraft_enabled = self.aircraft_provider.enabled
        aircraft_healthy = (
            aircraft_enabled
            and self.aircraft.last_error is None
            and self.aircraft.updated_at is not None
            and (now - self.aircraft.updated_at) < timedelta(
                seconds=max(self.settings.aircraft_poll_seconds * 5, 60)
            )
        )
        return [
            ProviderHealth(
                name="open-meteo",
                enabled=True,
                healthy=weather_status.available and not weather_status.stale,
                detail=(
                    self.weather.last_error
                    or ("available" if weather_status.available else "not loaded")
                ),
                updated_at=weather_status.observed_at,
            ),
            ProviderHealth(
                name="celestrak",
                enabled=True,
                healthy=tle_healthy,
                detail=(
                    self.satellites.last_error
                    or f"{len(self.satellites.records)} TLE records from "
                    f"{self.satellites.catalog_source}"
                ),
                updated_at=self.satellites.updated_at,
            ),
            ProviderHealth(
                name=self.aircraft_provider.name,
                enabled=aircraft_enabled,
                healthy=aircraft_healthy,
                detail=(
                    self.aircraft.last_error
                    or (
                        f"{sum(len(value) for value in self.aircraft.tracks.values())} observations"
                        if aircraft_enabled
                        else "not configured"
                    )
                ),
                updated_at=self.aircraft.updated_at,
            ),
        ]


def build_services(settings: Settings | None = None) -> AppServices:
    settings = settings or get_settings()
    campus = load_campus(settings.campus_geojson_path)
    store = DatabaseStore(settings.database_path)
    sizes = SatelliteSizeCatalog(settings.satellite_sizes_path)
    astronomy = CelestialCalculator(settings.resolved_ephemeris_path)
    weather = WeatherService(settings, store=store)
    satellites = SatelliteService(
        settings,
        campus,
        astronomy,
        sizes,
        weather,
        fallback_provider=None,
        store=store,
    )
    provider = _aircraft_provider(settings)
    aircraft = AircraftService(
        settings,
        campus,
        astronomy,
        weather,
        provider,
        store=store,
    )
    return AppServices(
        settings=settings,
        campus=campus,
        store=store,
        astronomy=astronomy,
        sizes=sizes,
        weather=weather,
        satellites=satellites,
        aircraft_provider=provider,
        aircraft=aircraft,
    )


def _aircraft_provider(settings: Settings) -> AircraftDataProvider:
    if settings.ads_provider == "variflight" and settings.variflight_app_id and settings.variflight_app_security:
        return VariflightRadarProvider(
            settings.variflight_radar_url,
            settings.variflight_app_id,
            settings.variflight_app_security,
        )
    if settings.ads_provider == "variflight":
        return UnavailableAircraftProvider(
            "Set AIRCRAFT_VARIFLIGHT_APP_ID and AIRCRAFT_VARIFLIGHT_APP_SECURITY "
            "after obtaining a DataWorks trial."
        )
    return UnavailableAircraftProvider()


async def _loop(function, interval_seconds: float, name: str, *, after=None) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await function()
            if after is not None:
                after()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Individual services retain their last error and expose it via health.
            continue


async def _satellite_prediction_loop(runtime: AppServices) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            start = now.replace(minute=0, second=0)
            end = start + timedelta(hours=runtime.settings.satellite_hours)
            await runtime.satellites.events(start, end, now=now)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(3600)
