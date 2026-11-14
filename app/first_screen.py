"""Export only the default first page for each future event expiry boundary.

The tiny index and the generation-time packet can be embedded in the HTML.
Later visits select one packet using that index, without fetching all events.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import sys

from app.compact_events import compact_snapshot


PAGE_SIZE = 10
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WEATHER_FIELDS = ("times", "cloud_cover_pct", "precipitation_mm", "weather_code")


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _milliseconds(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Snapshot times must include a UTC offset")
    delta = instant.astimezone(timezone.utc) - _EPOCH
    # Match Date.parse's millisecond precision without a float timestamp roundtrip.
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _iso(milliseconds: int) -> str:
    return (_EPOCH + timedelta(milliseconds=milliseconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def _at(series: dict, key: str, index: int):
    values = series.get(key, [])
    return values[index] if index < len(values) else None


def _weather_index(times: list[int], instant: int) -> int | None:
    insertion = bisect_left(times, instant)
    candidates = [index for index in (insertion - 1, insertion) if 0 <= index < len(times)]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda index: abs(times[index] - instant))
    return nearest if abs(times[nearest] - instant) <= 1_800_000 else None


def _clear(series: dict, index: int | None) -> bool:
    """Match presentation.mjs weatherCondition, including nearest-hour ties."""
    if index is None:
        return False
    code, cloud = _at(series, "weather_code", index), _at(series, "cloud_cover_pct", index)
    if _number(code) and code in (0, 1):
        return True
    if _number(code) and (code in (2, 3, 45, 48) or code >= 51):
        return False
    return _number(cloud) and cloud <= 20


def _packet_weather(weather: dict, series: dict, indices: list[int]) -> dict:
    deterministic = weather.get("deterministic")
    if deterministic is None:
        return {"latitude": weather.get("latitude"), "longitude": weather.get("longitude"),
                "deterministic": None}
    return {"latitude": weather.get("latitude"), "longitude": weather.get("longitude"),
            "deterministic": {"fetched_at": deterministic.get("fetched_at"),
                              "hourly": {key: [_at(series, key, index) for index in indices]
                                         for key in _WEATHER_FIELDS}}}


def build_first_screen_files(payload: dict, weather: dict, campus: dict, manifest: dict) -> dict[str, bytes]:
    """Build a bounded first page at every default candidate's expiry boundary.

    Packets contain exactly the next ten clear-weather events at altitude >=15°,
    with original event precision and enough visibility for the default map.
    Their digest appears only in the index; the outer manifest hashes the index.
    """
    source_sha256 = manifest.get("files", {}).get("events.json", "")
    if not re.fullmatch("[a-f0-9]{64}", source_sha256):
        raise ValueError("First-screen data requires the full event snapshot SHA256")
    if any(payload.get(key) != manifest.get(key) for key in
           ("generated_at", "from_time", "to_time", "catalog_sha256")):
        raise ValueError("First-screen payload and manifest versions differ")
    start, end = _milliseconds(manifest["from_time"]), _milliseconds(manifest["to_time"])
    if start >= end:
        raise ValueError("First-screen coverage must have a positive duration")
    series = (weather.get("deterministic") or {}).get("hourly") or {}
    weather_times = [_milliseconds(value) for value in series.get("times", [])]
    if weather_times != sorted(weather_times):
        raise ValueError("Hourly weather times must be chronological")
    candidates = []
    weather_indices = {}
    for event in payload["events"]:
        instant = _milliseconds(event["minimum_time"])
        altitude = event.get("altitude_deg")
        index = _weather_index(weather_times, instant)
        if start < instant < end and _number(altitude) and altitude >= 15 and _clear(series, index):
            candidates.append((instant, event))
            weather_indices[instant] = index
    candidates.sort(key=lambda entry: entry[0])
    event_times = [entry[0] for entry in candidates]
    metadata = {key: copy.deepcopy(value) for key, value in payload.items() if key != "events"}
    compact = compact_snapshot({"events": [entry[1] for entry in candidates]})["events"]
    source_manifest = copy.deepcopy(manifest)
    source_manifest["files"] = {name: digest for name, digest in manifest["files"].items()
                                if name != "first-screen.json" and not name.startswith("first-screen-")}
    packets, files = [], {}
    valid_from, offset = start, 0
    while valid_from < end:
        valid_until = event_times[offset] if offset < len(event_times) else end
        events = compact[offset:offset + PAGE_SIZE]
        indices = sorted({weather_indices[instant] for instant in event_times[offset:offset + PAGE_SIZE]})
        packet = {
            "format": "first-screen-v1", "valid_from": _iso(valid_from), "valid_until": _iso(valid_until),
            "source_sha256": source_sha256, "default_event_count": len(compact) - offset,
            "manifest": source_manifest,
            "payload": {**metadata, "event_count": len(events),
                        "visibility_event_count": sum(event.get("visibility") is not None for event in events),
                        "events": events},
            "campus": campus, "weather": _packet_weather(weather, series, indices),
        }
        filename = f"first-screen-{source_sha256[:12]}-{len(packets):05d}.json"
        content = _json(packet)
        files[filename] = content
        packets.append({"valid_from": packet["valid_from"], "valid_until": packet["valid_until"],
                        "filename": filename, "sha256": hashlib.sha256(content).hexdigest(),
                        "count": packet["default_event_count"]})
        valid_from = valid_until
        offset = bisect_right(event_times, valid_from)
    index = {"format": "first-screen-index-v1", "generated_at": manifest["generated_at"],
             "from_time": manifest["from_time"], "to_time": manifest["to_time"],
             "source_sha256": source_sha256, "packets": packets}
    files["first-screen.json"] = _json(index)
    return files


def main() -> None:
    """Read a fixture on stdin; emit filename -> JSON text for an in-memory server."""
    fixture = json.load(sys.stdin.buffer)
    files = build_first_screen_files(fixture["payload"], fixture["weather"], fixture["campus"], fixture["manifest"])
    sys.stdout.buffer.write(_json({name: value.decode("utf-8") for name, value in files.items()}))


if __name__ == "__main__":
    main()
