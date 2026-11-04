from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

import numpy as np

from app.config import Settings
from app.models import Announcement, WeatherEvidence, WeatherStatus
from app.providers.openmeteo import (
    DeterministicWeather,
    EnsembleForecast,
    EnsembleModelForecast,
    IntervalWeather,
    OpenMeteoProvider,
    condition_bad,
    current_condition,
    nearest_index,
)


def announcement_threshold(horizon_hours: float) -> float:
    if horizon_hours <= 2.0:
        return 0.70
    if horizon_hours <= 12.0:
        return 0.78
    if horizon_hours <= 24.0:
        return 0.85
    return 0.90


@dataclass(frozen=True)
class WeatherEvaluation:
    evidence: WeatherEvidence
    usable: bool


class WeatherService:
    def __init__(
        self,
        settings: Settings,
        provider: OpenMeteoProvider | None = None,
        store=None,
    ):
        self.settings = settings
        self.provider = provider or OpenMeteoProvider(
            settings.open_meteo_forecast_url,
            settings.open_meteo_ensemble_url,
            settings.weather_latitude,
            settings.weather_longitude,
        )
        self.deterministic: DeterministicWeather | None = None
        self.ensemble: EnsembleForecast | None = None
        self.updated_at: datetime | None = None
        self.last_error: str | None = None
        self._last_scores: dict[tuple[str, datetime], float] = {}
        self._lock = Lock()
        self.store = store
        if store is not None:
            cached = store.get_cache("weather_snapshot")
            if cached:
                try:
                    self.restore(cached[0])
                except (KeyError, TypeError, ValueError):
                    self.last_error = "invalid cached weather"

    def snapshot(self) -> dict:
        def encode(value):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, np.ndarray):
                return encode(value.tolist())
            if isinstance(value, list):
                return [encode(item) for item in value]
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, float) and not np.isfinite(value):
                return None
            return value
        return {
            "latitude": self.settings.weather_latitude,
            "longitude": self.settings.weather_longitude,
            "deterministic": encode(asdict(self.deterministic)) if self.deterministic else None,
            "ensemble": encode(asdict(self.ensemble)) if self.ensemble else None,
        }

    def restore(self, payload: dict) -> None:
        if (payload["latitude"], payload["longitude"]) != (self.settings.weather_latitude, self.settings.weather_longitude):
            return
        def interval(raw):
            return IntervalWeather(
                times=[_utc(datetime.fromisoformat(value)) for value in raw["times"]],
                **{name: np.asarray(raw[name], dtype=float) for name in
                   ("precipitation_mm", "cloud_cover_pct", "weather_code")},
            )
        raw = payload.get("deterministic")
        if raw:
            fields = dict(raw)
            fields["current_time"] = datetime.fromisoformat(raw["current_time"])
            fields["fetched_at"] = datetime.fromisoformat(raw["fetched_at"])
            for key in ("minutely_15", "hourly"):
                fields[key] = interval(raw[key])
            for key in ("precipitation_probability_pct", "visibility_m"):
                fields[key] = np.asarray(raw[key], dtype=float)
            self.deterministic = DeterministicWeather(**fields)
            self.updated_at = self.deterministic.fetched_at
        raw = payload.get("ensemble")
        if raw:
            models = {}
            for name, model in raw["models"].items():
                series = interval(model)
                models[name] = EnsembleModelForecast(name, series.times, series.cloud_cover_pct,
                                                    series.precipitation_mm, series.weather_code)
            self.ensemble = EnsembleForecast(models, datetime.fromisoformat(raw["fetched_at"]))

    def _save(self) -> None:
        if self.store is not None:
            self.store.set_cache("weather_snapshot", self.snapshot())

    async def refresh_deterministic(self) -> None:
        try:
            value = await self.provider.fetch_deterministic()
            with self._lock:
                self.deterministic = value
                self.updated_at = value.fetched_at
                self.last_error = None
            self._save()
        except Exception as exc:
            self.last_error = str(exc)

    async def refresh_ensemble(self) -> None:
        try:
            value = await self.provider.fetch_ensembles(self.settings.weather_models)
            with self._lock:
                self.ensemble = value
                self.last_error = None
            self._save()
        except Exception as exc:
            self.last_error = str(exc)

    def status(self, now: datetime | None = None) -> WeatherStatus:
        now = now or datetime.now(timezone.utc)
        weather = self.deterministic
        ensemble_stale = (
            self.ensemble is None
            or (now - _utc(self.ensemble.fetched_at)).total_seconds()
            > self.settings.weather_ensemble_stale_seconds
        )
        if weather is None:
            return WeatherStatus(
                available=False,
                stale=True,
                observed_at=None,
                model_updated_at=None,
                current_cloud_cover=None,
                current_precipitation=None,
                current_weather_code=None,
                current_condition="unknown",
                models=sorted(self.ensemble.models) if self.ensemble else [],
            )
        stale = (
            (now - _utc(weather.fetched_at)).total_seconds()
            > self.settings.weather_stale_seconds
            or ensemble_stale
        )
        return WeatherStatus(
            available=True,
            stale=stale,
            observed_at=weather.current_time,
            model_updated_at=self.ensemble.fetched_at if self.ensemble else None,
            current_cloud_cover=weather.current_cloud_cover_pct,
            current_precipitation=weather.current_precipitation_mm,
            current_weather_code=weather.current_weather_code,
            current_condition=current_condition(weather),
            models=sorted(self.ensemble.models) if self.ensemble else [],
        )

    def evaluate(
        self,
        target: str,
        event_time: datetime,
        now: datetime | None = None,
    ) -> WeatherEvaluation:
        now = _utc(now or datetime.now(timezone.utc))
        event_time = _utc(event_time)
        horizon_hours = max(0.0, (event_time - now).total_seconds() / 3600.0)
        threshold = announcement_threshold(horizon_hours)
        weather = self.deterministic
        ensemble = self.ensemble
        stale = (
            weather is None
            or (now - _utc(weather.fetched_at)).total_seconds() > self.settings.weather_stale_seconds
        )
        ensemble_stale = (
            ensemble is None
            or (now - _utc(ensemble.fetched_at)).total_seconds()
            > self.settings.weather_ensemble_stale_seconds
        )
        coverage_missing = ensemble is not None and (
            not ensemble.models or any(
                not model.times
                or model.times[0] > event_time - timedelta(hours=12)
                or model.times[-1] < event_time + timedelta(hours=12)
                for model in ensemble.models.values()
            )
        )
        if stale or ensemble_stale or coverage_missing:
            reasons = ["weather_unavailable"]
            if stale:
                reasons.append("deterministic_weather_stale")
            if ensemble_stale:
                reasons.append("ensemble_weather_stale_or_unavailable")
            if coverage_missing:
                reasons.append("weather_window_not_covered")
            return WeatherEvaluation(
                evidence=WeatherEvidence(
                    decision=Announcement.UNAVAILABLE,
                    score=0.0,
                    p0_clear=0.0,
                    p3_clear=0.0,
                    p12_clear=0.0,
                    threshold=threshold,
                    horizon_hours=horizon_hours,
                    observation_time=weather.current_time if weather else None,
                    model_updated_at=ensemble.fetched_at if ensemble else None,
                    reasons=reasons,
                ),
                usable=False,
            )

        reasons: list[str] = []
        if horizon_hours <= 2.0 and self._near_term_is_bad(weather, event_time):
            reasons.append("near_term_observed_overcast_or_precipitation")

        model_scores: dict[str, float] = {}
        p0_values: list[float] = []
        p3_values: list[float] = []
        p12_values: list[float] = []
        for model_name, model in ensemble.models.items():
            p0, p3, p12 = self._model_clear_fractions(model, event_time)
            model_score = _geometric_mean(p0, p3, p12)
            model_scores[model_name] = model_score
            p0_values.append(p0)
            p3_values.append(p3)
            p12_values.append(p12)
        p0 = float(np.mean(p0_values))
        p3 = float(np.mean(p3_values))
        p12 = float(np.mean(p12_values))
        score = float(np.mean(list(model_scores.values())))
        if not np.isfinite([p0, p3, p12, score]).all():
            return WeatherEvaluation(
                evidence=WeatherEvidence(
                    decision=Announcement.UNAVAILABLE,
                    score=0.0,
                    p0_clear=0.0,
                    p3_clear=0.0,
                    p12_clear=0.0,
                    model_scores=model_scores,
                    threshold=threshold,
                    horizon_hours=horizon_hours,
                    observation_time=weather.current_time,
                    model_updated_at=ensemble.fetched_at,
                    reasons=["weather_probability_invalid"],
                ),
                usable=False,
            )
        model_gap = (
            max(model_scores.values()) - min(model_scores.values())
            if len(model_scores) > 1
            else 0.0
        )
        key = (target.lower(), event_time)
        previous = self._last_scores.get(key)
        run_change = abs(score - previous) if previous is not None else 0.0
        with self._lock:
            self._last_scores[key] = score
            if len(self._last_scores) > 20_000:
                for old_key, _ in list(self._last_scores.items())[:10_000]:
                    self._last_scores.pop(old_key, None)

        if score < threshold:
            reasons.append("composite_score_below_horizon_threshold")
        if model_gap > 0.20:
            reasons.append("model_disagreement")
        if run_change > 0.20:
            reasons.append("forecast_run_unstable")
        if horizon_hours <= 2.0 and "near_term_observed_overcast_or_precipitation" in reasons:
            score = 0.0

        announced = not reasons
        return WeatherEvaluation(
            evidence=WeatherEvidence(
                decision=Announcement.ANNOUNCED if announced else Announcement.SUPPRESSED,
                score=score,
                p0_clear=p0,
                p3_clear=p3,
                p12_clear=p12,
                model_scores=model_scores,
                model_gap=min(1.0, model_gap),
                run_change=min(1.0, run_change),
                threshold=threshold,
                horizon_hours=horizon_hours,
                observation_time=weather.current_time,
                model_updated_at=ensemble.fetched_at,
                reasons=reasons,
            ),
            usable=True,
        )

    def _model_clear_fractions(
        self,
        model: EnsembleModelForecast,
        event_time: datetime,
    ) -> tuple[float, float, float]:
        index = nearest_index(model.times, event_time)
        bad = self._bad_member_matrix(model)
        clear = ~bad
        p0 = float(np.nanmean(clear[index]))
        p3 = _window_mean(clear, index, 3)
        p12 = _window_mean(clear, index, 12)
        return p0, p3, p12

    def _bad_member_matrix(self, model: EnsembleModelForecast) -> np.ndarray:
        if not (
            model.cloud_cover_pct.shape
            == model.precipitation_mm.shape
            == model.weather_code.shape
        ):
            raise ValueError(f"ensemble arrays have inconsistent shape for {model.model}")
        weather_codes = np.nan_to_num(model.weather_code, nan=-1.0).astype(int)
        return (
            (model.precipitation_mm >= self.settings.weather_rain_threshold_mm)
            | (model.cloud_cover_pct >= self.settings.weather_cloud_overcast_pct)
            | np.isin(
                weather_codes,
                list(range(45, 49))
                + list(range(51, 68))
                + list(range(71, 78))
                + list(range(80, 87))
                + [95, 96, 99],
            )
            | np.isnan(model.cloud_cover_pct)
            | np.isnan(model.precipitation_mm)
            | np.isnan(model.weather_code)
        )

    def _near_term_is_bad(self, weather: DeterministicWeather, event_time: datetime) -> bool:
        value = weather.minutely_15
        if not value.times or not value.times[0] <= event_time <= value.times[-1]:
            value = weather.hourly
        if not value.times or not value.times[0] <= event_time <= value.times[-1]:
            return True
        index = nearest_index(value.times, event_time)
        if condition_bad(
            value.cloud_cover_pct[index],
            value.precipitation_mm[index],
            value.weather_code[index],
            cloud_threshold_pct=self.settings.weather_cloud_overcast_pct,
            rain_threshold_mm=self.settings.weather_rain_threshold_mm,
        ):
            return True
        # Also stop if the current observation itself is already rainy.
        return condition_bad(
            weather.current_cloud_cover_pct,
            weather.current_precipitation_mm,
            weather.current_weather_code,
            cloud_threshold_pct=self.settings.weather_cloud_overcast_pct,
            rain_threshold_mm=self.settings.weather_rain_threshold_mm,
        )


def _window_mean(clear: np.ndarray, center: int, hours: int) -> float:
    start = max(0, center - hours)
    end = min(clear.shape[0], center + hours + 1)
    if start >= end:
        return 0.0
    return float(np.nanmean(clear[start:end]))


def _geometric_mean(a: float, b: float, c: float) -> float:
    values = np.clip(np.array([a, b, c], dtype=float), 0.0, 1.0)
    return float(np.prod(values) ** (1.0 / 3.0))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
