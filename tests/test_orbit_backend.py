from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import json

import httpx
import numpy as np
import pytest
from skyfield.api import EarthSatellite, wgs84

from app.astronomy import CelestialCalculator
from app.config import Settings
from app.geo import Geodetic, angular_separation_arcsec
from app.providers.celestrak import CelestrakProvider, CatalogUnchanged, TLEEntry, parse_omm_elements
from app.runtime import build_runtime
from app.store import DatabaseStore
from app.export import export_snapshot
from app.models import TransitEvent, Target
from app.satellites import SatelliteService


LINE1 = "1 25544U 98067A   20344.91667824  .00001264  00000-0  29621-4 0  9993"
LINE2 = "2 25544  51.6431 211.2001 0001731  65.5605  39.9047 15.49399730256344"
EPOCH = datetime(2020, 12, 9, 22, 0, 1, tzinfo=timezone.utc)


def entry():
    return TLEEntry("ISS", 25544, LINE1, LINE2, EPOCH)


def test_position_matches_independent_skyfield_topocentric(tmp_path):
    calculator = CelestialCalculator(tmp_path / "missing.bsp")
    satrec = entry().to_satrec()
    observer = Geodetic(31.02807, 121.42839)
    satellite = EarthSatellite.from_satrec(satrec, calculator.ts)
    site = wgs84.latlon(observer.latitude_deg, observer.longitude_deg)
    times = [EPOCH + timedelta(minutes=offset) for offset in (0, 10, 30, 60)]
    vectors, ranges = calculator.satellite_vectors(satrec, observer, times)
    for index, value in enumerate(times):
        expected_alt, expected_az, expected_range = (satellite - site).at(calculator.time(value)).altaz()
        actual = calculator.satellite_position(satrec, observer, value)
        assert actual.altitude_deg == pytest.approx(expected_alt.degrees, abs=0.001)
        assert actual.azimuth_deg == pytest.approx(expected_az.degrees, abs=0.001)
        assert actual.range_m == pytest.approx(expected_range.m, abs=10)
        assert ranges[index] == pytest.approx(actual.range_m, abs=1e-5)
        assert vectors[index] == pytest.approx(actual.vector_enu, abs=1e-10)


def test_sunlight_and_shadow_are_exported_without_guessing_magnitude(tmp_path):
    calculator = CelestialCalculator(tmp_path / "missing.bsp")
    satrec = entry().to_satrec()
    assert calculator.satellite_sunlit(satrec, EPOCH) is None
    kernel = Path("~/.cache/sjtu-transit/de421.bsp").expanduser()
    if not kernel.exists():
        pytest.skip("DE421 not available locally")
    calculator = CelestialCalculator(kernel)
    # Across a complete low Earth orbit there must be both illumination states.
    states = {calculator.satellite_sunlit(satrec, EPOCH + timedelta(minutes=m)) for m in range(0, 100, 5)}
    assert states == {True, False}


def test_omm_six_digit_id_and_database_roundtrip(tmp_path):
    fields = entry().as_omm()
    fields["NORAD_CAT_ID"] = 100001
    fields["EPOCH"] = "2026-11-04T01:02:03Z"
    records = parse_omm_elements([fields])
    assert records[0].to_satrec().satnum == 100001
    store = DatabaseStore(tmp_path / "cache.db")
    store.save_tles(records, datetime(2026, 11, 4, tzinfo=timezone.utc))
    restored = store.load_tles(0, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert restored[0].as_omm() == records[0].as_omm()
    assert restored[0].to_satrec().satnum == 100001
    store.close()


@pytest.mark.parametrize("payload", [[], {}, [{"NORAD_CAT_ID": 1}], "<html>error</html>"])
def test_bad_catalog_does_not_silently_replace_good_data(payload):
    with pytest.raises((ValueError, KeyError)):
        parse_omm_elements(payload)


def test_primary_mirror_cache_and_unchanged_provenance(tmp_path):
    settings = Settings(database_path=tmp_path / "cache.db", ephemeris_path=tmp_path / "missing.bsp")
    runtime = build_runtime(settings)
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=[entry().as_omm()])
    transport = httpx.MockTransport(handler)
    runtime.satellites.provider = CelestrakProvider("https://primary.test", transport=transport)
    runtime.satellites.fallback_provider = CelestrakProvider("https://mirror.test", transport=transport)
    asyncio.run(runtime.satellites.refresh_catalog())
    assert calls == ["primary.test", "mirror.test"]
    assert runtime.satellites.catalog_source == "community-mirror"
    first_download = runtime.satellites.updated_at
    asyncio.run(runtime.satellites.refresh_catalog())
    assert len(calls) == 2  # persisted refresh interval, no repeated requests
    runtime.store.close()
    runtime = build_runtime(settings)
    assert runtime.satellites.updated_at == first_download
    runtime.store.set_cache("orbit_primary_attempt", True, datetime(2000, 1, 1, tzinfo=timezone.utc))
    runtime.satellites.provider = CelestrakProvider("https://primary.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(403, text="GP data has not updated since your last successful download")))
    asyncio.run(runtime.satellites.refresh_catalog())
    assert runtime.satellites.updated_at == first_download
    assert runtime.satellites.checked_at >= first_download
    assert runtime.satellites.records
    # A later outage of both sources must retain the last valid catalog.
    runtime.store.set_cache("orbit_primary_attempt", True, datetime(2000, 1, 1, tzinfo=timezone.utc))
    failing = httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
    runtime.satellites.provider = CelestrakProvider("https://primary.test", transport=failing)
    runtime.satellites.fallback_provider = CelestrakProvider("https://mirror.test", transport=failing)
    asyncio.run(runtime.satellites.refresh_catalog())
    assert runtime.satellites.records[0].entry.norad_id == 25544
    assert runtime.satellites.updated_at == first_download
    assert runtime.satellites.last_error
    runtime.store.close()


def test_redirect_and_empty_catalog_are_errors():
    for response in (httpx.Response(301, headers={"Location": "https://other.test"}),
                     httpx.Response(200, text="[]")):
        provider = CelestrakProvider("https://primary.test", transport=httpx.MockTransport(lambda request: response))
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            asyncio.run(provider.fetch_active())


def test_publication_timestamp_skips_download_until_updated(tmp_path):
    calls = []
    published = ["2026 Nov 04 12:23:24"]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/index":
            return httpx.Response(200, text=f"<h3>Current as of {published[0]} UTC (Day 308)</h3>")
        return httpx.Response(200, json=[entry().as_omm()])
    settings = Settings(database_path=tmp_path / "cache.db", ephemeris_path=tmp_path / "missing.bsp")
    runtime = build_runtime(settings)
    provider = CelestrakProvider("https://primary.test/gp", update_url="https://primary.test/index",
                                 transport=httpx.MockTransport(handler))
    runtime.satellites.provider = provider
    asyncio.run(runtime.satellites.refresh_catalog())
    original_download = runtime.satellites.updated_at
    assert calls == ["/index", "/gp"]
    runtime.store.close()
    # A fresh process restores the publication marker along with the catalog.
    runtime = build_runtime(settings)
    assert runtime.satellites.provider.known_update_at == datetime(2026, 11, 4, 12, 23, 24, tzinfo=timezone.utc)
    provider.known_update_at = runtime.satellites.provider.known_update_at
    runtime.satellites.provider = provider
    runtime.store.set_cache("orbit_primary_attempt", True, EPOCH)
    asyncio.run(runtime.satellites.refresh_catalog())
    assert calls == ["/index", "/gp", "/index"]
    assert runtime.satellites.updated_at == original_download
    published[0] = "2026 Nov 04 14:23:38"
    runtime.store.set_cache("orbit_primary_attempt", True, EPOCH)
    asyncio.run(runtime.satellites.refresh_catalog())
    assert calls[-2:] == ["/index", "/gp"]
    assert runtime.store.get_cache("orbit_primary_update")[0] == "2026-11-04T14:23:38+00:00"
    runtime.store.close()


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, text="changed layout")])
def test_failed_publication_probe_never_downloads_catalog(response):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return response
    provider = CelestrakProvider("https://primary.test/gp", update_url="https://primary.test/index",
                                 transport=httpx.MockTransport(handler))
    with pytest.raises((httpx.HTTPStatusError, ValueError)):
        asyncio.run(provider.fetch_active())
    assert calls == ["/index"]
    assert provider.known_update_at is None


def test_stale_mirror_never_rolls_back_newer_cached_orbit(tmp_path):
    runtime = build_runtime(Settings(database_path=tmp_path / "cache.db", ephemeris_path=tmp_path / "missing.bsp"))
    fresh = entry().as_omm()
    fresh["EPOCH"] = "2026-11-04T00:00:00.000000"
    runtime.store.save_tles(parse_omm_elements([fresh]), EPOCH)
    runtime.satellites._restore_cached_catalog()
    runtime.satellites.provider = CelestrakProvider("https://primary.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(503)))
    runtime.satellites.fallback_provider = CelestrakProvider("https://mirror.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[entry().as_omm()])))
    asyncio.run(runtime.satellites.refresh_catalog())
    assert runtime.satellites.records[0].entry.epoch.year == 2026
    assert runtime.satellites.updated_at == EPOCH
    runtime.store.close()


def test_old_epoch_is_not_excluded_from_geometry(tmp_path, monkeypatch):
    runtime = build_runtime(Settings(database_path=tmp_path / "cache.db", ephemeris_path=tmp_path / "missing.bsp"))
    runtime.store.save_tles([entry()], EPOCH)
    runtime.satellites._restore_cached_catalog()
    now = EPOCH + timedelta(days=3)
    visited = []
    def candidates(record, target, observer, times, *args, **kwargs):
        visited.append(record.entry.norad_id)
        return []
    monkeypatch.setattr(runtime.satellites, "_candidate_windows", candidates)
    monkeypatch.setattr(runtime.astronomy, "targets_at", lambda target, observer, times:
                        tuple(np.full(len(times), value) for value in (180., 45., 1e11, 900.)))
    runtime.satellites._compute_events(now, now + timedelta(minutes=1), now, runtime.satellites.records)
    assert visited == [25544, 25544]
    runtime.store.close()


def test_export_96_hours_keeps_weather_unavailable_events_and_reuses_geometry(tmp_path, monkeypatch):
    import hashlib
    import gzip
    now = EPOCH.replace(hour=0, minute=0, second=0)
    settings = Settings(database_path=tmp_path / "cache.db", ephemeris_path=tmp_path / "missing.bsp")
    store = DatabaseStore(settings.database_path)
    six_digit = entry().as_omm()
    six_digit["NORAD_CAT_ID"] = 100001
    store.save_tles([entry(), *parse_omm_elements([six_digit])], EPOCH - timedelta(days=10))
    store.close()
    calls = []
    async def fake_events(self, start, end, now=None):
        calls.append(start)
        value = start + timedelta(hours=1)
        return [TransitEvent(
            event_type="satellite", target="moon", name="ISS", identifier="25544",
            start_time=value, minimum_time=value, end_time=value + timedelta(seconds=1),
            duration_ms=1000, min_separation_arcsec=1, target_angular_radius_arcsec=900,
            range_km=400, observer={"latitude":31.028, "longitude":121.428},
            azimuth_deg=180, altitude_deg=45, target_azimuth_deg=180, target_altitude_deg=45,
            orbit_epoch=EPOCH, confidence=0,
            weather=self.weather.evaluate("moon", value, now).evidence)]
    monkeypatch.setattr(SatelliteService, "events", fake_events)
    output = tmp_path / "output"
    first = asyncio.run(export_snapshot(settings, output, offline=True, now=now))
    assert len(calls) == 16
    assert gzip.decompress((output / "events.json.gz").read_bytes()) == (output / "events.json").read_bytes()
    payload = json.loads((output / "events.json").read_text())
    from app.event_columns import decode_snapshot
    from app.compact_events import compact_snapshot
    assert decode_snapshot((output / "events.columns.json.gz").read_bytes()) == compact_snapshot(payload)
    assert payload["hours"] == 96
    assert payload["announced_only"] is False
    assert len(payload["events"]) == 16
    assert all(event["weather"]["decision"] == "unavailable" for event in payload["events"])
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["catalog_fetched_at"] == (EPOCH - timedelta(days=10)).isoformat()
    assert manifest["catalog_count"] == 2
    assert manifest["tle_count"] == 1
    assert manifest["tle_omitted_count"] == 1
    first_screen = json.loads((output / "first-screen.json").read_bytes())
    assert first_screen["source_sha256"] == manifest["files"]["events.json"]
    # Missing deterministic weather has no default clear-sky candidates, while
    # the complete snapshot still retains every geometric event.
    assert len(first_screen["packets"]) == 1
    packet_entry = first_screen["packets"][0]
    assert packet_entry["count"] == 0
    assert packet_entry["filename"] not in manifest["files"]
    packet_bytes = (output / packet_entry["filename"]).read_bytes()
    assert hashlib.sha256(packet_bytes).hexdigest() == packet_entry["sha256"]
    assert json.loads(packet_bytes)["payload"]["events"] == []
    assert [row["NORAD_CAT_ID"] for row in json.loads((output / "orbits.json").read_text())] == [25544, 100001]
    assert len((output / "orbits.tle").read_text().splitlines()) == 3
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    second = asyncio.run(export_snapshot(settings, output, offline=True, now=now + timedelta(hours=1)))
    assert second["cache_hits"] == 16
    assert len(calls) == 17  # only the new six-hour tail needs computation
    assert second["catalog_fetched_at"] == first["catalog_fetched_at"]


def test_missing_catalog_preserves_previous_export(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "manifest.json").write_text("previous generation")
    settings = Settings(database_path=tmp_path / "cache.db", ephemeris_path=tmp_path / "missing.bsp")
    with pytest.raises(RuntimeError, match="No usable orbit catalog"):
        asyncio.run(export_snapshot(settings, output, offline=True))
    assert (output / "manifest.json").read_text() == "previous generation"


@pytest.mark.parametrize("target", [Target.SUN, Target.MOON])
@pytest.mark.parametrize("use_kernel", [False, True])
def test_window_interpolation_matches_exact_topocentric_places(tmp_path, target, use_kernel):
    path = Path("~/.cache/sjtu-transit/de421.bsp").expanduser() if use_kernel else tmp_path / "missing.bsp"
    if use_kernel and not path.exists():
        pytest.skip("Optional DE421 kernel is not installed")
    runtime = build_runtime(Settings(database_path=tmp_path / "cache.db", ephemeris_path=path))
    center_time = datetime(2026, 11, 5, 2, tzinfo=timezone.utc)
    interpolate = runtime.satellites._target_interpolator(target, center_time)
    for point in runtime.campus.grid(400)[::5]:
        observer = Geodetic(point.latitude, point.longitude)
        for offset in (-59.3, -21.7, 0.1, 34.9, 59.1):
            value = center_time + timedelta(seconds=offset)
            exact = runtime.astronomy.target_at(target.value, observer, value)
            approximate = interpolate(observer, value)
            separation = angular_separation_arcsec(exact.azimuth_deg, exact.altitude_deg,
                                                  approximate.azimuth_deg, approximate.altitude_deg)
            assert separation < 0.02
            assert approximate.angular_radius_arcsec == pytest.approx(exact.angular_radius_arcsec, abs=0.001)
    runtime.store.close()
    if runtime.astronomy.eph:
        runtime.astronomy.eph.close()


def test_daily_primary_download_limit_survives_restart(tmp_path):
    calls = []
    published = ["2026 Nov 04 12:23:24"]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/index":
            return httpx.Response(200, text=f"<h3>Current as of {published[0]} UTC (Day 308)</h3>")
        return httpx.Response(200, json=[entry().as_omm()])
    settings = Settings(
        database_path=tmp_path / "cache.db",
        ephemeris_path=tmp_path / "missing.bsp",
        celestrak_daily_download_limit=1,
    )
    runtime = build_runtime(settings)
    provider = CelestrakProvider(
        "https://primary.test/gp",
        update_url="https://primary.test/index",
        transport=httpx.MockTransport(handler),
    )
    runtime.satellites.provider = provider
    asyncio.run(runtime.satellites.refresh_catalog())
    assert calls == ["/index", "/gp"]
    today = datetime.now(timezone.utc).date().isoformat()
    assert runtime.store.get_cache("orbit_primary_downloads")[0] == {"date": today, "count": 1}
    first_download = runtime.satellites.updated_at
    runtime.store.close()

    # A fresh run must see the saved daily count and keep the cached catalog.
    published[0] = "2026 Nov 04 14:23:38"
    runtime = build_runtime(settings)
    provider.known_update_at = runtime.satellites.provider.known_update_at
    runtime.satellites.provider = provider
    runtime.store.set_cache("orbit_primary_attempt", True, EPOCH)
    asyncio.run(runtime.satellites.refresh_catalog())
    assert calls == ["/index", "/gp", "/index"]
    assert runtime.satellites.updated_at == first_download
    assert "daily download limit" in runtime.satellites.last_error

    # A new UTC day resets the budget and allows another download.
    runtime.store.set_cache("orbit_primary_downloads", {"date": "2000-01-01", "count": 99}, EPOCH)
    runtime.store.set_cache("orbit_primary_attempt", True, EPOCH)
    runtime.satellites.provider.known_update_at = datetime(2026, 11, 4, 12, 23, 24, tzinfo=timezone.utc)
    asyncio.run(runtime.satellites.refresh_catalog())
    assert calls[-2:] == ["/index", "/gp"]
    assert runtime.store.get_cache("orbit_primary_downloads")[0] == {"date": today, "count": 1}
    runtime.store.close()
