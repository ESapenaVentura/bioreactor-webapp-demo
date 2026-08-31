"""AnomalyStore: per-reactor ring buffer + acknowledge, and the drain-loop
edge detection that feeds it."""

import asyncio

import pytest

from app import config
from app.anomalies import AnomalyStore
from app.models import Reading
from app.state import Store, drain

from datetime import datetime, timedelta, timezone


@pytest.fixture
def store_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.anomalies.FILE", tmp_path / "anomalies.json")
    return tmp_path / "anomalies.json"


def test_cap_is_per_reactor_not_total(store_file, monkeypatch):
    monkeypatch.setattr(config, "MAX_ANOMALIES", 5)
    s = AnomalyStore()

    for i in range(8):
        s.record("A", "pH", float(i), float(i), "anomaly")
    for i in range(3):
        s.record("B", "DO", float(i), float(i), "low", 10.0)

    a = s.all("A")
    assert len(a) == 5                       # trimmed to the cap
    assert [e["value"] for e in a] == [3, 4, 5, 6, 7]   # oldest dropped
    assert len(s.all("B")) == 3              # untouched by A's overflow
    assert len(s.all()) == 8


def test_record_persists_and_reloads(store_file, monkeypatch):
    monkeypatch.setattr(config, "MAX_ANOMALIES", 100)
    AnomalyStore().record("A", "pH", 7.9, 1.0, "high", 7.5)

    assert store_file.exists()
    reloaded = AnomalyStore().all()
    assert len(reloaded) == 1 and reloaded[0]["sensor"] == "pH"


def test_ack_only_touches_named_ids(store_file):
    s = AnomalyStore()
    a = s.record("A", "pH", 1.0, 1.0, "anomaly")
    b = s.record("A", "pH", 2.0, 2.0, "anomaly")

    touched = s.ack([a["id"]], at=123.0)
    assert [e["id"] for e in touched] == [a["id"]]
    assert s.all()[0]["acked"] is True and s.all()[0]["ackedAt"] == 123.0
    assert s.all()[1]["acked"] is False

    # already-acked rows aren't returned again
    assert s.ack([a["id"], b["id"]], at=200.0) == [b_ for b_ in s.all() if b_["id"] == b["id"]]


def test_ack_all_can_scope_to_a_reactor(store_file):
    s = AnomalyStore()
    s.record("A", "pH", 1.0, 1.0, "anomaly")
    s.record("B", "DO", 2.0, 2.0, "low", 10.0)

    s.ack_all(reactor="A")
    assert s.all("A")[0]["acked"] is True
    assert s.all("B")[0]["acked"] is False


# --- drain edge detection ------------------------------------------------

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _run_drain(readings, store, anomalies):
    q = asyncio.Queue()
    for r in readings:
        q.put_nowait(r)
    task = asyncio.create_task(drain(q, store, anomalies))
    await q.join()
    task.cancel()


def _readings(values, sensor="pH", reactor="A", status_ok=True):
    return [
        Reading(
            reactor=reactor, category="Time Series", sensor=sensor, value=float(v),
            ts=START + timedelta(seconds=i), status_ok=status_ok,
        )
        for i, v in enumerate(values)
    ]


def test_drain_logs_one_row_on_the_rising_edge(store_file, monkeypatch):
    import random
    monkeypatch.setattr(config, "MAX_ANOMALIES", 100)
    rng = random.Random(0)
    calm = [7.0 + rng.gauss(0, 0.02) for _ in range(90)]
    spike = [9.0] * (config.ANOMALY_CONFIRM + 2)      # stays anomalous a while

    store, anomalies = Store(), AnomalyStore()
    asyncio.run(_run_drain(_readings(calm + spike), store, anomalies))

    rows = anomalies.all()
    assert len(rows) == 1                              # one edge, not one per reading
    assert rows[0]["kind"] == "anomaly"
    assert rows[0]["sensor"] == "pH" and rows[0]["reactor"] == "A"


def test_drain_logs_a_watchlist_crossing_with_the_threshold(store_file, monkeypatch):
    monkeypatch.setattr(config, "MAX_ANOMALIES", 100)
    store, anomalies = Store(), AnomalyStore()
    store.apply_watchlist("A", {"pH": {"min": None, "max": 7.5}})

    asyncio.run(_run_drain(_readings([7.0] * 5 + [7.9] * 3), store, anomalies))

    rows = anomalies.all()
    assert len(rows) == 1
    assert rows[0]["kind"] == "high" and rows[0]["threshold"] == 7.5
