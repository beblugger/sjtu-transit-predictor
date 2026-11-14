"""Independently audit an exported snapshot using Skyfield topocentric positions.

No downloads. This checks computed geometry, not real-world orbital accuracy.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize_scalar
from skyfield.api import EarthSatellite, load, load_file, wgs84
from shapely.geometry import shape

from app.campus import load_campus
from app.geo import angular_separation_arcsec
from app.providers.celestrak import parse_omm_elements


def size_violations(event: dict) -> list[str]:
    """Audit exported radii and RCS equivalents without using the size catalog."""
    violations = []
    diameter = event.get("object_angular_diameter_arcsec")
    radius = event.get("object_angular_radius_arcsec")
    if radius is not None and (diameter is None or not math.isclose(2 * radius, diameter, rel_tol=1e-10, abs_tol=1e-10)):
        violations.append("angular_radius_diameter_mismatch")
    span = event.get("physical_size_m")
    if event.get("angular_diameter_method") == "rcs_sphere":
        rcs, equivalent = event.get("satellite_rcs_m2"), event.get("equivalent_diameter_m")
        if rcs is None or rcs <= 0 or equivalent is None or not math.isclose(equivalent, 2 * math.sqrt(rcs / math.pi), rel_tol=1e-10):
            violations.append("rcs_equivalent_diameter_mismatch")
        if any(event.get(key) is not None for key in ("physical_size_m", "physical_size_min_m", "physical_size_max_m", "object_angular_diameter_min_arcsec", "object_angular_diameter_max_arcsec")):
            violations.append("rcs_estimate_reported_as_physical_size")
        span = equivalent
    if span is not None:
        expected = 2 * math.degrees(math.atan(span / (2 * event["range_km"] * 1000))) * 3600
        if diameter is None or not math.isclose(diameter, expected, rel_tol=1e-9, abs_tol=1e-9):
            violations.append("angular_size_distance_mismatch")
    return violations


def validate(root: Path, ephemeris: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    for filename, digest in manifest["files"].items():
        if Path(filename).name != filename:
            raise ValueError("Manifest filenames must be local basenames")
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Snapshot digest mismatch: {filename}")
    event_bytes = (root / "events.json").read_bytes()
    payload = json.loads(event_bytes)
    if ("events.json.gz" in manifest["files"] and
            gzip.decompress((root / "events.json.gz").read_bytes()) != event_bytes):
        raise ValueError("Compressed event data differs from the full event JSON")
    if "events.columns.json.gz" in manifest["files"]:
        from app.event_columns import decode_snapshot
        from app.compact_events import compact_snapshot
        if decode_snapshot((root / "events.columns.json.gz").read_bytes()) != compact_snapshot(payload):
            raise ValueError("Column event data differs from the compact event representation")
    if "first-screen.json" in manifest["files"]:
        from app.first_screen import build_first_screen_files
        first_screen = build_first_screen_files(
            payload, json.loads((root / "weather.json").read_bytes()),
            json.loads((root / "campus.geojson").read_bytes()), manifest)
        for filename, expected in first_screen.items():
            if (root / filename).read_bytes() != expected:
                raise ValueError(f"First-screen data differs from the full snapshot: {filename}")
    catalog = parse_omm_elements(json.loads((root / "orbits.json").read_text()))
    ts = load.timescale()
    satellites = {entry.norad_id: EarthSatellite.from_satrec(entry.to_satrec(), ts) for entry in catalog}
    eph = load_file(str(ephemeris))
    campus = load_campus(root / "campus.geojson")
    events = payload["events"]
    minimum_errors = []
    contact_errors = []
    violations = []
    region_count, boundary_count, boundary_errors = 0, 0, []
    campus_geometry = shape(json.loads((root / "campus.geojson").read_text())["features"][0]["geometry"])
    start = datetime.fromisoformat(manifest["from_time"])
    end = datetime.fromisoformat(manifest["to_time"])
    try:
        for event in events:
            violations.extend({"id": event["identifier"], "reason": reason} for reason in size_violations(event))
            visibility = event.get("visibility")
            if visibility is not None:
                region_count += 1
                region = shape(visibility["geometry"])
                domain = shape(visibility["calculation_domain"]) if visibility.get("calculation_domain") else campus_geometry
                if (not region.is_valid or region.is_empty or not domain.buffer(1e-10).covers(region)
                        or region.intersection(campus_geometry).is_empty):
                    violations.append({"id": event["identifier"], "reason": "invalid_ground_region"})
                for sample in visibility["boundary_samples"]:
                    boundary_count += 1
                    point = wgs84.latlon(sample["latitude"], sample["longitude"],
                                         elevation_m=visibility["chart"]["observer_elevation_m"])
                    sat = satellites[int(event["identifier"])]
                    body = eph[event["target"]]
                    estimate = datetime.fromisoformat(sample["predicted_minimum_time"])
                    def residual(offset):
                        from datetime import timedelta
                        instant = ts.from_datetime(estimate + timedelta(seconds=float(offset)))
                        satellite_position = (sat - point).at(instant)
                        target_position = (eph["earth"] + point).at(instant).observe(body).apparent()
                        angle = satellite_position.separation_from(target_position).arcseconds()
                        radius = 695_700_000 if event["target"] == "sun" else 1_737_400
                        threshold = np.degrees(2 * np.arctan(radius / (2 * target_position.distance().m))) * 3600
                        span = event.get("physical_size_m") or event.get("equivalent_diameter_m")
                        if span is not None:
                            threshold += np.degrees(np.arctan(span / (2 * satellite_position.distance().m))) * 3600
                        return float(angle), float(threshold)
                    width = max(3., min(600., event["duration_ms"] / 1000 * 1.5 + 3))
                    result = minimize_scalar(lambda offset: residual(offset)[0] ** 2,
                                             bounds=(-width, width), method="bounded", options={"xatol": 1e-5})
                    angle, threshold = residual(result.x)
                    error = abs(angle - threshold)
                    boundary_errors.append(error)
                    # Add the independent implementations' existing 1 arcsec
                    # agreement allowance to the inverse model's spatial tolerance.
                    allowance = 1 + np.degrees(sample["normal_gradient_rad_per_m"] * visibility["sampled_boundary_tolerance_m"]) * 3600
                    if abs(result.x) > width * .98 or error > allowance:
                        violations.append({"id": event["identifier"], "reason": "inverse_boundary_mismatch", "error_arcsec": error})
            observer = event["observer"]
            # The search grid, event contacts and inverse regions must use the
            # same WGS84 ellipsoid height declared in this snapshot.
            if abs(observer["elevation_m"] - campus.center.altitude_m) > 1e-6:
                violations.append({"id": event["identifier"], "reason": "observer_height_mismatch"})
            if visibility is not None and abs(visibility["chart"]["observer_elevation_m"] - observer["elevation_m"]) > 1e-6:
                violations.append({"id": event["identifier"], "reason": "region_height_mismatch"})
            if not campus.contains(observer["latitude"], observer["longitude"]):
                violations.append({"id": event["identifier"], "reason": "observer_outside_campus"})
            minimum = datetime.fromisoformat(event["minimum_time"].replace("Z", "+00:00"))
            if not start <= minimum < end:
                violations.append({"id": event["identifier"], "reason": "event_outside_window"})
            site = wgs84.latlon(observer["latitude"], observer["longitude"], elevation_m=observer["elevation_m"])
            target = eph[event["target"]]
            satellite = satellites[int(event["identifier"])]
            radius_m = 695_700_000 if event["target"] == "sun" else 1_737_400
            for key in ("minimum_time", "start_time", "end_time"):
                value = datetime.fromisoformat(event[key].replace("Z", "+00:00"))
                t = ts.from_datetime(value)
                sat_alt, sat_az, _ = (satellite - site).at(t).altaz()
                target_alt, target_az, distance = (eph["earth"] + site).at(t).observe(target).apparent().altaz()
                separation = angular_separation_arcsec(sat_az.degrees, sat_alt.degrees,
                                                      target_az.degrees, target_alt.degrees)
                threshold = np.degrees(2 * np.arctan(radius_m / (2 * distance.m))) * 3600
                threshold += (event["object_angular_diameter_arcsec"] or 0) / 2
                if not np.isfinite(separation):
                    violations.append({"id": event["identifier"], "reason": "nonfinite_position"})
                    continue
                if key == "minimum_time":
                    error = abs(separation - event["min_separation_arcsec"])
                    minimum_errors.append(error)
                    invalid = error > 1 or separation > threshold + 1 or min(sat_alt.degrees, target_alt.degrees) <= 0
                else:
                    error = abs(separation - threshold)
                    contact_errors.append(error)
                    invalid = error > 1
                if invalid:
                    violations.append({"id": event["identifier"], "field": key, "error_arcsec": float(error)})
    finally:
        eph.close()
    if region_count != manifest.get("visibility_event_count", region_count):
        violations.append({"reason": "visibility_count_mismatch"})
    return {
        "events_checked": len(events),
        "maximum_minimum_separation_difference_arcsec": max(minimum_errors, default=0),
        "maximum_contact_error_arcsec": max(contact_errors, default=0),
        "six_digit_event_count": sum(int(event["identifier"]) >= 100000 for event in events),
        "inverse_regions_checked": region_count,
        "inverse_boundary_samples_checked": boundary_count,
        "maximum_inverse_boundary_residual_arcsec": max(boundary_errors, default=0),
        "violations": violations,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("dist/data"))
    parser.add_argument("--ephemeris", type=Path, default=Path("~/.cache/sjtu-transit/de421.bsp").expanduser())
    args = parser.parse_args()
    report = validate(args.data, args.ephemeris)
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(report["violations"]))
