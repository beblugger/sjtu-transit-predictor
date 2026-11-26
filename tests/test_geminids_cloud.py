from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.geminids_cloud import (
    DET_MODELS,
    ENS_MODELS,
    SITES,
    ModelSpec,
    Series,
    best_window,
    build,
    consensus_scores,
    fetch_site,
    night_slots,
    probability_below,
    star_score,
    weighted_median,
)

SLOTS = night_slots("2026-12-12")


def _det_payload(values: dict[str, float], last_stamp: str = "2026-12-12T23:00") -> dict:
    times = [f"2026-12-11T{h:02d}:00" for h in range(24)]
    times += [f"2026-12-12T{h:02d}:00" for h in range(24)]
    hourly: dict[str, list] = {"time": times}
    for spec in DET_MODELS:
        hourly[f"cloud_cover_{spec.key}"] = [_bucket(values, stamp) for stamp in times]
        hourly[f"cloud_cover_low_{spec.key}"] = [5.0 for _ in times]
        hourly[f"cloud_cover_mid_{spec.key}"] = [5.0 for _ in times]
        hourly[f"cloud_cover_high_{spec.key}"] = [10.0 for _ in times]
    hourly["time"] = [stamp if stamp <= last_stamp else stamp for stamp in times]
    return {"hourly": hourly}


def _bucket(values: dict[str, float], stamp: str) -> float | None:
    hour = stamp[11:13]
    if stamp.startswith("2026-12-12") and f"2026-12-12T{hour}:00" in values:
        return values[f"2026-12-12T{hour}:00"]
    return None


def _ens_payload() -> dict:
    times = [f"2026-12-12T{h:02d}:00" for h in range(24)]
    hourly: dict[str, list] = {"time": times}
    for spec in ENS_MODELS:
        key = spec.key
        for member in range(spec.members):
            name = f"cloud_cover_member{member:02d}_{key}"
            hourly[name] = [100.0 if member % 2 else 10.0 for _ in times]
    return {"hourly": hourly}


def _mock_transport(values: dict[str, float] | None = None) -> httpx.MockTransport:
    payload_det = _det_payload(values if values is not None else {
        f"2026-12-12T{h:02d}:00": 20.0 for h in range(18, 24)})
    payload_ens = _ens_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        if "ensemble" in str(request.url):
            return httpx.Response(200, json=payload_ens)
        return httpx.Response(200, json=payload_det)

    return httpx.MockTransport(handler)


def test_night_slots_crosses_midnight():
    slots = night_slots("2026-12-12")
    assert len(slots) == 12
    assert slots[0] == "2026-12-12T18:00"
    assert slots[5] == "2026-12-12T23:00"
    assert slots[6] == "2026-12-13T00:00"
    assert slots[-1] == "2026-12-13T05:00"


def test_weighted_median_respects_weights():
    assert weighted_median([(10.0, 1.0), (90.0, 1.0)]) == 10.0
    assert weighted_median([(10.0, 3.0), (90.0, 1.0)]) == 10.0
    assert weighted_median([(10.0, 1.0), (90.0, 3.0)]) == 90.0
    assert weighted_median([]) is None


def test_star_score_bounds_and_low_cloud_penalty():
    assert star_score(0.0, 0.0, 0.0) == pytest.approx(100.0)
    assert star_score(100.0, 100.0, 100.0) == pytest.approx(0.0)
    clear_high = star_score(40.0, 5.0, 90.0)
    low_cloud = star_score(40.0, 90.0, 5.0)
    assert low_cloud < clear_high


def test_consensus_decays_with_lead_time():
    series = [Series(spec, "det", total={SLOTS[0]: 10.0}) for spec in DET_MODELS]
    near, near_scores = consensus_scores(series, SLOTS[:1], "2026-12-12",
                                         dt.date(2026, 12, 11))
    far, _ = consensus_scores(series, SLOTS[:1], "2026-12-12", dt.date(2026, 11, 1))
    assert near[SLOTS[0]] == far[SLOTS[0]]
    assert near_scores[SLOTS[0]] > 70


def test_consensus_skips_sources_without_target_night():
    active = Series(ModelSpec("ecmwf_ifs025", "ECMWF IFS", "ECMWF", 1.0), "det",
                    total={slot: 30.0 for slot in SLOTS},
                    low={slot: 10.0 for slot in SLOTS},
                    high={slot: 20.0 for slot in SLOTS})
    stale = Series(ModelSpec("gfs_seamless", "GFS", "NOAA", 0.9), "det",
                   error="目标夜已超出时效")
    consensus, scores = consensus_scores([active, stale], SLOTS, "2026-12-12",
                                         dt.date(2026, 12, 11))
    assert set(consensus) == set(SLOTS)
    assert all(value == 30.0 for value in consensus.values())
    assert len(scores) == len(SLOTS)


def test_best_window_requires_complete_three_hour_block():
    scores = {slot: 10.0 for slot in SLOTS}
    for slot in SLOTS[8:11]:
        scores[slot] = 100.0
    window, value = best_window(SLOTS, scores)
    assert window == (SLOTS[8], SLOTS[10])
    assert value == pytest.approx(100.0)
    assert best_window(SLOTS, {})[0] is None


def test_probability_below_uses_members():
    series = Series(ModelSpec("gem_global", "GEM-ENS", "ECCC", members=4), "ens",
                    members={SLOTS[0]: [0.0, 10.0, 80.0, 90.0]})
    assert probability_below(series, SLOTS[0], 30.0) == pytest.approx(50.0)
    assert probability_below(series, SLOTS[1], 30.0) is None


def test_fetch_marks_out_of_horizon_sources_instead_of_dropping_them():
    with httpx.Client(transport=_mock_transport(), base_url="https://api.open-meteo.com") as client:
        series, meta = fetch_site(client, SITES[0], SLOTS)
    assert meta["errors"] == []
    by_label = {item.spec.label: item for item in series}
    assert len(series) == len(DET_MODELS) + len(ENS_MODELS)
    # Cloud cover only exists for 12-12 hours in this payload, so 12-13 stays empty.
    assert by_label["ECMWF IFS"].total
    assert "2026-12-13T02:00" not in by_label["ECMWF IFS"].total
    assert by_label["ECMWF IFS"].last_valid == "2026-12-12T23:00"
    # Ensembles keep their member counts and derive a mean.
    assert by_label["EC-ENS"].spec.members == ENS_MODELS[0].members
    members = by_label["EC-ENS"].members[SLOTS[0]]
    assert len(members) == ENS_MODELS[0].members
    assert by_label["EC-ENS"].total[SLOTS[0]] == pytest.approx(sum(members) / len(members))


def test_meta_records_endpoints_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        series, meta = fetch_site(client, SITES[1], SLOTS, retries=1)
    assert len(meta["errors"]) == 2
    assert all(not item.active for item in series)
    assert all(item.error for item in series)


def test_transport_failure_is_visible_on_every_row():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("handshake timed out")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        series, meta = fetch_site(client, SITES[0], SLOTS, retries=1)
    assert "deterministic" in meta["errors"][0]
    assert all(item.reason.startswith("请求失败") for item in series)


def test_partial_coverage_refuses_whole_night_verdict(tmp_path):
    pytest.importorskip("matplotlib")
    # Only 18:00-20:00 exist: three hours must not become a "night median".
    values = {f"2026-12-12T{h:02d}:00": 100.0 for h in range(18, 21)}
    _, verdicts = build("2026-12-12", SITES[:1], tmp_path, font_dir=None,
                        today=dt.date(2026, 12, 10), transport=_mock_transport(values))
    _, verdict = verdicts[0]
    assert verdict["hours"] == 3
    assert verdict["median_cloud"] is None
    assert "3/12" in verdict["headline"]
    assert verdict["note"]


def test_build_writes_chart_and_metadata(tmp_path):
    pytest.importorskip("matplotlib")
    values = {f"2026-12-12T{h:02d}:00": 40.0 for h in range(18, 24)}
    values.update({f"2026-12-13T{h:02d}:00": 15.0 for h in range(0, 6)})
    png, verdicts = build("2026-12-12", SITES[:1], tmp_path, label="test",
                          font_dir=None, today=dt.date(2026, 12, 10),
                          transport=_mock_transport(values))
    assert png.exists() and png.stat().st_size > 10_000
    site, verdict = verdicts[0]
    assert site.key == SITES[0].key
    assert verdict["median_cloud"] is not None
    assert verdict["p_clear"] is not None
    assert len(verdict["rows"]) == len(DET_MODELS) + len(ENS_MODELS) + 1
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["slots"] == SLOTS
    assert set(meta["sites"]) == {SITES[0].key}
    assert meta["sites"][SITES[0].key]["sources"]
