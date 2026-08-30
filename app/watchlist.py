"""Server-side storage for user-defined watchlists.

A *watchlist* is a set of hard min/max limits per sensor, scoped to one reactor.
The "active" watchlist for a reactor is applied to the live SensorStates; saved
watchlists are named presets you can load. Both live in one JSON file so a
restart doesn't drop your alarm config.

    {
      "saved":  {reactor: {name: {sensor: {"min": x|null, "max": y|null}}}},
      "active": {reactor: {sensor: {"min": x|null, "max": y|null}}}
    }
"""

import json
import pathlib
import threading

FILE = pathlib.Path(__file__).parent / "data" / "watchlists.json"

_lock = threading.Lock()


def _clean(thresholds: dict) -> dict:
    """Keep only sensors with a usable band; drop empty or inverted ones."""
    out = {}
    for sensor, band in (thresholds or {}).items():
        low = band.get("min")
        high = band.get("max")
        low = float(low) if low is not None else None
        high = float(high) if high is not None else None
        if low is not None and high is not None and high <= low:
            continue                              # inverted — ignore
        if low is None and high is None:
            continue
        out[sensor] = {"min": low, "max": high}
    return out


def bands(thresholds: dict) -> dict:
    """{sensor: {"min", "max"}} -> {sensor: (low, high)} for SensorState.limits."""
    return {s: (b["min"], b["max"]) for s, b in _clean(thresholds).items()}


class WatchlistStore:
    def __init__(self):
        try:
            self._data = json.loads(FILE.read_text())
        except (FileNotFoundError, ValueError):
            self._data = {}

    def _flush(self) -> None:
        FILE.parent.mkdir(exist_ok=True)
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(FILE)

    # --- saved presets ---------------------------------------------------

    def names(self, reactor: str) -> list[str]:
        return sorted(self._data.get("saved", {}).get(reactor, {}))

    def get_saved(self, reactor: str, name: str):
        return self._data.get("saved", {}).get(reactor, {}).get(name)

    def save(self, reactor: str, name: str, thresholds: dict) -> None:
        with _lock:
            self._data.setdefault("saved", {}).setdefault(reactor, {})[name] = _clean(thresholds)
            self._flush()

    def delete(self, reactor: str, name: str) -> bool:
        with _lock:
            saved = self._data.get("saved", {}).get(reactor, {})
            if name not in saved:
                return False
            del saved[name]
            self._flush()
            return True

    # --- active watchlist ----------------------------------------------

    def active(self, reactor: str) -> dict:
        return self._data.get("active", {}).get(reactor, {})

    def all_active(self) -> dict:
        return dict(self._data.get("active", {}))

    def set_active(self, reactor: str, thresholds: dict) -> dict:
        cleaned = _clean(thresholds)
        with _lock:
            active = self._data.setdefault("active", {})
            if cleaned:
                active[reactor] = cleaned
            else:
                active.pop(reactor, None)
            self._flush()
        return cleaned
