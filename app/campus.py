from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.geo import Geodetic
from app.models import ObserverPoint


@dataclass(frozen=True)
class Campus:
    name: str
    rings: tuple[tuple[tuple[float, float], ...], ...]
    bounds: tuple[float, float, float, float]
    center: Geodetic

    def contains(self, latitude: float, longitude: float) -> bool:
        inside = False
        for ring in self.rings:
            if _point_in_ring(longitude, latitude, ring):
                inside = not inside
        return inside

    def grid(self, spacing_m: float = 100.0) -> list[ObserverPoint]:
        if spacing_m <= 0:
            raise ValueError("grid spacing must be positive")
        min_lon, min_lat, max_lon, max_lat = self.bounds
        lat_step = spacing_m / 111_320.0
        lon_step = spacing_m / (111_320.0 * np.cos(np.radians(self.center.latitude_deg)))
        rows = max(2, int(np.ceil((max_lat - min_lat) / lat_step)) + 1)
        cols = max(2, int(np.ceil((max_lon - min_lon) / lon_step)) + 1)
        points: list[ObserverPoint] = []
        for row in range(rows):
            lat = min_lat + row * (max_lat - min_lat) / max(1, rows - 1)
            for col in range(cols):
                lon = min_lon + col * (max_lon - min_lon) / max(1, cols - 1)
                if self.contains(lat, lon):
                    points.append(
                        ObserverPoint(
                            latitude=lat,
                            longitude=lon,
                            elevation_m=self.center.altitude_m,
                            campus_cell_id=f"{row:03d}-{col:03d}",
                        )
                    )
        if not points:
            raise RuntimeError("campus grid did not contain any points")
        return points

    def refine(self, point: ObserverPoint, spacing_m: float = 25.0) -> list[ObserverPoint]:
        lat_delta = spacing_m / 111_320.0
        lon_delta = spacing_m / (111_320.0 * np.cos(np.radians(point.latitude)))
        candidates: list[ObserverPoint] = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                lat = point.latitude + di * lat_delta
                lon = point.longitude + dj * lon_delta
                if self.contains(lat, lon):
                    candidates.append(
                        ObserverPoint(
                            latitude=lat,
                            longitude=lon,
                            elevation_m=self.center.altitude_m,
                            campus_cell_id=f"{point.campus_cell_id or 'refine'}-{di}{dj}",
                        )
                    )
        return candidates


def _point_in_ring(x: float, y: float, ring: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


def load_campus(path: Path) -> Campus:
    data = json.loads(path.read_text(encoding="utf-8"))
    feature = data["features"][0]
    geometry = feature["geometry"]
    coordinates = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        raw_rings = [coordinates[0]]
    elif geometry["type"] == "MultiPolygon":
        raw_rings = [polygon[0] for polygon in coordinates]
    else:
        raise ValueError(f"unsupported campus geometry: {geometry['type']}")
    rings = tuple(tuple((float(p[0]), float(p[1])) for p in ring) for ring in raw_rings)
    all_points = [point for ring in rings for point in ring]
    min_lon = min(p[0] for p in all_points)
    min_lat = min(p[1] for p in all_points)
    max_lon = max(p[0] for p in all_points)
    max_lat = max(p[1] for p in all_points)
    center_data = feature["properties"].get("center", [121.42839, 31.02807])
    return Campus(
        name=feature["properties"].get("name", "SJTU Minhang"),
        rings=rings,
        bounds=(min_lon, min_lat, max_lon, max_lat),
        center=Geodetic(float(center_data[1]), float(center_data[0]),
                        float(feature["properties"].get("observer_ellipsoid_height_m", 10.0))),
    )
