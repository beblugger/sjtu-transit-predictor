from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.providers.celestrak import TLEEntry


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
                """
            )
            self._connection.commit()
