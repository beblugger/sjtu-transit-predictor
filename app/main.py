from __future__ import annotations

import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import get_settings
from app.models import (
    Announcement,
    EventListResponse,
    HealthResponse,
    WeatherStatus,
)
from app.services import AppServices, build_services

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app(services: AppServices | None = None, start_services: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = services or build_services(get_settings())
        app.state.services = runtime
        if start_services:
            await runtime.start()
        try:
            yield
        finally:
            if start_services:
                await runtime.stop()

    app = FastAPI(
        title="SJTU Transit Predictor",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/events.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get("/api/v1/events", response_model=EventListResponse)
    async def events(
        request: Request,
        kind: Literal["satellite", "aircraft", "all"] = Query("all", alias="type"),
        hours: int = Query(96, ge=1, le=96),
        announced_only: bool = Query(False),
    ):
        runtime = _services(request)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        end = now + timedelta(hours=hours)
        warnings: list[str] = []
        satellite_events = []
        aircraft_events = []
        if kind in ("satellite", "all"):
            if runtime.satellites.catalog_is_stale(now) and not runtime.satellites.records:
                warnings.append("satellite_catalog_unavailable")
            prediction_start = now.replace(minute=0, second=0)
            satellite_events = [
                event
                for event in await runtime.satellites.events(
                    prediction_start, end, now=now
                )
                if event.minimum_time >= now - timedelta(seconds=5)
            ]
        if kind in ("aircraft", "all"):
            if runtime.aircraft_provider.enabled:
                aircraft_events = await runtime.aircraft.events(now)
            else:
                warnings.append("aircraft_provider_unavailable")
        all_events = sorted(
            satellite_events + aircraft_events,
            key=lambda item: item.minimum_time,
        )
        selected = (
            [event for event in all_events if event.weather.decision == Announcement.ANNOUNCED]
            if announced_only
            else all_events
        )
        return EventListResponse(
            generated_at=now,
            from_time=now,
            to_time=end,
            announced_only=announced_only,
            events=selected,
            suppressed_count=len(all_events) - len(selected),
            warnings=warnings,
        )

    @app.get("/api/v1/weather/status", response_model=WeatherStatus)
    async def weather_status(request: Request):
        return _services(request).weather.status()

    @app.get("/api/v1/weather/snapshot")
    async def weather_snapshot(request: Request):
        return _services(request).weather.snapshot()

    @app.get("/api/v1/orbits")
    async def orbits(request: Request, format: Literal["json", "tle"] = "json"):
        runtime = _services(request)
        records = list(runtime.satellites.records)
        if format == "json":
            return [record.entry.as_omm() for record in records]
        from sgp4.exporter import export_tle
        lines = []
        omitted = 0
        for record in records:
            if record.entry.norad_id > 99999:
                omitted += 1
                continue
            lines.extend((record.entry.name, *export_tle(record.satrec)))
        return Response("\n".join(lines) + "\n", media_type="text/plain",
                        headers={"X-TLE-Omitted-Count": str(omitted)})

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health(request: Request):
        runtime = _services(request)
        providers = runtime.provider_health()
        core_ok = all(
            provider.healthy
            for provider in providers
            if provider.enabled and provider.name != runtime.aircraft_provider.name
        )
        return HealthResponse(
            status="ok" if core_ok else "degraded",
            generated_at=datetime.now(timezone.utc),
            providers=providers,
            tle_count=len(runtime.satellites.records),
            aircraft_track_count=sum(len(value) for value in runtime.aircraft.tracks.values()),
        )

    @app.get("/events.html", response_class=HTMLResponse, include_in_schema=False)
    async def events_html(
        request: Request,
        kind: Literal["satellite", "aircraft", "all"] = Query("all", alias="type"),
        hours: int = Query(96, ge=1, le=96),
        announced_only: bool = Query(False),
    ):
        response = await events(request, kind=kind, hours=hours, announced_only=announced_only)
        runtime = _services(request)
        map_data = _campus_map_context(runtime.campus, response.events)
        return templates.TemplateResponse(
            request=request,
            name="events.html",
            context={
                "result": response,
                "weather": runtime.weather.status(),
                "campus": runtime.campus,
                "map_data": map_data,
                "aircraft_enabled": runtime.aircraft_provider.enabled,
                "generated_at_local": response.generated_at.astimezone(
                    timezone(timedelta(hours=8))
                ),
            },
        )

    return app


def _services(request: Request) -> AppServices:
    return request.app.state.services


def _campus_map_context(campus, events) -> dict:
    min_lon, min_lat, max_lon, max_lat = campus.bounds
    width = 800.0
    height = 560.0
    padding = 30.0
    lon_scale = math.cos(math.radians(campus.center.latitude_deg))
    aspect = ((max_lon - min_lon) * lon_scale) / max(1e-12, max_lat - min_lat)
    if aspect >= 1:
        draw_width = width - 2 * padding
        draw_height = draw_width / aspect
        x0 = padding
        y0 = (height - draw_height) / 2
    else:
        draw_height = height - 2 * padding
        draw_width = draw_height * aspect
        x0 = (width - draw_width) / 2
        y0 = padding

    def project(longitude: float, latitude: float) -> tuple[float, float]:
        x = x0 + (longitude - min_lon) / (max_lon - min_lon) * draw_width
        y = y0 + (max_lat - latitude) / (max_lat - min_lat) * draw_height
        return x, y

    polygons = []
    for ring in campus.rings:
        points = " ".join(
            f"{x:.1f},{y:.1f}"
            for x, y in (project(longitude, latitude) for longitude, latitude in ring)
        )
        polygons.append(points)
    markers = []
    for event in events:
        x, y = project(event.observer.longitude, event.observer.latitude)
        markers.append(
            {
                "x": round(x, 1),
                "y": round(y, 1),
                "label": f"{event.name} / {event.target.value}",
            }
        )
    return {
        "width": width,
        "height": height,
        "polygons": polygons,
        "markers": markers,
        "bounds": campus.bounds,
    }


app = create_app()
