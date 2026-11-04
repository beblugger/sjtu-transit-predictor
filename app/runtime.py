"""Construct the services used by one finite static-data build."""
from __future__ import annotations

from dataclasses import dataclass

from app.astronomy import CelestialCalculator
from app.campus import Campus, load_campus
from app.config import Settings, get_settings
from app.satellite_sizes import SatelliteSizeCatalog
from app.satellites import SatelliteService
from app.store import DatabaseStore
from app.weather import WeatherService


@dataclass
class Runtime:
    settings: Settings
    campus: Campus
    store: DatabaseStore
    astronomy: CelestialCalculator
    sizes: SatelliteSizeCatalog
    weather: WeatherService
    satellites: SatelliteService


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    campus = load_campus(settings.campus_geojson_path)
    store = DatabaseStore(settings.database_path)
    sizes = SatelliteSizeCatalog(settings.satellite_sizes_path)
    astronomy = CelestialCalculator(settings.resolved_ephemeris_path)
    weather = WeatherService(settings, store=store)
    satellites = SatelliteService(settings, campus, astronomy, sizes, weather, store=store)
    return Runtime(settings, campus, store, astronomy, sizes, weather, satellites)
