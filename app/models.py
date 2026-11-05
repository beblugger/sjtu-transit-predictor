from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Target(str, Enum):
    SUN = "sun"
    MOON = "moon"


class EventType(str, Enum):
    SATELLITE = "satellite"


class Announcement(str, Enum):
    ANNOUNCED = "announced"
    SUPPRESSED = "suppressed"
    UNAVAILABLE = "unavailable"


class ObserverPoint(BaseModel):
    latitude: float
    longitude: float
    elevation_m: float = 0.0
    campus_cell_id: str | None = None


class WeatherEvidence(BaseModel):
    decision: Announcement
    score: float = Field(ge=0.0, le=1.0)
    p0_clear: float = Field(ge=0.0, le=1.0)
    p3_clear: float = Field(ge=0.0, le=1.0)
    p12_clear: float = Field(ge=0.0, le=1.0)
    model_scores: dict[str, float] = Field(default_factory=dict)
    model_gap: float = Field(default=0.0, ge=0.0, le=1.0)
    run_change: float = Field(default=0.0, ge=0.0, le=1.0)
    threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    horizon_hours: float = 0.0
    observation_time: datetime | None = None
    model_updated_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class TransitEvent(BaseModel):
    event_type: EventType
    target: Target
    name: str
    identifier: str
    start_time: datetime
    minimum_time: datetime
    end_time: datetime
    duration_ms: int
    min_separation_arcsec: float
    target_angular_radius_arcsec: float
    object_angular_diameter_arcsec: float | None = None
    object_angular_diameter_min_arcsec: float | None = None
    object_angular_diameter_max_arcsec: float | None = None
    physical_size_m: float | None = None
    physical_size_min_m: float | None = None
    physical_size_max_m: float | None = None
    physical_size_source: str | None = None
    physical_size_confidence: str | None = None
    angular_diameter_method: Literal["max_dimension", "unknown"] = "unknown"
    size_unknown: bool = False
    range_km: float
    observer: ObserverPoint
    azimuth_deg: float
    altitude_deg: float
    target_azimuth_deg: float
    target_altitude_deg: float
    weather: WeatherEvidence
    confidence: float = Field(ge=0.0, le=1.0)
    orbit_epoch: datetime | None = None
    orbit_age_at_event_hours: float | None = None
    reason_codes: list[str] = Field(default_factory=list)
    visibility: dict | None = None
    transit_diagram: dict | None = None


class WeatherStatus(BaseModel):
    available: bool
    stale: bool
    observed_at: datetime | None
    model_updated_at: datetime | None
    current_cloud_cover: float | None
    current_precipitation: float | None
    current_weather_code: int | None
    current_condition: Literal["clear", "overcast", "rain", "unknown"]
    models: list[str] = Field(default_factory=list)
