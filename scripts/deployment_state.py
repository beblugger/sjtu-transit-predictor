"""Compact durable inputs, separate from disposable Actions geometry caches."""
from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import sqlite3
import tempfile


def pack(database: Path, destination: Path) -> None:
    # SQLite backup also includes committed WAL contents without copying live files.
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / "inputs.db"
        with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
            target.execute("DELETE FROM kv_cache WHERE key LIKE 'prediction:%'")
            target.execute("DELETE FROM aircraft_samples")
            target.commit()
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("VACUUM")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(gzip.compress(snapshot.read_bytes(), mtime=0))


def restore(snapshot: Path, database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    # Do not accidentally create a database with an old WAL alongside it.
    if any(Path(str(database) + suffix).exists() for suffix in ("", "-wal", "-shm")):
        raise FileExistsError(database)
    payload = gzip.decompress(snapshot.read_bytes())
    if not payload.startswith(b"SQLite format 3\0"):
        raise ValueError("Invalid SQLite snapshot")
    database.write_bytes(payload)
    with sqlite3.connect(database) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Corrupt SQLite snapshot")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("pack", "restore"))
    parser.add_argument("--database", type=Path, default=Path(".state/transit.db"))
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "pack":
        pack(args.database, args.snapshot)
    else:
        restore(args.snapshot, args.database)
