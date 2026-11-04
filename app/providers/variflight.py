from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from app.geo import Geodetic


@dataclass(frozen=True)
class AircraftObservation:
    identifier: str
    observed_at: datetime
    position: Geodetic
    callsign: str | None = None
    icao24: str | None = None
    aircraft_type: str | None = None
    registration: str | None = None
    ground_speed_mps: float | None = None
    track_deg: float | None = None
    vertical_rate_mps: float | None = None
    source: str = "unknown"


class AircraftDataProvider(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def name(self) -> str: ...

    async def fetch_around(
        self,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
    ) -> list[AircraftObservation]: ...

    async def enrich(
        self,
        observation: AircraftObservation,
    ) -> AircraftObservation | None: ...


class UnavailableAircraftProvider:
    name = "unavailable"

    def __init__(self, detail: str = "Aircraft ADS-B provider is not configured"):
        self.detail = detail

    @property
    def enabled(self) -> bool:
        return False

    async def fetch_around(
        self,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
    ) -> list[AircraftObservation]:
        return []

    async def enrich(
        self,
        observation: AircraftObservation,
    ) -> AircraftObservation | None:
        return None


class VariflightRadarProvider:
    """Official DataWorks Radar ADS-B V2 area query.

    Credentials are obtained through a DataWorks trial or data agreement.
    """

    name = "variflight-dataworks-radar"

    def __init__(
        self,
        url: str,
        app_id: str,
        app_security: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url
        self.app_id = app_id
        self.app_security = app_security
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_security)

    async def fetch_around(
        self,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
    ) -> list[AircraftObservation]:
        if not self.enabled:
            return []
        now = datetime.now(timezone.utc)
        params = {
            "appid": self.app_id,
            "latcenter": _format_number(center_latitude),
            "longcenter": _format_number(center_longitude),
            "radius": _format_number(radius_km, 3),
        }
        params["token"] = signed_token(params, self.app_security)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = await client.get(self.url, params=params)
            response.raise_for_status()
            payload = response.json()
        if payload.get("error_code") not in (None, 0, "0"):
            raise RuntimeError(
                f"VariFlight radar error {payload.get('error_code')}: {payload.get('error')}"
            )
        rows = payload.get("data") or []
        observations: list[AircraftObservation] = []
        for row in rows:
            try:
                latitude = float(row["lat"])
                longitude = float(row["lng"])
                height = float(row.get("height") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            speed_kph = _optional_float(row.get("speed"))
            track = _optional_float(row.get("angle"))
            callsign = str(row.get("flightNo") or row.get("fnum") or "").strip() or None
            identifier = callsign or f"{latitude:.5f},{longitude:.5f}"
            observations.append(
                AircraftObservation(
                    identifier=identifier,
                    callsign=callsign,
                    observed_at=now,
                    position=Geodetic(latitude, longitude, height),
                    ground_speed_mps=(speed_kph / 3.6 if speed_kph is not None else None),
                    track_deg=track,
                    vertical_rate_mps=None,
                    aircraft_type=str(row.get("aircraftType") or row.get("airModels") or "") or None,
                    source=self.name,
                )
            )
        return observations

    async def enrich(
        self,
        observation: AircraftObservation,
    ) -> AircraftObservation | None:
        return None


class VariflightMCPProvider:
    """Optional per-tail enrichment through the official Aviation MCP API."""

    name = "variflight-aviation-mcp"

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def fetch_around(
        self,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
    ) -> list[AircraftObservation]:
        return []

    async def enrich(
        self,
        observation: AircraftObservation,
    ) -> AircraftObservation | None:
        if not self.enabled or not observation.registration:
            return None
        payload = {
            "endpoint": "realtimeLocation",
            "params": {"anum": observation.registration},
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = await client.post(
                self.url,
                json=payload,
                headers={"X-VARIFLIGHT-KEY": self.api_key},
            )
            response.raise_for_status()
            result = response.json()
        data = result.get("data", result)
        position = data.get("position") or {}
        latitude = _optional_float(position.get("lat") or data.get("lat"))
        longitude = _optional_float(position.get("lng") or data.get("lng"))
        if latitude is None or longitude is None:
            return None
        timestamp = _optional_float(data.get("updatetime"))
        observed_at = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if timestamp is not None
            else observation.observed_at
        )
        speed = _optional_float(data.get("speed"))
        speed_mps = speed / 3.6 if speed is not None else observation.ground_speed_mps
        return AircraftObservation(
            identifier=observation.identifier,
            observed_at=observed_at,
            position=Geodetic(
                latitude,
                longitude,
                _optional_float(data.get("height")) or observation.position.altitude_m,
            ),
            callsign=str(data.get("fnum") or observation.callsign or "") or None,
            aircraft_type=observation.aircraft_type,
            registration=str(data.get("anum") or observation.registration or "") or None,
            ground_speed_mps=speed_mps,
            track_deg=_optional_float(data.get("angle")) or observation.track_deg,
            vertical_rate_mps=_optional_float(data.get("vspeed")),
            source=self.name,
        )


def signed_token(params: dict[str, str], app_security: str) -> str:
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    first = hashlib.md5((query + app_security).encode("utf-8")).hexdigest()
    return hashlib.md5(first.encode("utf-8")).hexdigest()


def _format_number(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
