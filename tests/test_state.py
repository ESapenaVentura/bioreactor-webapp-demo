"""SensorState behaviour that isn't pure-function processing."""

import random
from datetime import datetime, timedelta, timezone

from app import config
from app.models import Reading
from app.state import SensorState

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _feed(state, values):
    # continue from wherever this state's clock left off
    t0 = (state.times[-1] + 1) if state.times else START.timestamp()
    for i, v in enumerate(values):
        state.add(Reading(
            reactor="R", category="Time Series", sensor="pH",
            value=float(v),
            ts=datetime.fromtimestamp(t0 + i, tz=timezone.utc),
            status_ok=True,
        ))


def _calm(n, seed=0, level=7.0, noise=0.02):
    rng = random.Random(seed)
    return [level + rng.gauss(0, noise) for _ in range(n)]


def test_anomaly_needs_to_hold_before_it_is_reported():
    """A lone flagged reading must not flip `anomaly` — that flip-flop is what
    flooded the log with duplicates on a cold boot."""
    st = SensorState()
    _feed(st, _calm(60))
    assert st.anomaly is False

    _feed(st, [9.0])                                  # one wild reading
    assert st.anomaly is False                        # not confirmed yet

    _feed(st, [9.0] * (config.ANOMALY_CONFIRM - 1))
    assert st.anomaly is True                         # held long enough


def test_anomaly_clears_on_the_first_clean_reading():
    st = SensorState()
    _feed(st, _calm(60))
    _feed(st, [9.0] * config.ANOMALY_CONFIRM)
    assert st.anomaly is True

    _feed(st, _calm(1, seed=1))
    assert st.anomaly is False


def test_a_single_spike_never_reports_anomalous():
    st = SensorState()
    _feed(st, _calm(80))
    _feed(st, [50.0])                                 # inject-style single point
    _feed(st, _calm(5, seed=2))
    assert st.anomaly is False
