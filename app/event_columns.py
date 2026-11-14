"""Column encoding for the browser's compressed event snapshot."""
from __future__ import annotations

import copy
import gzip
import json
import sys


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _column(values: list) -> dict:
    nonnull = [value for value in values if value is not None]
    if nonnull and all(isinstance(value, dict) for value in nonnull):
        keys = nonnull[0].keys()
        if all(value.keys() == keys for value in nonnull):
            node = {"o": {key: _column([value[key] if value is not None else None
                                         for value in values]) for key in keys}}
            if len(nonnull) != len(values):
                node["p"] = [value is not None for value in values]
            return node

    # Intern repeated values only when doing so makes this column's gzip smaller.
    # Serialization also gives complex JSON values a stable, lossless key.
    unique, indices, seen = [], [], {}
    for value in values:
        key = _json(value)
        if key not in seen:
            seen[key] = len(unique)
            unique.append(value)
        indices.append(seen[key])
    plain = {"v": values}
    if len(unique) == len(values):
        return plain
    dictionary = {"d": unique, "i": indices}
    plain_size = len(gzip.compress(_json(plain).encode(), mtime=0))
    dictionary_size = len(gzip.compress(_json(dictionary).encode(), mtime=0))
    return dictionary if dictionary_size + 20 < plain_size else plain


def encode_snapshot(payload: dict) -> bytes:
    """Encode events without rounding their values or time references."""
    from app.compact_events import compact_snapshot
    payload = compact_snapshot(payload)
    events = payload["events"]
    document = {"format": "event-columns-v2", "metadata": {key: value for key, value in payload.items() if key != "events"},
                "count": len(events), "events": _column(events)}
    return gzip.compress(_json(document).encode("utf-8"), mtime=0)


def decode_snapshot(data: bytes) -> dict:
    """Decode and validate a column snapshot for export audits."""
    document = json.loads(gzip.decompress(data))
    if not isinstance(document, dict) or document.get("format") not in ("event-columns-v1", "event-columns-v2"):
        raise ValueError("Invalid event columns format")
    count, metadata = document.get("count"), document.get("metadata")
    if type(count) is not int or count < 0 or not isinstance(metadata, dict):
        raise ValueError("Invalid event columns metadata")

    def decode(node):
        if not isinstance(node, dict):
            raise ValueError("Invalid event column")
        if "o" in node:
            fields, present = node["o"], node.get("p")
            if not isinstance(fields, dict) or (present is not None and
                    (not isinstance(present, list) or len(present) != count or
                     any(type(value) is not bool for value in present))):
                raise ValueError("Invalid event object column")
            columns = {key: decode(value) for key, value in fields.items()}
            return [None if present is not None and not present[index] else
                    {key: column[index] for key, column in columns.items()}
                    for index in range(count)]
        if isinstance(node.get("v"), list) and len(node["v"]) == count:
            return node["v"]
        dictionary, indices = node.get("d"), node.get("i")
        if (isinstance(dictionary, list) and isinstance(indices, list) and len(indices) == count and
                all(type(index) is int and 0 <= index < len(dictionary) for index in indices)):
            return [copy.deepcopy(dictionary[index]) for index in indices]
        raise ValueError("Invalid event value column")

    events = decode(document.get("events"))
    if any(not isinstance(event, dict) for event in events):
        raise ValueError("events must be a list of objects")
    return {**metadata, "events": events}


def main() -> None:
    sys.stdout.buffer.write(encode_snapshot(json.load(sys.stdin.buffer)))


if __name__ == "__main__":
    main()
