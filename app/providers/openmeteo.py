from __future__ import annotations

import asyncio
import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np


BAD_WMO_CODES = set(range(45, 49)) | set(range(51, 68)) | set(range(71, 78)) | set(
    range(80, 87)
) | {95, 96, 99}


@dataclass(frozen=True)
class IntervalWeather:
    times: list[datetime]
    precipitation_mm: np.ndarray
    cloud_cover_pct: np.ndarray
    weather_code: np.ndarray


@dataclass(frozen=True)
class DeterministicWeather:
    current_time: datetime
    current_precipitation_mm: float
    current_cloud_cover_pct: float
    current_weather_code: int
    minutely_15: IntervalWeather
    hourly: IntervalWeather
    precipitation_probability_pct: np.ndarray
    visibility_m: np.ndarray
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class EnsembleModelForecast:
    model: str
    times: list[datetime]
    cloud_cover_pct: np.ndarray
    precipitation_mm: np.ndarray
    weather_code: np.ndarray


@dataclass(frozen=True)
class EnsembleForecast:
    models: dict[str, EnsembleModelForecast]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OpenMeteoProvider:
    def __init__(
        self,
        forecast_url: str,
        ensemble_url: str,
        latitude: float,
        longitude: float,
        *,
        timeout_seconds: float = 40.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.forecast_url = forecast_url
        self.ensemble_url = ensemble_url
        self.latitude = latitude
        self.longitude = longitude
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def fetch_deterministic(self) -> DeterministicWeather:
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": "UTC",
            "forecast_days": 6,
            "past_days": 1,
            "current": "cloud_cover,precipitation,weather_code",
            "minutely_15": "precipitation,cloud_cover,weather_code",
            "hourly": (
                "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                "precipitation,precipitation_probability,visibility,weather_code"
            ),
        }
        payload = await self._get_json(self.forecast_url, params)
        current = payload["current"]
        minutely = payload["minutely_15"]
        hourly = payload["hourly"]
        return DeterministicWeather(
            current_time=_parse_time(current["time"]),
            current_precipitation_mm=float(current["precipitation"]),
            current_cloud_cover_pct=float(current["cloud_cover"]),
            current_weather_code=int(current["weather_code"]),
            minutely_15=_parse_interval(minutely),
            hourly=_parse_interval(hourly),
            precipitation_probability_pct=np.asarray(
                _none_to_nan(hourly.get("precipitation_probability", [])), dtype=float
            ),
            visibility_m=np.asarray(_none_to_nan(hourly.get("visibility", [])), dtype=float),
        )

    async def fetch_ensembles(self, models: tuple[str, ...]) -> EnsembleForecast:
        requests = [self._fetch_ensemble_model(model) for model in models]
        values = await asyncio.gather(*requests)
        return EnsembleForecast(models={item.model: item for item in values})

    async def _fetch_ensemble_model(self, model: str) -> EnsembleModelForecast:
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": "UTC",
            "forecast_days": 6,
            "past_days": 1,
            "hourly": "cloud_cover,precipitation,weather_code",
            "models": model,
        }
        payload = await self._get_json(self.ensemble_url, params)
        hourly = payload["hourly"]
        times = [_parse_time(value) for value in hourly["time"]]
        return EnsembleModelForecast(
            model=model,
            times=times,
            cloud_cover_pct=_member_matrix(hourly, "cloud_cover"),
            precipitation_mm=_member_matrix(hourly, "precipitation"),
            weather_code=_member_matrix(hourly, "weather_code"),
        )

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"User-Agent": "sjtu-transit-predictor/0.1"}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"Open-Meteo error: {payload.get('reason', payload['error'])}")
        return payload


def _parse_interval(data: dict[str, Any]) -> IntervalWeather:
    return IntervalWeather(
        times=[_parse_time(value) for value in data["time"]],
        precipitation_mm=np.asarray(_none_to_nan(data["precipitation"]), dtype=float),
        cloud_cover_pct=np.asarray(_none_to_nan(data["cloud_cover"]), dtype=float),
        weather_code=np.asarray(_none_to_nan(data["weather_code"]), dtype=float),
    )


def _member_matrix(hourly: dict[str, Any], name: str) -> np.ndarray:
    member_keys = sorted(
        key for key in hourly if key == name or key.startswith(f"{name}_member")
    )
    if not member_keys:
        raise ValueError(f"ensemble field {name!r} not present")
    return np.column_stack(
        [np.asarray(_none_to_nan(hourly[key]), dtype=float) for key in member_keys]
    )


def _none_to_nan(values: list[Any]) -> list[float]:
    return [math.nan if value is None else float(value) for value in values]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def nearest_index(times: list[datetime], target: datetime) -> int:
    if not times:
        raise ValueError("empty weather series")
    target = _as_utc(target)
    pos = bisect.bisect_left(times, target)
    if pos <= 0:
        return 0
    if pos >= len(times):
        return len(times) - 1
    before = times[pos - 1]
    after = times[pos]
    return pos - 1 if target - before <= after - target else pos


def condition_bad(
    cloud_cover_pct: float,
    precipitation_mm: float,
    weather_code: float,
    *,
    cloud_threshold_pct: float = 80.0,
    rain_threshold_mm: float = 0.1,
) -> bool:
    if math.isnan(cloud_cover_pct) or math.isnan(precipitation_mm):
        return True
    code = int(weather_code) if not math.isnan(weather_code) else -1
    return (
        precipitation_mm >= rain_threshold_mm
        or cloud_cover_pct >= cloud_threshold_pct
        or code in BAD_WMO_CODES
    )


def current_condition(weather: DeterministicWeather) -> str:
    if condition_bad(
        weather.current_cloud_cover_pct,
        weather.current_precipitation_mm,
        weather.current_weather_code,
    ):
        if weather.current_precipitation_mm >= 0.1:
            return "rain"
        return "overcast"
    return "clear"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
