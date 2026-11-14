import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from app.first_screen import build_first_screen_files


START = datetime(2026, 11, 10, tzinfo=timezone.utc)


def fixture(count=120):
    events = []
    for index in range(count):
        instant = (START + timedelta(minutes=index * 30 + 1, microseconds=123456)).isoformat()
        events.append({"identifier": str(25544 + index), "target": "moon", "name": f"Satellite {index}",
                       "minimum_time": instant, "start_time": instant, "end_time": instant,
                       "observer": {"latitude": 31.0280787654321, "longitude": 121.4283987654321},
                       "altitude_deg": 15.000000001 if index % 3 else 14.999999999,
                       "weather": {"decision": "unavailable", "score": 0.123456789},
                       "duration_ms": 1, "range_km": 400.123456789, "visibility": None})
    metadata = {"schema_version": 1, "generated_at": START.isoformat(), "from_time": START.isoformat(),
                "to_time": (START + timedelta(hours=96)).isoformat(), "event_count": count,
                "catalog_sha256": "a" * 64, "catalog_source": "test", "catalog_count": 16000,
                "catalog_fetched_at": (START - timedelta(hours=1)).isoformat(),
                "warnings": [], "weather_status": {"stale": False}}
    payload = {**metadata, "events": events}
    manifest = {**metadata, "files": {"events.json": hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
                                       "weather.json": "b" * 64, "campus.geojson": "c" * 64}}
    weather = {"latitude": 31, "longitude": 121,
               "deterministic": {"fetched_at": START.isoformat(), "minutely_15": {"large": "not needed"},
                                 "hourly": {
                                     "times": [(START + timedelta(hours=h)).isoformat() for h in range(97)],
                                     "weather_code": [0] * 97, "cloud_cover_pct": [75] * 97,
                                     "precipitation_mm": [0] * 97}}, "ensemble": {"large": "not needed"}}
    campus = {"type": "FeatureCollection", "features": []}
    return payload, weather, campus, manifest


def documents(files):
    index = json.loads(files["first-screen.json"])
    return index, [json.loads(files[entry["filename"]]) for entry in index["packets"]]


def ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def test_every_expiry_window_has_exactly_the_next_first_page_without_mutating_science():
    args = fixture()
    original = copy.deepcopy(args)
    files = build_first_screen_files(*args)
    index, packets = documents(files)
    assert args == original
    assert files == build_first_screen_files(*args)
    expected = [event for event in args[0]["events"] if event["altitude_deg"] >= 15]
    assert len(packets) == len(expected) + 1
    assert index["packets"][0]["valid_from"] == packets[0]["valid_from"]
    assert ms(packets[-1]["valid_until"]) == ms(args[0]["to_time"])
    for offset, (entry, packet) in enumerate(zip(index["packets"], packets)):
        assert entry["sha256"] == hashlib.sha256(files[entry["filename"]]).hexdigest()
        assert "/" not in entry["filename"]
        assert packet["payload"]["events"] == expected[offset:offset + 10]
        assert len(packet["payload"]["events"]) <= 10
        assert entry["count"] == packet["default_event_count"] == len(expected) - offset
        assert packet["payload"]["event_count"] == len(packet["payload"]["events"])
        assert packet["manifest"] == args[3]
        assert packet["source_sha256"] == index["source_sha256"] == args[3]["files"]["events.json"]
        assert ms(packet["valid_from"]) < ms(packet["valid_until"])
        if offset:
            assert packet["valid_from"] == packets[offset - 1]["valid_until"]
        assert "ensemble" not in packet["weather"]
        assert "minutely_15" not in packet["weather"]["deterministic"]
        assert len(packet["weather"]["deterministic"]["hourly"]["times"]) <= 10
    assert packets[-1]["payload"]["events"] == []
    # A visit several days after generation still gets that time's real first page.
    late = next(packet for packet in packets if ms(packet["valid_from"]) >= ms((START + timedelta(hours=48)).isoformat()))
    assert late["payload"]["events"][0] not in packets[0]["payload"]["events"]


def test_events_in_the_same_javascript_millisecond_expire_together_without_rounding_values():
    payload, weather, campus, manifest = fixture(3)
    payload["events"][0]["altitude_deg"] = 15
    for event, microsecond in zip(payload["events"], (123001, 123456, 123999)):
        event["minimum_time"] = (START + timedelta(minutes=1, microseconds=microsecond)).isoformat()
    index, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert len(packets) == 2
    assert index["packets"][1]["count"] == 0
    assert packets[0]["payload"]["events"] == payload["events"]
    assert packets[0]["valid_until"] == "2026-11-10T00:01:00.123Z"


@pytest.mark.parametrize("deterministic", [None, {"hourly": {"times": []}}])
def test_missing_weather_yields_an_explicit_empty_default_page(deterministic):
    payload, weather, campus, manifest = fixture()
    weather["deterministic"] = deterministic
    index, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert len(packets) == 1
    assert packets[0]["payload"]["events"] == []
    assert index["packets"][0]["count"] == 0


@pytest.mark.parametrize("code,cloud,clear", [(0, 99, True), (1, 99, True), (2, 0, False),
                                              (3, 0, False), (45, 0, False), (48, 0, False),
                                              (51, 0, False), (99, 0, False),
                                              (None, 20, True), (None, 20.001, False),
                                              (None, None, False), (True, None, False)])
def test_default_weather_matches_the_ui_condition_instead_of_announcement_score(code, cloud, clear):
    payload, weather, campus, manifest = fixture(1)
    payload["events"][0]["altitude_deg"] = 15
    weather["deterministic"]["hourly"]["weather_code"] = [code] * 97
    weather["deterministic"]["hourly"]["cloud_cover_pct"] = [cloud] * 97
    _, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert bool(packets[0]["payload"]["events"]) is clear


def test_nearest_hour_tie_uses_the_earlier_weather_and_missing_coverage_is_unavailable():
    payload, weather, campus, manifest = fixture(1)
    event = payload["events"][0]
    event["altitude_deg"] = 15
    event["minimum_time"] = (START + timedelta(minutes=30)).isoformat()
    weather["deterministic"]["hourly"]["weather_code"][1] = 3
    _, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert packets[0]["payload"]["events"] == [event]
    event["minimum_time"] = (START + timedelta(minutes=30, milliseconds=1)).isoformat()
    _, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert packets[0]["payload"]["events"] == []
    weather["deterministic"]["hourly"]["times"] = [START.isoformat()]
    event["minimum_time"] = (START + timedelta(hours=1)).isoformat()
    _, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert packets[0]["payload"]["events"] == []


def test_stale_first_screen_manifest_entries_are_not_copied_into_packet_metadata():
    payload, weather, campus, manifest = fixture(2)
    manifest["files"].update({"first-screen.json": "d" * 64, "first-screen-old.json": "e" * 64})
    _, packets = documents(build_first_screen_files(payload, weather, campus, manifest))
    assert "first-screen.json" not in packets[0]["manifest"]["files"]
    assert "first-screen-old.json" not in packets[0]["manifest"]["files"]
