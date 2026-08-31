"""Server-side storage for the anomaly / alarm log.

Every confirmed statistical anomaly and every watchlist limit crossing becomes a
row here. Rows are grouped by reactor and ring-buffered at
``config.MAX_ANOMALIES`` *per reactor*, so a noisy reactor can't push another
reactor's history out of the file. One JSON file, like the watchlist, so a
restart (or a browser with its cache cleared) doesn't lose the log — and every
browser sees the same rows.

    {
      reactor: [
        {id, reactor, sensor, value, at, kind, threshold, acked, ackedAt},
        ...                                    # oldest first
      ]
    }

Field notes: ``at`` / ``ackedAt`` are epoch seconds; ``kind`` is
``"anomaly"`` | ``"high"`` | ``"low"``; ``threshold`` is the crossed watchlist
bound for high/low rows, else ``None``.
"""

import json
import pathlib
import threading
import time
import uuid

from app import config

FILE = pathlib.Path(__file__).parent / "data" / "anomalies.json"

_lock = threading.Lock()


class AnomalyStore:
    def __init__(self):
        try:
            raw = json.loads(FILE.read_text())
            self._data = raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, ValueError):
            self._data = {}

    def _flush(self) -> None:
        FILE.parent.mkdir(exist_ok=True)
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(FILE)

    def record(self, reactor, sensor, value, at, kind, threshold=None) -> dict:
        entry = {
            "id": uuid.uuid4().hex,
            "reactor": reactor,
            "sensor": sensor,
            "value": value,
            "at": at,
            "kind": kind,
            "threshold": threshold,
            "acked": False,
            "ackedAt": None,
        }
        with _lock:
            rows = self._data.setdefault(reactor, [])
            rows.append(entry)
            excess = len(rows) - config.MAX_ANOMALIES
            if excess > 0:
                del rows[:excess]
            self._flush()
        return entry

    def all(self, reactor: str | None = None) -> list[dict]:
        """Every row, oldest-first. ``reactor`` scopes to one reactor."""
        if reactor is not None:
            return list(self._data.get(reactor, []))
        return [e for rows in self._data.values() for e in rows]

    def ack(self, ids, at: float | None = None) -> list[dict]:
        at = time.time() if at is None else at
        wanted = set(ids)
        touched = []
        with _lock:
            for rows in self._data.values():
                for e in rows:
                    if e["id"] in wanted and not e["acked"]:
                        e["acked"] = True
                        e["ackedAt"] = at
                        touched.append(e)
            if touched:
                self._flush()
        return touched

    def ack_all(self, reactor: str | None = None, at: float | None = None) -> list[dict]:
        at = time.time() if at is None else at
        groups = (
            [self._data.get(reactor, [])] if reactor is not None
            else list(self._data.values())
        )
        touched = []
        with _lock:
            for rows in groups:
                for e in rows:
                    if not e["acked"]:
                        e["acked"] = True
                        e["ackedAt"] = at
                        touched.append(e)
            if touched:
                self._flush()
        return touched
