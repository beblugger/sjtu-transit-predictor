from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    Announcement,
    EventType,
    ObserverPoint,
    Target,
    TransitEvent,
    WeatherEvidence,
    WeatherStatus,
)


class FakeSatellites:
    records = [object()]
    updated_at = datetime.now(timezone.utc)

    def catalog_is_stale(self, now=None):
        return False

    async def events(self, start, end, now=None):
        return [_event()]


class FakeAircraft:
    last_error = None
    updated_at = datetime.now(timezone.utc)
    tracks = {}

    async def events(self, now=None):
        return []


class FakeProvider:
    name = "unavailable"
    enabled = False


class FakeWeather:
    def status(self):
        return WeatherStatus(
            available=True,
            stale=False,
            observed_at=datetime.now(timezone.utc),
            model_updated_at=datetime.now(timezone.utc),
            current_cloud_cover=10,
            current_precipitation=0,
            current_weather_code=0,
            current_condition="clear",
            models=["ecmwf_ifs025", "gfs025"],
        )


def _event() -> TransitEvent:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return TransitEvent(
        event_type=EventType.SATELLITE,
        target=Target.MOON,
        name="TEST SAT",
        identifier="12345",
        start_time=now,
        minimum_time=now + timedelta(milliseconds=500),
        end_time=now + timedelta(seconds=1),
        duration_ms=1000,
        min_separation_arcsec=10.0,
        target_angular_radius_arcsec=900.0,
        object_angular_diameter_arcsec=50.0,
        physical_size_m=100.0,
        physical_size_min_m=50.0,
        physical_size_max_m=100.0,
        range_km=400.0,
        observer=ObserverPoint(latitude=31.028, longitude=121.428),
        azimuth_deg=180.0,
        altitude_deg=45.0,
        target_azimuth_deg=180.0,
        target_altitude_deg=45.0,
        weather=WeatherEvidence(
            decision=Announcement.ANNOUNCED,
            score=0.95,
            p0_clear=1.0,
            p3_clear=0.95,
            p12_clear=0.9,
            threshold=0.7,
            reasons=[],
        ),
        confidence=0.9,
        reason_codes=[],
    )


def _services():
    return SimpleNamespace(
        settings=SimpleNamespace(),
        campus=SimpleNamespace(
            name="test campus",
            center=SimpleNamespace(latitude_deg=31.028, longitude_deg=121.428),
            bounds=(121.42, 31.02, 121.44, 31.04),
            rings=(
                (
                    (121.42, 31.02),
                    (121.44, 31.02),
                    (121.44, 31.04),
                    (121.42, 31.04),
                    (121.42, 31.02),
                ),
            ),
        ),
        satellites=FakeSatellites(),
        aircraft=FakeAircraft(),
        aircraft_provider=FakeProvider(),
        weather=FakeWeather(),
        provider_health=lambda: [],
    )


def test_events_api_and_html():
    app = create_app(services=_services(), start_services=False)
    with TestClient(app) as client:
        response = client.get("/api/v1/events?type=satellite&hours=2")
        assert response.status_code == 200
        payload = response.json()
        assert payload["announced_only"] is False
        assert len(payload["events"]) == 1
        assert payload["warnings"] == []
        html = client.get("/events.html?type=satellite&hours=2")
        assert html.status_code == 200
        assert "TEST SAT" in html.text
        assert "Open-Meteo" in html.text
