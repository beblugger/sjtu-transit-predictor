from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.providers.celestrak import TLEEntry
from app.providers.variflight import AircraftObservation
from app.geo import Geodetic


class DatabaseStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def set_cache(self, key: str, value: object, fetched_at: datetime | None = None) -> None:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO kv_cache(key, value, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at
                """,
                (key, payload, fetched_at.isoformat()),
            )
            self._connection.commit()

    def get_cache(self, key: str) -> tuple[object, datetime] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value, fetched_at FROM kv_cache WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value"]), datetime.fromisoformat(row["fetched_at"])

    def prune_cache(self, prefix: str, before: datetime) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM kv_cache WHERE key LIKE ? AND fetched_at < ?",
                                     (prefix + "%", before.isoformat()))
            self._connection.commit()

    def save_tles(self, entries: list[TLEEntry], fetched_at: datetime | None = None) -> None:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        with self._lock:
            self._connection.execute("DELETE FROM tle_catalog")
            self._connection.executemany(
                """
                INSERT INTO tle_catalog(norad_id, name, line1, line2, epoch, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry.norad_id,
                        entry.name,
                        entry.line1,
                        entry.line2,
                        entry.epoch.isoformat(),
                        fetched_at.isoformat(),
                    )
                    for entry in entries
                ],
            )
            self._connection.commit()
        self.set_cache("tle_fetched_at", fetched_at.isoformat(), fetched_at)
        self.set_cache("orbit_catalog", [entry.as_omm() for entry in entries], fetched_at)

    def load_tles(self, max_age_seconds: int, now: datetime | None = None) -> list[TLEEntry]:
        now = now or datetime.now(timezone.utc)
        cached = self.get_cache("orbit_catalog")
        if cached is not None:
            from app.providers.celestrak import parse_omm_elements
            rows, fetched_at = cached
            if max_age_seconds <= 0 or (now - fetched_at).total_seconds() <= max_age_seconds:
                return parse_omm_elements(rows)
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT name, norad_id, line1, line2, epoch, fetched_at FROM tle_catalog"
            ).fetchall()
        result: list[TLEEntry] = []
        for row in rows:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            if max_age_seconds <= 0 or (now - fetched_at).total_seconds() <= max_age_seconds:
                result.append(
                    TLEEntry(
                        name=row["name"],
                        norad_id=int(row["norad_id"]),
                        line1=row["line1"],
                        line2=row["line2"],
                        epoch=datetime.fromisoformat(row["epoch"]),
                    )
                )
        return result

    def save_observations(self, observations: list[AircraftObservation]) -> None:
        if not observations:
            return
        with self._lock:
            self._connection.executemany(
                """
                INSERT OR REPLACE INTO aircraft_samples(
                  identifier, observed_at, latitude, longitude, altitude_m,
                  callsign, icao24, aircraft_type, registration,
                  ground_speed_mps, track_deg, vertical_rate_mps, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        obs.identifier,
                        obs.observed_at.isoformat(),
                        obs.position.latitude_deg,
                        obs.position.longitude_deg,
                        obs.position.altitude_m,
                        obs.callsign,
                        obs.icao24,
                        obs.aircraft_type,
                        obs.registration,
                        obs.ground_speed_mps,
                        obs.track_deg,
                        obs.vertical_rate_mps,
                        obs.source,
                    )
                    for obs in observations
                ],
            )
            self._connection.commit()

    def load_observations(
        self,
        since: datetime,
    ) -> dict[str, list[AircraftObservation]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM aircraft_samples WHERE observed_at >= ? ORDER BY observed_at",
                (since.isoformat(),),
            ).fetchall()
        result: dict[str, list[AircraftObservation]] = {}
        for row in rows:
            observation = AircraftObservation(
                identifier=row["identifier"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                position=Geodetic(
                    float(row["latitude"]),
                    float(row["longitude"]),
                    float(row["altitude_m"]),
                ),
                callsign=row["callsign"],
                icao24=row["icao24"],
                aircraft_type=row["aircraft_type"],
                registration=row["registration"],
                ground_speed_mps=_optional_float(row["ground_speed_mps"]),
                track_deg=_optional_float(row["track_deg"]),
                vertical_rate_mps=_optional_float(row["vertical_rate_mps"]),
                source=row["source"],
            )
            result.setdefault(observation.identifier, []).append(observation)
        return result

    def prune_observations(self, before: datetime) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM aircraft_samples WHERE observed_at < ?",
                (before.isoformat(),),
            )
            self._connection.commit()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_cache (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tle_catalog (
                  norad_id INTEGER PRIMARY KEY,
                  name TEXT NOT NULL,
                  line1 TEXT NOT NULL,
                  line2 TEXT NOT NULL,
                  epoch TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS aircraft_samples (
                  identifier TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  latitude REAL NOT NULL,
                  longitude REAL NOT NULL,
                  altitude_m REAL NOT NULL,
                  callsign TEXT,
                  icao24 TEXT,
                  aircraft_type TEXT,
                  registration TEXT,
                  ground_speed_mps REAL,
                  track_deg REAL,
                  vertical_rate_mps REAL,
                  source TEXT NOT NULL,
                  PRIMARY KEY(identifier, observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_aircraft_time
                  ON aircraft_samples(observed_at);
                """
            )
            self._connection.commit()


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
