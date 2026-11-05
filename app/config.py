from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRANSIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: Path = Path("./transit.db")
    ephemeris_path: Path = Path("~/.cache/sjtu-transit/de421.bsp")

    campus_geojson_path: Path = Path(__file__).parent / "data" / "campus_minhang.geojson"
    satellite_sizes_path: Path = Path(__file__).parent / "data" / "satellite_sizes.yaml"

    celestrak_active_url: str = (
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json"
    )
    community_orbit_url: str = "https://raw.githubusercontent.com/satvisorcom/satvisor-data/master/celestrak/json/active.json"
    celestrak_update_url: str = "https://celestrak.org/NORAD/elements/"
    celestrak_timeout_seconds: float = 20.0
    celestrak_daily_download_limit: int = Field(default=3, ge=1)
    tle_refresh_seconds: int = Field(default=7200, ge=7200)
    tle_stale_seconds: int = 6 * 3600
    satellite_hours: int = Field(default=96, ge=1, le=96)
    satellite_coarse_step_seconds: float = 60.0
    satellite_max_targets: int = 0
    campus_grid_meters: float = 100.0
    campus_refine_meters: float = 25.0

    weather_latitude: float = 31.02807
    weather_longitude: float = 121.42839
    weather_refresh_seconds: int = 900
    weather_ensemble_refresh_seconds: int = 3 * 3600
    weather_stale_seconds: int = 30 * 60
    weather_ensemble_stale_seconds: int = 12 * 3600
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_ensemble_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
    weather_models: tuple[str, ...] = ("ecmwf_ifs025", "gfs025")
    weather_cloud_overcast_pct: float = 80.0
    weather_rain_threshold_mm: float = 0.1

    @property
    def resolved_ephemeris_path(self) -> Path:
        return self.ephemeris_path.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
