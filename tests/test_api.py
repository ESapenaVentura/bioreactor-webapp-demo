"""Contract tests for the web layer.

The point of the field-set assertions below: if the frontend starts reading a
new key, someone adds it here, and this test fails until the backend actually
sends it — which is a lot easier to debug than a blank chart.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import Reading

# --- the contract, enumerated explicitly -----------------------------------

LATEST_FIELDS = {
    "time", "value", "smoothed", "status", "status_ok", "anomaly", "roc",
}
SERIES_FIELDS = {"times", "values", "smoothed_series"}
SNAPSHOT_SENSOR_FIELDS = LATEST_FIELDS | SERIES_FIELDS   # what state.full() owes
TICK_SENSOR_FIELDS = LATEST_FIELDS                       # tick carries no series

REACTOR = "Cytiva Wave"


# --- helpers --------------------------------------------------------------

def _seed(store, sensor="Temperature", values=(36.5,) * 5, reactor=REACTOR):
    """Push readings into the store, one second apart, continuing this sensor's
    clock so repeated calls for the same sensor don't collide."""
    key = f"{reactor}|{sensor}"
    existing = store.sensors.get(key)
    t0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    if existing and existing.times:
        t0 = existing.times[-1] + 1
    for i, v in enumerate(values):
        store.add(Reading(
            reactor=reactor, category="Time Series", sensor=sensor,
            value=float(v),
            ts=datetime.fromtimestamp(t0 + i, tz=timezone.utc), status_ok=True,
        ))
    return key


def _wait_until(predicate, timeout=2.0):
    """Poll from the test thread while the app's event loop runs elsewhere."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Keep tests off the network: the real source would sit in a reconnect
    # loop against a server that isn't there.
    async def _no_source(queue, status):
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "run_source_forever", _no_source)
    # Faster than the 4 Hz default so websocket tests don't dawdle.
    monkeypatch.setattr(main.config, "BROADCAST_HZ", 50.0)
    # Never touch the real watchlist file.
    monkeypatch.setattr("app.watchlist.FILE", tmp_path / "watchlists.json")

    with TestClient(main.app) as c:
        yield c


# --- tests ---------------------------------------------------------------

def test_root_serves_the_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "bioreactor monitor" in r.text.lower()


def test_snapshot_enumerates_every_field_the_browser_reads(client):
    key = _seed(client.app.state.store, values=[36.5 + 0.01 * i for i in range(30)])

    body = client.get("/api/snapshot").json()

    assert set(body) == {"type", "connected", "sensors"}
    assert body["type"] == "init"
    assert set(body["sensors"]) == {key}
    # Exact equality, not a subset: a missing backend field fails here.
    assert set(body["sensors"][key]) == SNAPSHOT_SENSOR_FIELDS


def test_socket_sends_init_first_then_ticks_and_tick_has_no_series(client):
    key = _seed(client.app.state.store, values=[36.5 + 0.01 * i for i in range(30)])

    with client.websocket_connect("/ws") as ws:
        init = ws.receive_json()
        assert init["type"] == "init"
        assert set(init["sensors"][key]) == SNAPSHOT_SENSOR_FIELDS

        tick = ws.receive_json()
        assert tick["type"] == "tick"
        assert set(tick) == {"type", "connected", "updates"}
        assert "series" not in tick
        for entry in tick["updates"].values():
            assert set(entry) == TICK_SENSOR_FIELDS
            assert not (set(entry) & SERIES_FIELDS)


def test_injection_changes_the_current_value(client):
    store = client.app.state.store
    key = _seed(store, values=(36.5,) * 30)
    before = store.sensors[key].values[-1]

    r = client.post("/api/debug/inject", json={"sensor": "Temperature", "sigmas": 10})
    assert r.status_code == 200
    injected = r.json()["injected"]
    assert injected != before

    # It rides the same queue as real readings, so it should land in the store.
    assert _wait_until(lambda: store.sensors[key].values[-1] == injected)


def test_injecting_an_unknown_sensor_is_a_404_not_a_500(client):
    _seed(client.app.state.store)                      # a different sensor exists

    r = client.post("/api/debug/inject", json={"sensor": "Pressure", "sigmas": 10})
    assert r.status_code == 404


def test_health_reports_counts(client):
    _seed(client.app.state.store, values=(36.5,) * 7)

    body = client.get("/api/health").json()
    assert body["connected"] is False
    assert body["sensors"] == 1
    assert body["received"] == 7
    assert body["browsers"] == 0


# --- multi-reactor -------------------------------------------------------

def test_reactors_endpoint_lists_what_the_store_holds(client):
    store = client.app.state.store
    _seed(store, "Temperature", reactor="Cytiva Wave")
    _seed(store, "pH", reactor="Cytiva XDR")

    body = client.get("/api/reactors").json()
    assert set(body["reactors"]) == {"Cytiva Wave", "Cytiva XDR"}
    assert body["default"] == "Cytiva Wave"


def test_health_and_snapshot_scope_to_one_reactor(client):
    store = client.app.state.store
    _seed(store, "Temperature", values=(36.5,) * 4, reactor="Cytiva Wave")
    _seed(store, "pH", values=(7.0,) * 6, reactor="Cytiva XDR")

    whole = client.get("/api/health").json()
    assert whole["sensors"] == 2 and whole["received"] == 10 and whole["reactor"] is None

    scoped = client.get("/api/health", params={"reactor": "Cytiva XDR"}).json()
    assert scoped["reactor"] == "Cytiva XDR"
    assert scoped["sensors"] == 1
    assert scoped["received"] == 6

    snap = client.get("/api/snapshot", params={"reactor": "Cytiva XDR"}).json()
    assert set(snap["sensors"]) == {"Cytiva XDR|pH"}


def test_inject_targets_the_named_reactor(client):
    store = client.app.state.store
    _seed(store, "pH", values=(7.0,) * 30, reactor="Cytiva Wave")
    xdr = _seed(store, "pH", values=(7.4,) * 30, reactor="Cytiva XDR")

    r = client.post("/api/debug/inject", json={"reactor": "Cytiva XDR", "sensor": "pH"})
    assert r.status_code == 200
    assert r.json()["reactor"] == "Cytiva XDR"
    injected = r.json()["injected"]

    assert _wait_until(lambda: store.sensors[xdr].values[-1] == injected)
    # the Cytiva Wave pH series is untouched
    assert store.sensors["Cytiva Wave|pH"].values[-1] == 7.0


def test_inject_unknown_reactor_is_404(client):
    _seed(client.app.state.store, "pH", reactor="Cytiva Wave")

    r = client.post("/api/debug/inject", json={"reactor": "Nope", "sensor": "pH"})
    assert r.status_code == 404


# --- watchlist ---------------------------------------------------------

def test_watchlist_apply_drives_sensor_status(client):
    store = client.app.state.store
    key = _seed(store, "pH", values=(7.0,) * 5, reactor="Cytiva Wave")
    assert store.sensors[key].status == "ok"

    r = client.post("/api/watchlist", json={
        "reactor": "Cytiva Wave",
        "thresholds": {"pH": {"min": 6.8, "max": 7.2}},
    })
    assert r.status_code == 200
    assert r.json()["active"] == {"pH": {"min": 6.8, "max": 7.2}}

    _seed(store, "pH", values=(7.9,), reactor="Cytiva Wave")   # one high reading
    assert store.sensors[key].status == "high"


def test_watchlist_open_bound_only_max(client):
    store = client.app.state.store
    key = _seed(store, "DO", values=(80.0,) * 3, reactor="Cytiva Wave")

    client.post("/api/watchlist", json={
        "reactor": "Cytiva Wave", "thresholds": {"DO": {"max": 90.0}},   # no min
    })
    _seed(store, "DO", values=(50.0,), reactor="Cytiva Wave")            # low, but no min set
    assert store.sensors[key].status == "ok"
    _seed(store, "DO", values=(95.0,), reactor="Cytiva Wave")
    assert store.sensors[key].status == "high"


def test_watchlist_inverted_band_is_dropped(client):
    r = client.post("/api/watchlist", json={
        "reactor": "Cytiva Wave", "thresholds": {"pH": {"min": 8.0, "max": 7.0}},
    })
    assert r.json()["active"] == {}


def test_watchlist_save_load_delete(client):
    body = {"reactor": "Cytiva Wave", "thresholds": {"Temperature": {"min": 36.5, "max": 37.5}}}
    client.put("/api/watchlist/saved/SOP limits", json=body)

    listing = client.get("/api/watchlist", params={"reactor": "Cytiva Wave"}).json()
    assert "SOP limits" in listing["names"]

    got = client.get("/api/watchlist/saved/SOP limits", params={"reactor": "Cytiva Wave"}).json()
    assert got["thresholds"] == {"Temperature": {"min": 36.5, "max": 37.5}}

    # scoped per reactor
    assert client.get("/api/watchlist", params={"reactor": "Cytiva XDR"}).json()["names"] == []

    d = client.delete("/api/watchlist/saved/SOP limits", params={"reactor": "Cytiva Wave"})
    assert d.status_code == 200 and d.json()["names"] == []
    assert client.get("/api/watchlist/saved/SOP limits",
                      params={"reactor": "Cytiva Wave"}).status_code == 404


def test_watchlist_unload_clears_limits(client):
    store = client.app.state.store
    key = _seed(store, "pH", values=(7.0,) * 3, reactor="Cytiva Wave")
    client.post("/api/watchlist", json={
        "reactor": "Cytiva Wave", "thresholds": {"pH": {"min": 6.9, "max": 7.1}},
    })
    _seed(store, "pH", values=(9.0,), reactor="Cytiva Wave")
    assert store.sensors[key].status == "high"

    client.delete("/api/watchlist", params={"reactor": "Cytiva Wave"})
    _seed(store, "pH", values=(9.0,), reactor="Cytiva Wave")
    assert store.sensors[key].status == "ok"
    assert store.sensors[key].limits is None
