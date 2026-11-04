from datetime import datetime, timezone
import sqlite3

from app.store import DatabaseStore
from scripts.deployment_state import merge, pack


def test_durable_inputs_override_old_cache_but_keep_geometry(tmp_path):
    old = datetime(2026, 11, 4, 0, tzinfo=timezone.utc)
    new = datetime(2026, 11, 4, 2, tzinfo=timezone.utc)
    cached_db = tmp_path / "cached.db"
    durable_db = tmp_path / "durable.db"
    with_store = DatabaseStore(cached_db)
    with_store.set_cache("orbit_primary_attempt", True, old)
    with_store.set_cache("prediction:cached", ["expensive geometry"], old)
    with_store.set_cache("weather_snapshot", "newer cached weather", new)
    with_store.close()
    durable = DatabaseStore(durable_db)
    durable.set_cache("orbit_primary_attempt", True, new)
    durable.set_cache("weather_snapshot", "older weather", old)
    durable.set_cache("prediction:discard", ["disposable"], new)
    durable.close()
    snapshot = tmp_path / "inputs.db.gz"
    pack(durable_db, snapshot)
    merge(snapshot, cached_db)
    restored = DatabaseStore(cached_db)
    assert restored.get_cache("orbit_primary_attempt")[1] == new
    assert restored.get_cache("prediction:cached")[0] == ["expensive geometry"]
    assert restored.get_cache("prediction:discard") is None
    assert restored.get_cache("weather_snapshot")[0] == "newer cached weather"
    restored.close()
    # The same durable snapshot also recovers when Actions evicts the whole cache.
    empty = tmp_path / "recovered.db"
    merge(snapshot, empty)
    with sqlite3.connect(empty) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
