from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import math

import yaml

from app.geo import angular_diameter_arcsec
from app.photometry import load_photometry_snapshot


@dataclass(frozen=True)
class SatelliteSize:
    norad_id: int
    name: str
    characteristic_length_m: float
    width_m: float | None
    height_m: float | None
    source_url: str
    retrieved_at: str
    confidence: str
    method: Literal["max_dimension", "rcs_sphere"] = "max_dimension"
    rcs_m2: float | None = None

    @property
    def minimum_m(self) -> float:
        values = [
            value
            for value in (self.characteristic_length_m, self.width_m, self.height_m)
            if value is not None and value > 0
        ]
        return min(values)

    @property
    def maximum_m(self) -> float:
        values = [
            value
            for value in (self.characteristic_length_m, self.width_m, self.height_m)
            if value is not None and value > 0
        ]
        return max(values)

    def angular_diameter_arcsec(self, range_m: float) -> float:
        return angular_diameter_arcsec(self.maximum_m, range_m)

    def angular_radius_arcsec(self, range_m: float) -> float:
        return self.angular_diameter_arcsec(range_m) / 2

    def event_fields(self, range_m: float) -> dict:
        """Keep measured spans separate from an RCS equivalent diameter."""
        diameter = self.angular_diameter_arcsec(range_m)
        fields = dict(object_angular_radius_arcsec=diameter / 2,
                      object_angular_diameter_arcsec=diameter,
                      angular_diameter_method=self.method,
                      angular_size_source=self.source_url,
                      angular_size_confidence=self.confidence)
        if self.method == "rcs_sphere":
            fields.update(equivalent_diameter_m=self.maximum_m, satellite_rcs_m2=self.rcs_m2)
        else:
            low, high = self.angular_diameter_range_arcsec(range_m)
            fields.update(object_angular_diameter_min_arcsec=low,
                          object_angular_diameter_max_arcsec=high,
                          physical_size_m=self.maximum_m, physical_size_min_m=self.minimum_m,
                          physical_size_max_m=self.maximum_m, physical_size_source=self.source_url,
                          physical_size_confidence=self.confidence)
        return fields

    def angular_diameter_range_arcsec(self, range_m: float) -> tuple[float, float]:
        return (
            angular_diameter_arcsec(self.minimum_m, range_m),
            angular_diameter_arcsec(self.maximum_m, range_m),
        )


class SatelliteSizeCatalog:
    def __init__(self, path: Path):
        self.path = path
        self._sizes = self._load(path)
        snapshot = load_photometry_snapshot()
        for raw_id, parameters in snapshot["satellites"].items():
            norad_id = int(raw_id)
            rcs = _optional_float(parameters.get("rcs_m2"))
            if norad_id in self._sizes or rcs is None:
                continue
            self._sizes[norad_id] = SatelliteSize(
                norad_id, f"NORAD {norad_id}", 2 * math.sqrt(rcs / math.pi), None, None,
                snapshot["source"], snapshot["retrieved_at"], "low", "rcs_sphere", rcs,
            )

    def get(self, norad_id: int) -> SatelliteSize | None:
        return self._sizes.get(norad_id)

    def __len__(self) -> int:
        return len(self._sizes)

    @staticmethod
    def _load(path: Path) -> dict[int, SatelliteSize]:
        if not path.exists():
            return {}
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result: dict[int, SatelliteSize] = {}
        for raw_id, raw in (payload.get("satellites") or {}).items():
            norad_id = int(raw_id)
            length = raw.get("characteristic_length_m")
            if _optional_float(length) is None:
                continue
            result[norad_id] = SatelliteSize(
                norad_id=norad_id,
                name=str(raw.get("name") or f"NORAD {norad_id}"),
                characteristic_length_m=float(length),
                width_m=_optional_float(raw.get("width_m")),
                height_m=_optional_float(raw.get("height_m")),
                source_url=str(raw.get("source_url") or ""),
                retrieved_at=str(raw.get("retrieved_at") or ""),
                confidence=str(raw.get("confidence") or "unknown"),
            )
        return result


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None
