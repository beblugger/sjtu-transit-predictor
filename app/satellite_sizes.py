from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.geo import angular_diameter_arcsec


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

    def angular_diameter_range_arcsec(self, range_m: float) -> tuple[float, float]:
        return (
            angular_diameter_arcsec(self.minimum_m, range_m),
            angular_diameter_arcsec(self.maximum_m, range_m),
        )


class SatelliteSizeCatalog:
    def __init__(self, path: Path):
        self.path = path
        self._sizes = self._load(path)

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
            if length is None or float(length) <= 0:
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
    return number if number > 0 else None
