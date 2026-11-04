"""Batch backend for Pages: no web server, credentials, or aircraft polling."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import tempfile
import time
import sys

from sgp4.exporter import export_tle

from app.config import Settings
from app.models import TransitEvent
from app.services import build_services


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def prediction_fingerprint(runtime) -> str:
    """Invalidate cached geometry when data, numerical code, or settings change."""
    digest = hashlib.sha256()
    for dependency in ("sgp4", "skyfield", "numpy", "scipy"):
        digest.update(f"{dependency}={version(dependency)}".encode())
    for record in runtime.satellites.records:
        digest.update(_json(record.entry.as_omm()).encode())
    for path in (runtime.settings.campus_geojson_path, runtime.settings.satellite_sizes_path,
                 Path(__file__).with_name("satellites.py"), Path(__file__).with_name("astronomy.py"),
                 Path(__file__).with_name("geo.py"), Path(__file__).with_name("campus.py"),
                 Path(__file__).with_name("satellite_sizes.py")):
        digest.update(path.read_bytes())
    digest.update(_json({
        "step": runtime.settings.satellite_coarse_step_seconds,
        "grid": runtime.settings.campus_grid_meters,
        "refine": runtime.settings.campus_refine_meters,
        "ephemeris": runtime.astronomy.ephemeris_backend,
    }).encode())
    if runtime.astronomy.eph is not None:
        digest.update(runtime.settings.resolved_ephemeris_path.read_bytes())
    return digest.hexdigest()


async def predict_cached(runtime, start: datetime, end: datetime) -> tuple[list[TransitEvent], int]:
    fingerprint = prediction_fingerprint(runtime)
    cursor = start.replace(hour=(start.hour // 6) * 6, minute=0, second=0, microsecond=0)
    events = []
    cache_hits = 0
    while cursor < end:
        chunk_end = cursor + timedelta(hours=6)
        key = f"prediction:{fingerprint}:{cursor.isoformat()}"
        cached = runtime.store.get_cache(key)
        if cached:
            chunk = [TransitEvent.model_validate(row) for row in cached[0]]
            cache_hits += 1
        else:
            chunk_started = time.monotonic()
            chunk = await runtime.satellites.events(cursor, chunk_end, now=start)
            runtime.store.set_cache(key, [event.model_dump(mode="json") for event in chunk])
            print(f"Computed {cursor.isoformat()} to {chunk_end.isoformat()}: {len(chunk)} events in {time.monotonic() - chunk_started:.1f}s", file=sys.stderr, flush=True)
        for event in chunk:
            if start <= event.minimum_time < end:
                # Cached geometry survives weather changes. Re-score every export.
                event.weather = runtime.weather.evaluate(event.target.value, event.minimum_time, start).evidence
                event.confidence = event.weather.score
                event.reason_codes = [code for code in event.reason_codes
                                      if code in ("object_size_unknown_centerline_only", "long_orbit_extrapolation")]
                if event.orbit_epoch and (start - event.orbit_epoch).total_seconds() > runtime.settings.tle_stale_seconds:
                    event.reason_codes.append("orbit_epoch_old")
                event.reason_codes.extend(event.weather.reasons)
                events.append(event)
        cursor = chunk_end
    return sorted(events, key=lambda event: event.minimum_time), cache_hits


async def export_snapshot(settings: Settings, output: Path, *, offline: bool = False,
                          now: datetime | None = None) -> dict:
    started = time.monotonic()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    runtime = build_services(settings)
    warnings = []
    try:
        if not offline:
            await runtime.satellites.refresh_catalog()
            weather = runtime.weather
            if weather.deterministic is None or (now - weather.deterministic.fetched_at).total_seconds() >= settings.weather_refresh_seconds:
                await weather.refresh_deterministic()
                if weather.last_error:
                    warnings.append(f"weather: {weather.last_error}")
            if weather.ensemble is None or (now - weather.ensemble.fetched_at).total_seconds() >= settings.weather_ensemble_refresh_seconds:
                await weather.refresh_ensemble()
                if weather.last_error:
                    warnings.append(f"ensemble: {weather.last_error}")
        if not runtime.satellites.records:
            raise RuntimeError(f"No usable orbit catalog; previous export preserved: {runtime.satellites.last_error}")
        if runtime.satellites.last_error:
            warnings.append(runtime.satellites.last_error)
        if runtime.satellites.catalog_is_stale(now):
            warnings.append("orbit_download_stale_using_last_valid_snapshot")
        if runtime.astronomy.eph is None:
            warnings.append("analytical_ephemeris_lower_accuracy")
        end = now + timedelta(hours=settings.satellite_hours)
        events, cache_hits = await predict_cached(runtime, now, end)
        omm_rows = [record.entry.as_omm() for record in runtime.satellites.records]
        catalog_hash = hashlib.sha256(_json(omm_rows).encode()).hexdigest()
        tles = []
        for record in runtime.satellites.records:
            if record.entry.norad_id <= 99999:
                line1, line2 = export_tle(record.satrec)
                tles.extend((record.entry.name, line1, line2))
        metadata = {
            "schema_version": 1,
            "generated_at": now.isoformat(),
            "from_time": now.isoformat(), "to_time": end.isoformat(),
            "hours": settings.satellite_hours,
            "catalog_source": runtime.satellites.catalog_source,
            "catalog_fetched_at": runtime.satellites.updated_at.isoformat() if runtime.satellites.updated_at else None,
            "catalog_checked_at": runtime.satellites.checked_at.isoformat() if runtime.satellites.checked_at else None,
            "primary_upstream_updated_at": runtime.satellites.provider.known_update_at.isoformat() if runtime.satellites.provider.known_update_at else None,
            "catalog_sha256": catalog_hash,
            "catalog_count": len(omm_rows), "event_count": len(events),
            "tle_count": len(tles) // 3, "tle_omitted_count": len(omm_rows) - len(tles) // 3,
            "ephemeris": runtime.astronomy.ephemeris_backend,
            "weather_status": runtime.weather.status(now).model_dump(mode="json"),
            "warnings": warnings,
        }
        files = {
            "events.json": _json({**metadata, "announced_only": False,
                                   "events": [event.model_dump(mode="json") for event in events]}),
            "orbits.json": _json(omm_rows),
            "orbits.tle": "\n".join(tles) + "\n",
            "weather.json": _json(runtime.weather.snapshot()),
            "campus.geojson": settings.campus_geojson_path.read_text(),
        }
        metadata["files"] = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in files.items()}
        files["manifest.json"] = _json(metadata)
        # Stage the complete generation before touching any existing export.
        output = output.resolve()
        if output == settings.database_path.resolve().parent or output in settings.database_path.resolve().parents:
            raise ValueError("Export directory must not contain the database")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".transit-export-", dir=output.parent) as staging:
            stage = Path(staging)
            for name, content in files.items():
                (stage / name).write_text(content, encoding="utf-8")
            output.mkdir(parents=True, exist_ok=True)
            # Commit manifest last; readers can check each file's digest.
            for name in files:
                (stage / name).replace(output / name)
        runtime.store.prune_cache("prediction:", now - timedelta(days=7))
        return {**metadata, "cache_hits": cache_hits, "elapsed_seconds": round(time.monotonic() - started, 3)}
    finally:
        runtime.store.close()
        if runtime.astronomy.eph is not None:
            runtime.astronomy.eph.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Export satellite Sun/Moon transits as static JSON")
    parser.add_argument("--output", type=Path, default=Path("dist/data"))
    parser.add_argument("--database", type=Path, default=Path(".state/transit.db"))
    parser.add_argument("--hours", type=int, choices=range(1, 97), metavar="1..96", default=96)
    parser.add_argument("--offline", action="store_true", help="Use stored orbit/weather snapshots without network requests")
    args = parser.parse_args(argv)
    settings = Settings(database_path=args.database, satellite_hours=args.hours)
    result = asyncio.run(export_snapshot(settings, args.output, offline=args.offline))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
