import math

from scripts.validate_export import size_violations


def valid_estimate():
    diameter = 2 * math.degrees(math.atan(5 / 500_000)) * 3600
    return {"angular_diameter_method": "rcs_sphere", "range_km": 500,
            "satellite_rcs_m2": math.pi * 25, "equivalent_diameter_m": 10,
            "object_angular_radius_arcsec": diameter / 2, "object_angular_diameter_arcsec": diameter}


def test_independent_size_audit_catches_radius_diameter_and_range_unit_errors():
    event = valid_estimate()
    assert not size_violations(event)
    assert "angular_radius_diameter_mismatch" in size_violations({**event, "object_angular_radius_arcsec": event["object_angular_diameter_arcsec"]})
    assert "angular_size_distance_mismatch" in size_violations({**event, "range_km": 500_000})
    assert "rcs_equivalent_diameter_mismatch" in size_violations({**event, "equivalent_diameter_m": 5})
    assert "rcs_estimate_reported_as_physical_size" in size_violations({**event, "physical_size_m": 10})
    assert "rcs_estimate_reported_as_physical_size" in size_violations({**event, "object_angular_diameter_min_arcsec": 1})
