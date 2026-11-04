"""Independently audit an exported snapshot using Skyfield topocentric positions.

No downloads. This checks computed geometry, not real-world orbital accuracy.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import numpy as np
from skyfield.api import EarthSatellite, load, load_file, wgs84

from app.campus import load_campus
from app.geo import angular_separation_arcsec
from app.providers.celestrak import parse_omm_elements


def validate(root: Path, ephemeris: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    for filename, digest in manifest["files"].items():
        if Path(filename).name != filename:
            raise ValueError("Manifest filenames must be local basenames")
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Snapshot digest mismatch: {filename}")
    catalog = parse_omm_elements(json.loads((root / "orbits.json").read_text()))
    ts = load.timescale()
    satellites = {entry.norad_id: EarthSatellite.from_satrec(entry.to_satrec(), ts) for entry in catalog}
    eph = load_file(str(ephemeris))
    campus = load_campus(root / "campus.geojson")
    events = json.loads((root / "events.json").read_text())["events"]
    minimum_errors = []
    contact_errors = []
    violations = []
    start = datetime.fromisoformat(manifest["from_time"])
    end = datetime.fromisoformat(manifest["to_time"])
    try:
        for event in events:
            observer = event["observer"]
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
    return {
        "events_checked": len(events),
        "maximum_minimum_separation_difference_arcsec": max(minimum_errors, default=0),
        "maximum_contact_error_arcsec": max(contact_errors, default=0),
        "six_digit_event_count": sum(int(event["identifier"]) >= 100000 for event in events),
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
