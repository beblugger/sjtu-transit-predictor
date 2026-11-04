from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import re

import httpx
from sgp4.api import Satrec
from sgp4 import omm as sgp4_omm, exporter


@dataclass(frozen=True)
class TLEEntry:
    name: str
    norad_id: int
    line1: str
    line2: str
    epoch: datetime
    omm: dict | None = None

    def to_satrec(self) -> Satrec:
        if self.omm is None:
            return Satrec.twoline2rv(self.line1, self.line2)
        record = Satrec()
        fields = dict(self.omm)
        fields["EPOCH"] = self.epoch.replace(tzinfo=None).isoformat(timespec="microseconds")
        sgp4_omm.initialize(record, fields)
        return record

    def as_omm(self) -> dict:
        return dict(self.omm) if self.omm is not None else exporter.export_omm(self.to_satrec(), self.name)


def parse_omm_elements(rows: object) -> list[TLEEntry]:
    """Accept CelesTrak's OMM JSON subset, including six/nine digit IDs."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("Orbit catalog must be a nonempty JSON array")
    entries = []
    seen = set()
    numeric = ("MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
               "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT")
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("Invalid OMM row")
        fields = dict(raw)
        for key, default in {"CLASSIFICATION_TYPE": "U", "OBJECT_ID": "",
                             "EPHEMERIS_TYPE": 0, "ELEMENT_SET_NO": 0, "REV_AT_EPOCH": 0}.items():
            fields.setdefault(key, default)
        for key, expected in {"CENTER_NAME": "EARTH", "REF_FRAME": "TEME",
                              "TIME_SYSTEM": "UTC", "MEAN_ELEMENT_THEORY": "SGP4"}.items():
            if fields.get(key, expected) != expected:
                raise ValueError(f"Unsupported OMM {key}")
        norad_id = int(fields["NORAD_CAT_ID"])
        if not 0 < norad_id <= 999_999_999 or norad_id in seen:
            raise ValueError("Invalid or duplicate NORAD ID")
        for key in numeric:
            if not math.isfinite(float(fields[key])):
                raise ValueError(f"Invalid OMM {key}")
        if float(fields["MEAN_MOTION"]) <= 0 or not 0 <= float(fields["ECCENTRICITY"]) < 1:
            raise ValueError("Invalid orbit")
        epoch = datetime.fromisoformat(str(fields["EPOCH"]).replace("Z", "+00:00"))
        epoch = epoch.replace(tzinfo=timezone.utc) if epoch.tzinfo is None else epoch.astimezone(timezone.utc)
        fields["EPOCH"] = epoch.replace(tzinfo=None).isoformat(timespec="microseconds")
        entry = TLEEntry(str(fields.get("OBJECT_NAME") or f"NORAD {norad_id}"), norad_id,
                         "", "", epoch, fields)
        entry.to_satrec()  # Validate before replacing a known-good catalog.
        entries.append(entry)
        seen.add(norad_id)
    return entries


class CatalogUnchanged(Exception):
    """The provider reports that its current catalog has already been fetched."""


class CelestrakProvider:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        update_url: str | None = None,
    ):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.update_url = update_url
        self.known_update_at: datetime | None = None

    async def fetch_active(self, *, max_targets: int = 0) -> list[TLEEntry]:
        headers = {
            "User-Agent": "sjtu-transit-predictor/0.1 (+https://github.com/beblugger/sjtu-transit-predictor)",
            "Accept": "application/json, text/plain",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            update_at = None
            if self.update_url:
                index = await client.get(self.update_url, headers=headers)
                index.raise_for_status()
                text = re.sub(r"<[^>]+>", " ", index.text)
                match = re.search(r"Current as of\s+(\d{4} [A-Za-z]{3} \d{2} \d{2}:\d{2}:\d{2}) UTC", text)
                if not match:
                    # Do not download blindly if the freshness check is broken.
                    raise ValueError("GP index has no recognizable Current as of timestamp")
                update_at = datetime.strptime(match[1], "%Y %b %d %H:%M:%S").replace(tzinfo=timezone.utc)
                if self.known_update_at is not None and update_at <= self.known_update_at:
                    raise CatalogUnchanged(f"GP publication timestamp unchanged: {update_at.isoformat()}")
            response = await client.get(self.url, headers=headers)
            if response.status_code == 403 and "has not updated" in response.text.lower():
                raise CatalogUnchanged(response.text.strip())
            response.raise_for_status()
        if response.text.lstrip().startswith("["):
            entries = parse_omm_elements(json.loads(response.text))
        else:
            entries = parse_three_line_elements(response.text)
        if not entries:
            raise ValueError("Orbit provider returned no usable records")
        if update_at is not None:
            self.known_update_at = update_at
        if max_targets > 0:
            entries = entries[:max_targets]
        return entries


def parse_three_line_elements(text: str) -> list[TLEEntry]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    entries: list[TLEEntry] = []
    index = 0
    while index + 2 < len(lines):
        name = lines[index].strip()
        line1 = lines[index + 1].strip()
        line2 = lines[index + 2].strip()
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            index += 1
            continue
        try:
            norad_id = int(line1[2:7])
            epoch = _parse_tle_epoch(line1[18:32])
            entries.append(TLEEntry(name, norad_id, line1, line2, epoch))
        except (ValueError, IndexError):
            pass
        index += 3
    return entries


def _parse_tle_epoch(raw: str) -> datetime:
    year2 = int(raw[:2])
    year = 2000 + year2 if year2 < 57 else 1900 + year2
    day = float(raw[2:])
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1.0)


def tle_age_seconds(entry: TLEEntry, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - entry.epoch).total_seconds())
