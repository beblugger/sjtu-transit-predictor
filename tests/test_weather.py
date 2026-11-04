from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from app.config import Settings
from app.providers.openmeteo import (
    DeterministicWeather,
    EnsembleForecast,
    EnsembleModelForecast,
    IntervalWeather,
)
from app.weather import WeatherService


def _weather(now: datetime, *, clear: bool=True) -> WeatherService:
    times = [now + timedelta(hours=index) for index in range(73)]
    cloud = 10.0 if clear else 100.0
    rain = 0.0 if clear else 1.0
    code = 0 if clear else 63
    deterministic = DeterministicWeather(
        current_time=now,
        current_precipitation_mm=rain,
        current_cloud_cover_pct=cloud,
        current_weather_code=code,
        minutely_15=IntervalWeather(
            times=[now + timedelta(minutes=15 * index) for index in range(289)],
            precipitation_mm=np.full(289, rain),
            cloud_cover_pct=np.full(289, cloud),
            weather_code=np.full(289, code),
        ),
        hourly=IntervalWeather(
            times=times,
            precipitation_mm=np.full(len(times), rain),
            cloud_cover_pct=np.full(len(times), cloud),
            weather_code=np.full(len(times), code),
        ),
        precipitation_probability_pct=np.zeros(len(times)),
        visibility_m=np.full(len(times), 20_000.0),
        fetched_at=now,
    )
    models = {}
    for model in ("ecmwf_ifs025", "gfs025"):
        members = np.full((len(times), 5), cloud)
        models[model] = EnsembleModelForecast(
            model=model,
            times=times,
            cloud_cover_pct=members,
            precipitation_mm=np.full((len(times), 5), rain),
            weather_code=np.full((len(times), 5), code),
        )
    service = WeatherService(Settings())
    service.deterministic = deterministic
    service.ensemble = EnsembleForecast(models=models, fetched_at=now)
    return service


def test_persistent_clear_event_is_announced():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    result = _weather(now, clear=True).evaluate("moon", now + timedelta(hours=24), now)
    assert result.usable
    assert result.evidence.decision.value == "announced"
    assert result.evidence.score >= result.evidence.threshold


def test_persistent_rain_event_is_suppressed():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    result = _weather(now, clear=False).evaluate("sun", now + timedelta(hours=12), now)
    assert result.evidence.decision.value == "suppressed"
    assert result.evidence.score == 0.0


def test_missing_weather_never_claims_clear():
    service = WeatherService(Settings())
    result = service.evaluate("sun", datetime.now(timezone.utc) + timedelta(hours=3))
    assert result.evidence.decision.value == "unavailable"
    assert not result.usable


def test_out_of_range_weather_is_unavailable_not_nearest_day():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    service = _weather(now)
    for hours in (80, 96, 65):
        result = service.evaluate("sun", now + timedelta(hours=hours), now)
        assert not result.usable
        assert "weather_window_not_covered" in result.evidence.reasons


def test_weather_snapshot_roundtrip_preserves_fetch_time_and_members():
    import json
    now = datetime.now(timezone.utc).replace(microsecond=0)
    original = _weather(now)
    snapshot = json.loads(json.dumps(original.snapshot(), allow_nan=False))
    restored = WeatherService(Settings())
    restored.restore(snapshot)
    assert restored.deterministic.fetched_at == now
    assert restored.ensemble.fetched_at == now
    assert restored.evaluate("moon", now + timedelta(hours=24), now) == original.evaluate("moon", now + timedelta(hours=24), now)
