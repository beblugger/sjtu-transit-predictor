import copy
import gzip
import hashlib
import json

import pytest
from shapely.geometry import box, mapping
from shapely.ops import transform

from app.compact_events import compact_snapshot
from app.event_columns import decode_snapshot, encode_snapshot


def visibility(*, horizon=False, rectangular=True):
    chart = {"longitude": 121.4283987654321, "latitude": 31.0280787654321,
             "east_m_per_degree": 95432.123456789, "north_m_per_degree": 110432.987654321,
             "observer_elevation_m": 10.123456789}
    domain = box(-500, -500, 500, 500)
    if not rectangular:
        domain = domain.difference(box(200, 200, 500, 500))
    region = domain.intersection(box(-100, -500, 100, 500))
    if horizon:
        region = region.intersection(box(-500, -500, 500, 50))

    def geographic(x, y, z=None):
        return (chart["longitude"] + x / chart["east_m_per_degree"],
                chart["latitude"] + y / chart["north_m_per_degree"])

    return {"method": "adaptive_first_order_angular_inverse", "chart": chart,
            "reference_time": "2026-11-10T12:34:56.987654+00:00",
            "geometry": mapping(transform(geographic, region)),
            "calculation_domain": mapping(transform(geographic, domain)),
            "models": [{"bounds_m": [-500, -500, 500, 500], "origin_m": [0, 0],
                        "cross_track_gradient_rad_per_m": [0.000001, 0],
                        "cross_track_at_origin_rad": 0, "contact_radius_rad": 0.0001,
                        "time_offset_s": 0.123456789123,
                        "time_gradient_s_per_m": [0.0123456789123, 0.0098765432123]}],
            "sampled_boundary_tolerance_m": 5.0,
            "maximum_sampled_boundary_error_m": 0.123456789,
            "boundary_samples": [{"longitude": chart["longitude"]}]}


def test_event_values_and_time_references_retain_full_precision():
    event = {"identifier": "9007199254740993", "minimum_time": "2026-11-10T12:34:56.987654+00:00",
             "start_time": "2026-11-10T12:34:55.123456+00:00",
             "end_time": "2026-11-10T12:34:58.123456+00:00",
             "observer": {"latitude": 31.0280787654321, "longitude": 121.4283987654321},
             "altitude_deg": 14.999999999, "azimuth_deg": 359.999999999,
             "target_altitude_deg": 15.000000001, "target_azimuth_deg": 0.000000001,
             "object_angular_diameter_arcsec": 0.0123456789123,
             "weather": {"score": 0.499999999999, "threshold": 0.5, "decision": "cloudy"},
             "visibility": visibility()}
    payload = {"generated_at": event["minimum_time"], "events": [event]}
    original = copy.deepcopy(payload)
    encoded = encode_snapshot(payload)
    decoded = decode_snapshot(encoded)
    assert payload == original
    assert encoded == encode_snapshot(payload)
    result = decoded["events"][0]
    assert {key: value for key, value in result.items() if key != "visibility"} == {
        key: value for key, value in event.items() if key != "visibility"}
    compact = result["visibility"]
    assert compact["chart"] == event["visibility"]["chart"]
    assert compact["reference_time"] == event["visibility"]["reference_time"]
    model, band = event["visibility"]["models"][0], compact["bands"][0]
    for key in ("bounds_m", "origin_m", "time_offset_s", "time_gradient_s_per_m"):
        assert band[key] == model[key]
    assert compact["maximum_sampled_boundary_error_m"] == 0.123456789


def test_horizon_clip_is_retained_and_nonrectangular_domain_keeps_full_geometry():
    clipped = compact_snapshot({"events": [{"visibility": visibility(horizon=True)}]})
    band = clipped["events"][0]["visibility"]["bands"][0]
    assert band["clip"][:2] == pytest.approx([0, 1], abs=1e-9)
    assert band["clip"][2] == pytest.approx(50, abs=1e-6)
    original = {"events": [{"visibility": visibility(rectangular=False)}]}
    assert compact_snapshot(original) == original
    assert "bands" not in decode_snapshot(encode_snapshot(original))["events"][0]["visibility"]


@pytest.mark.parametrize("events", [None, {}, [None], [1]])
def test_encoder_rejects_events_that_are_not_objects(events):
    with pytest.raises(ValueError, match="events must be a list of objects"):
        encode_snapshot({"events": events})


def test_empty_and_heterogeneous_event_columns_roundtrip():
    for payload in ({"events": []}, {"events": [{"optional": None}, {}, {"optional": {"value": 1}}]}):
        assert decode_snapshot(encode_snapshot(payload)) == payload


def test_old_column_format_and_dictionary_values_are_independent():
    document = {"format": "event-columns-v1", "metadata": {"generated_at": "old"}, "count": 2,
                "events": {"d": [{"observer": {"latitude": 31}}], "i": [0, 0]}}
    result = decode_snapshot(gzip.compress(json.dumps(document).encode()))
    result["events"][0]["observer"]["latitude"] = 0
    assert result["events"][1]["observer"]["latitude"] == 31


@pytest.mark.parametrize("node", [{"v": [None]}, {"v": [1]}, {"d": [1], "i": [0]},
                                  {"d": [{}], "i": [True]}, {"o": {}, "p": [1]}])
def test_decoder_rejects_invalid_event_columns(node):
    document = {"format": "event-columns-v2", "metadata": {}, "count": 1, "events": node}
    with pytest.raises(ValueError):
        decode_snapshot(gzip.compress(json.dumps(document).encode()))


@pytest.mark.parametrize("filename", ["events.json.gz", "events.columns.json.gz"])
def test_export_audit_rejects_different_event_snapshots_before_geometry_checks(tmp_path, filename):
    from scripts.validate_export import validate

    original = {"events": [{"identifier": "25544", "altitude_deg": 14.999999}]}
    different = {"events": [{"identifier": "25544", "altitude_deg": 15.0}]}
    full_json = json.dumps(original).encode()
    archive = (gzip.compress(json.dumps(different).encode()) if filename == "events.json.gz"
               else encode_snapshot(different))
    files = {"events.json": full_json, filename: archive}
    for name, value in files.items():
        (tmp_path / name).write_bytes(value)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}))
    with pytest.raises(ValueError, match="event data differs"):
        validate(tmp_path, tmp_path / "no-ephemeris-needed.bsp")
