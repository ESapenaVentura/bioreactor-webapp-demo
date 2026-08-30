import logging
from collections import deque

from app import config
from app.models import Reading
from app.processing import anomaly_flags, ewma_step, range_check, rate_of_change

log = logging.getLogger(__name__)

class SensorState:
    def __init__(self, reactor="", category="", sensor="", limits=None):
        # Kept so a synthetic Reading can be rebuilt from the state alone
        # (the fault-injection endpoint needs this).
        self.reactor = reactor
        self.category = category
        self.sensor = sensor
        self.times = deque(maxlen=600)
        self.values = deque(maxlen=600)
        self.smoothed = deque(maxlen=600)
        self.limits = limits                      # (low, high); either may be None
        self.status_ok = True
        self.status = "ok"
        self.anomaly = False
        self._anomaly_streak = 0                  # consecutive flagged readings
        self.roc = 0.0
        self.count = 0                            # cumulative readings, for /api/health

    def add(self, reading: Reading) -> None:
        """Append one reading and update every derived value incrementally."""
        t = reading.ts.timestamp()

        # Out-of-order timestamps do happen; ignore rather than corrupt the series.
        if self.times and t <= self.times[-1]:
            return

        self.count += 1

        if self.smoothed:
            dt = t - self.times[-1]
            s = ewma_step(self.smoothed[-1], reading.value, dt, config.EWMA_TAU_SECONDS)
        else:
            s = reading.value                     # first sample seeds the filter

        self.times.append(t)
        self.values.append(reading.value)
        self.smoothed.append(s)
        self.status_ok = reading.status_ok

        low, high = self.limits or (None, None)
        if low is not None or high is not None:
            self.status = range_check(reading.value, low, high, previous=self.status)
        else:
            self.status = "ok"

        # Recompute over a bounded tail: cheap, and the scatter estimate should
        # reflect recent behaviour rather than the whole session.
        tail = list(self.values)[-config.ANOMALY_LOOKBACK :]
        flags = anomaly_flags(tail, k=config.ANOMALY_K, window=config.ANOMALY_WINDOW)
        # Debounce: only call it an anomaly once it has held for a few readings.
        # A lone flip from noise sitting near the threshold never gets here, so
        # it can't spawn a table entry.
        self._anomaly_streak = self._anomaly_streak + 1 if (flags and flags[-1]) else 0
        self.anomaly = self._anomaly_streak >= config.ANOMALY_CONFIRM

        if len(self.smoothed) >= 2:
            self.roc = rate_of_change(
                list(self.times)[-2:], list(self.smoothed)[-2:], per_seconds=60.0
            )[-1]

    def latest(self):
        if not self.times:
            return {}
        return {
            "time": self.times[-1],
            "value": self.values[-1],
            "smoothed": self.smoothed[-1],
            "status": self.status,
            "status_ok": self.status_ok,
            "anomaly": self.anomaly,
            "roc": self.roc,
        }

    def full(self):
        return {
            **self.latest(),
            "times": list(self.times),
            "values": list(self.values),
            "smoothed_series": list(self.smoothed),
        }


class Store:
    def __init__(self, limits=None):
        self.sensors = {}
        self.limits = limits or {}
        self.received = 0                          # cumulative, for /api/health
        # Active watchlist per reactor: {reactor: {sensor: (low, high)}}. Applied
        # to a SensorState the moment it is created, and re-applied on change.
        self.watchlists = {}

    def add(self, reading: Reading) -> None:
        state = self.sensors.get(reading.key)
        if state is None:
            wl = self.watchlists.get(reading.reactor, {})
            state = self.sensors[reading.key] = SensorState(
                reactor=reading.reactor,
                category=reading.category,
                sensor=reading.sensor,
                limits=wl.get(reading.sensor) or self.limits.get(reading.sensor),
            )
        state.add(reading)
        self.received += 1

    def apply_watchlist(self, reactor: str, thresholds: dict) -> None:
        """thresholds: {sensor: {"min": float|None, "max": float|None}}.
        An empty dict unloads the watchlist for that reactor."""
        wl = {}
        for sensor, band in (thresholds or {}).items():
            low = band.get("min")
            high = band.get("max")
            if low is not None or high is not None:
                wl[sensor] = (low, high)
        self.watchlists[reactor] = wl
        for state in self.sensors.values():
            if state.reactor == reactor:
                new = wl.get(state.sensor)
                if new != state.limits:
                    state.limits = new
                    state.status = "ok"           # re-evaluate against the new band

    def tick(self, connected):
        return {
            "type": "tick",
            "connected": connected,
            "updates": {
                key: state.latest() for key, state in self.sensors.items()
            },
        }

    def init(self, connected):
        return {
            "type": "init",
            "connected": connected,
            "sensors": {
                key: state.full() for key, state in self.sensors.items()
            },
        }


async def drain(queue, store):
    while True:
        reading = await queue.get()
        try:
            store.add(reading)
        except Exception:                          # one bad reading must not kill the consumer
            log.exception("dropping reading for %s", reading.key)
        finally:
            queue.task_done()
