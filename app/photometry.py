"""Satellite brightness models described by Stellarium's Satellites plugin.

Equations and catalog provenance: app/data/satellite_photometry.json and
THIRD_PARTY_NOTICES.md. Magnitudes exclude atmospheric extinction and flares.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path

PHOTOMETRY_PATH = Path(__file__).with_name("data") / "satellite_photometry.json"


@dataclass(frozen=True)
class MagnitudeEstimate:
    magnitude: float
    model: str


@lru_cache(maxsize=1)
def load_photometry_snapshot() -> dict:
    return json.loads(PHOTOMETRY_PATH.read_text(encoding="utf-8"))


def load_photometry_catalog() -> dict[str, dict]:
    return load_photometry_snapshot()["satellites"]


def estimate_magnitude(
    name: str,
    range_km: float,
    phase_angle_deg: float | None,
    *,
    sunlit: bool | None,
    standard_magnitude: float | None = None,
    rcs_m2: float | None = None,
) -> MagnitudeEstimate | None:
    """Estimate a sunlit satellite; missing inputs never become placeholder mag 7."""
    if sunlit is not True or not math.isfinite(range_km) or range_km <= 0:
        return None
    distance_term = 5 * math.log10(range_km)
    if name.upper().startswith("STARLINK"):
        magnitude = 5.93 + distance_term - 15
        if "DARKSAT" in name.upper():
            magnitude *= 0.78
        return MagnitudeEstimate(magnitude, "stellarium-starlink")
    if phase_angle_deg is None or not math.isfinite(phase_angle_deg) or not 0 <= phase_angle_deg <= 180:
        return None
    angle = math.radians(phase_angle_deg)
    if standard_magnitude is not None and math.isfinite(standard_magnitude) and standard_magnitude < 99:
        fraction = max(1e-6, (1 + math.cos(angle)) / 2)
        magnitude = standard_magnitude - 15.75 + distance_term - 2.5 * math.log10(fraction)
        return MagnitudeEstimate(magnitude, "stellarium-standard")
    if rcs_m2 is None or not math.isfinite(rcs_m2) or rcs_m2 <= 0:
        return None
    albedo = 0.2
    if 0.436 <= angle < 1.745:
        coefficients = [3.1765, -22.0968, 62.182, -90.0993, 70.3031, -27.9227, 4.7373]
    elif 1.745 <= angle <= 2.618:
        coefficients = [0.510905, -2.72607, 4.96646, -3.02085]
    else:
        coefficients = None
    if coefficients:
        albedo = 0.0
        for coefficient in coefficients:
            albedo = albedo * angle + coefficient
    # Equivalent Lambert sphere phase term, stable near a solar transit (180°).
    supplement = math.pi - angle
    phase_term = (supplement**3 / 3 - supplement**5 / 30 + supplement**7 / 840
                  if supplement < 0.01 else math.sin(angle) + supplement * math.cos(angle))
    if albedo <= 0 or phase_term <= 0:
        return None
    log_flux = (math.log10(phase_term) + math.log10(2 * albedo) + math.log10(rcs_m2)
                - math.log10(3 * math.pi**2) - 2 * (math.log10(range_km) + 3))
    return MagnitudeEstimate(-26.74 - 2.5 * log_flux, "stellarium-rcs")
