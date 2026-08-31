import asyncio
import contextlib
import os
import pathlib
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.anomalies import AnomalyStore
from app.hub import ConnectionManager, broadcast_loop
from app.models import Reading
from app.processing import robust_sigma
from app.source import run_source_forever
from app.state import Store, drain
from app.watchlist import WatchlistStore


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Everything before ``yield`` starts at boot; everything after stops at shutdown."""
    app.state.queue = asyncio.Queue(maxsize=1000)
    app.state.store = Store()
    app.state.hub = ConnectionManager()
    app.state.status = {"connected": False, "last_error": None, "reactors": []}

    app.state.watchlists = WatchlistStore()
    for reactor, thresholds in app.state.watchlists.all_active().items():
        app.state.store.apply_watchlist(reactor, thresholds)

    app.state.anomalies = AnomalyStore()

    tasks = [
        asyncio.create_task(run_source_forever(app.state.queue, app.state.status)),
        asyncio.create_task(drain(app.state.queue, app.state.store, app.state.anomalies)),
        asyncio.create_task(
            broadcast_loop(
                app.state.hub, app.state.store, app.state.status, config.BROADCAST_HZ
            )
        ),
    ]

    yield

    for task in tasks:
        task.cancel()
    # We asked them to stop; CancelledError is compliance, not failure. Without
    # return_exceptions the first one re-raises and shutdown becomes a traceback.
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="bioreactor monitor", lifespan=lifespan)

STATIC = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    """Serve the single-page frontend."""
    return FileResponse(STATIC / "index.html")


def _reactor_of(key: str) -> str:
    return key.split("|", 1)[0]


@app.get("/api/reactors")
async def reactors():
    """Reactor names the source found, and which is the default selection."""
    names = app.state.status.get("reactors") or sorted(
        {s.reactor for s in app.state.store.sensors.values()}
    )
    default = config.REACTOR if config.REACTOR in names else (names[0] if names else None)
    return {"reactors": names, "default": default}


@app.get("/api/health")
async def health(reactor: str | None = None):
    """What you curl when something looks wrong. ``?reactor=`` scopes the counts."""
    store = app.state.store
    states = list(store.sensors.values())
    if reactor is not None:
        states = [s for s in states if s.reactor == reactor]
        received = sum(s.count for s in states)
    else:
        received = store.received
    return {
        "connected": app.state.status["connected"],
        "last_error": app.state.status["last_error"],
        "reactor": reactor,
        "reactors": app.state.status.get("reactors", []),
        "sensors": len(states),
        "received": received,
        "browsers": len(app.state.hub),
    }


@app.get("/api/snapshot")
async def snapshot(reactor: str | None = None):
    """The same full payload a browser gets on connect. What tests assert against."""
    payload = app.state.store.init(app.state.status["connected"])
    if reactor is not None:
        payload["sensors"] = {
            k: v for k, v in payload["sensors"].items() if _reactor_of(k) == reactor
        }
    return payload


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    hub = app.state.hub
    await hub.connect(websocket)
    try:
        await websocket.send_json(
            app.state.store.init(app.state.status["connected"])
        )
        while True:
            # We send, we don't receive — but the handler must await something,
            # or it returns and Starlette closes the socket underneath us.
            # receive_text() is also how we learn the client went away.
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)


class WatchlistBody(BaseModel):
    reactor: str
    # {sensor: {"min": float|None, "max": float|None}}
    thresholds: dict = {}


@app.get("/api/watchlist")
async def watchlist_get(reactor: str):
    """The active watchlist for a reactor, plus the names of its saved presets."""
    wl = app.state.watchlists
    return {"reactor": reactor, "active": wl.active(reactor), "names": wl.names(reactor)}


@app.get("/api/watchlists")
async def watchlists_all():
    """Every reactor's active watchlist + preset names in one shot (page load)."""
    wl = app.state.watchlists
    names = app.state.status.get("reactors") or sorted(
        {s.reactor for s in app.state.store.sensors.values()}
    )
    return {r: {"active": wl.active(r), "names": wl.names(r)} for r in names}


@app.post("/api/watchlist")
async def watchlist_apply(body: WatchlistBody):
    """Make these thresholds the reactor's active watchlist. Empty = unload."""
    cleaned = app.state.watchlists.set_active(body.reactor, body.thresholds)
    app.state.store.apply_watchlist(body.reactor, cleaned)
    return {"reactor": body.reactor, "active": cleaned}


@app.delete("/api/watchlist")
async def watchlist_unload(reactor: str):
    app.state.watchlists.set_active(reactor, {})
    app.state.store.apply_watchlist(reactor, {})
    return {"reactor": reactor, "active": {}}


@app.get("/api/watchlist/saved/{name}")
async def watchlist_saved_get(name: str, reactor: str):
    th = app.state.watchlists.get_saved(reactor, name)
    if th is None:
        raise HTTPException(404, f"no saved watchlist {name!r} for {reactor!r}")
    return {"name": name, "reactor": reactor, "thresholds": th}


@app.put("/api/watchlist/saved/{name}")
async def watchlist_saved_put(name: str, body: WatchlistBody):
    app.state.watchlists.save(body.reactor, name, body.thresholds)
    return {"name": name, "names": app.state.watchlists.names(body.reactor)}


@app.delete("/api/watchlist/saved/{name}")
async def watchlist_saved_delete(name: str, reactor: str):
    if not app.state.watchlists.delete(reactor, name):
        raise HTTPException(404, f"no saved watchlist {name!r} for {reactor!r}")
    return {"deleted": name, "names": app.state.watchlists.names(reactor)}


@app.get("/api/anomalies")
async def anomalies_get(reactor: str | None = None):
    """The anomaly / alarm log, oldest-first. ``?reactor=`` scopes to one reactor;
    the browser fetches the lot and filters client-side."""
    return {"anomalies": app.state.anomalies.all(reactor)}


class AckBody(BaseModel):
    ids: list[str] = []


@app.post("/api/anomalies/ack")
async def anomalies_ack(body: AckBody):
    """Acknowledge specific rows by id. Returns the full log so the browser can
    just replace its copy."""
    app.state.anomalies.ack(body.ids)
    return {"anomalies": app.state.anomalies.all()}


class AckAllBody(BaseModel):
    reactor: str | None = None


@app.post("/api/anomalies/ack-all")
async def anomalies_ack_all(body: AckAllBody):
    """Acknowledge every open row (optionally just one reactor's)."""
    app.state.anomalies.ack_all(body.reactor)
    return {"anomalies": app.state.anomalies.all()}


class Injection(BaseModel):
    sensor: str
    reactor: str | None = None
    sigmas: float = 10.0


@app.post("/api/debug/inject")
async def inject(body: Injection):
    """DEMO AFFORDANCE, not a feature.

    Reads the sensor's recent scatter, builds one synthetic Reading offset by N
    sigmas, and puts it on the same queue the real readings use — so it exercises
    the whole path (drain, state, processing, broadcast, browser). The only thing
    faked is the number.
    """
    store = app.state.store
    state = store.sensors.get(f"{body.reactor}|{body.sensor}") if body.reactor else None
    if state is None:
        state = next(
            (
                s for s in store.sensors.values()
                if s.sensor == body.sensor
                and (body.reactor is None or s.reactor == body.reactor)
            ),
            None,
        )
    if state is None or not state.values:
        where = f" on {body.reactor!r}" if body.reactor else ""
        raise HTTPException(404, f"no data yet for sensor {body.sensor!r}{where}")

    # `or 0.01` guards a sensor so flat its robust sigma is zero — otherwise you
    # inject N sigmas of nothing and wonder why the badge never lights.
    scatter = robust_sigma(list(state.values)[-60:]) or 0.01
    value = state.values[-1] + body.sigmas * scatter

    # The detector debounces (ANOMALY_CONFIRM consecutive flagged readings), so a
    # single synthetic point wouldn't register — send a short burst. Timestamp
    # them a hair after the last real reading, not at wall-clock now: dating them
    # in the future would make the next few genuine readings look out-of-order
    # and get dropped, stretching the fake excursion.
    n = max(1, config.ANOMALY_CONFIRM)
    base = datetime.fromtimestamp(state.times[-1], tz=timezone.utc)
    for i in range(n):
        await app.state.queue.put(Reading(
            reactor=state.reactor,
            category=state.category,
            sensor=state.sensor,
            value=value,
            ts=base + timedelta(milliseconds=i + 1),
            status_ok=True,
        ))
    return {
        "reactor": state.reactor,
        "sensor": state.sensor,
        "injected": value,
        "count": n,
        "sigmas": body.sigmas,
        "scatter": scatter,
    }


if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0 so a browser on the Windows side of WSL can reach it.
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
