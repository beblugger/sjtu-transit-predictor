import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.first_screen import build_first_screen_files
from scripts.build_frontend import build_frontend


START = datetime(2026, 11, 10, tzinfo=timezone.utc)
SOURCE = Path(__file__).resolve().parents[1] / "deploy"


def snapshot():
    events = []
    for index in range(12):
        instant = (START + timedelta(minutes=index + 1)).isoformat()
        events.append({"identifier": str(25544 + index), "target": "moon", "name": f"Satellite {index}",
                       "minimum_time": instant, "start_time": instant, "end_time": instant,
                       "observer": {"latitude": 31.0, "longitude": 121.0}, "altitude_deg": 30,
                       "weather": {"decision": "unavailable", "score": 0}, "duration_ms": 1,
                       "range_km": 400, "visibility": None})
    metadata = {"schema_version": 1, "generated_at": START.isoformat(), "from_time": START.isoformat(),
                "to_time": (START + timedelta(hours=1)).isoformat(), "event_count": len(events),
                "catalog_sha256": "a" * 64, "catalog_source": "test", "catalog_count": 1,
                "catalog_fetched_at": START.isoformat(), "warnings": [], "weather_status": {}}
    payload = {**metadata, "events": events}
    weather = {"latitude": 31, "longitude": 121, "deterministic": {"fetched_at": START.isoformat(), "hourly": {
        "times": [START.isoformat(), (START + timedelta(hours=1)).isoformat()],
        "weather_code": [0, 0], "cloud_cover_pct": [0, 0], "precipitation_mm": [0, 0]}}}
    campus = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
              "geometry": {"type": "Polygon", "coordinates": [[[121, 31], [122, 31], [122, 32], [121, 31]]]}}]}
    files = {"events.json": json.dumps(payload, separators=(",", ":")).encode(),
             "weather.json": json.dumps(weather, separators=(",", ":")).encode(),
             "campus.geojson": json.dumps(campus, separators=(",", ":")).encode()}
    manifest = {**metadata, "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    return payload, weather, campus, manifest, files


def write_data(path, payload, weather, campus, manifest, files, first_screen=True):
    path.mkdir(parents=True)
    for name, data in files.items():
        (path / name).write_bytes(data)
    if first_screen:
        generated = build_first_screen_files(payload, weather, campus, manifest)
        for name, data in generated.items():
            (path / name).write_bytes(data)
        manifest = {**manifest, "files": {**manifest["files"],
                  "first-screen.json": hashlib.sha256(generated["first-screen.json"]).hexdigest()}}
    (path / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
    return manifest


def test_build_inlines_one_verified_first_page_and_prefetches_expired_packets(tmp_path):
    payload, weather, campus, manifest, files = snapshot()
    data, output = tmp_path / "data", tmp_path / "site"
    write_data(data, payload, weather, campus, manifest, files)
    result = build_frontend(SOURCE, data, output)
    html = (output / "index.html").read_text()
    assert result["embedded_first_screen"] is True
    assert result["inline_events"] == 10
    assert result["packets"] == 13
    assert '<script type="module" src="app.mjs"></script>' not in html
    assert "window.__firstScreenData={" in html
    assert "window.__firstScreenPacketRequest" in html
    assert "Satellite 11" not in html, "later events must not be inlined"
    assert '<link rel="stylesheet" href="styles.css">' not in html
    assert (output / "app.mjs").exists(), "source modules remain available for explicit fallback"


def test_build_falls_back_for_old_snapshots_without_first_screen_index(tmp_path):
    payload, weather, campus, manifest, files = snapshot()
    data, output = tmp_path / "data", tmp_path / "site"
    write_data(data, payload, weather, campus, manifest, files, first_screen=False)
    result = build_frontend(SOURCE, data, output)
    html = (output / "index.html").read_text()
    assert result["embedded_first_screen"] is False
    assert result["inline_events"] == 0
    assert result["packets"] == 0
    assert "window.__firstScreenData=" not in html
    assert "window.__firstScreenData.packet=" not in html
    assert '<script type="module">' in html
