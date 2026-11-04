from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIRCRAFT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SJTU Transit Predictor"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: Path = Path("./transit.db")
    ephemeris_path: Path = Path("~/.cache/sjtu-transit/de421.bsp")

    campus_geojson_path: Path = Path(__file__).parent / "data" / "campus_minhang.geojson"
    satellite_sizes_path: Path = Path(__file__).parent / "data" / "satellite_sizes.yaml"

    celestrak_active_url: str = (
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json"
    )
    community_orbit_url: str = "https://raw.githubusercontent.com/satvisorcom/satvisor-data/master/celestrak/json/active.json"
    celestrak_timeout_seconds: float = 20.0
    satnogs_tle_url: str = "https://db.satnogs.org/api/tle/?format=json"
    tle_refresh_seconds: int = 7200
    tle_stale_seconds: int = 6 * 3600
    satellite_hours: int = Field(default=96, ge=1, le=96)
    satellite_coarse_step_seconds: float = 60.0
    satellite_fine_step_seconds: float = 0.25
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

    ads_provider: str = "none"
    aircraft_query_radius_km: float = 100.0
    aircraft_poll_seconds: float = 5.0
    aircraft_horizon_seconds: int = 600
    aircraft_min_samples: int = 4
    aircraft_track_retention_seconds: int = 180
    variflight_app_id: str = ""
    variflight_app_security: str = Field(default="", repr=False)
    variflight_radar_url: str = "https://open-al.variflight.com/api/radarv2"
    variflight_mcp_url: str = "https://mcp.variflight.com/api/v1/mcp/data"
    variflight_api_key: str = Field(default="", repr=False)

    @property
    def resolved_ephemeris_path(self) -> Path:
        return self.ephemeris_path.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
