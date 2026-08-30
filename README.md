# Bioreactor Monitor

A real-time web dashboard for bioreactor sensor telemetry served over OPC-UA.

## Overview

Bioreactor Monitor connects to an OPC-UA server (a real instrument, or the
bundled simulator), subscribes to every reactor and sensor it exposes, and
streams the readings to the browser over a WebSocket. The page shows a live tile
and a trend chart per sensor, runs anomaly detection on the incoming data, and
lets an operator define per-sensor min/max alarm limits — a *watchlist* — that
raise `HIGH`/`LOW` events when a value leaves its band. It is one process with no
database: the working set lives in memory, alarm limits persist to a small JSON
file, and the whole thing ships as two containers you bring up with a single
command.

## Features

### Live sensor feed

Every sensor gets a tile (current value, rate of change, status badges) and a
trend chart drawn with [uPlot](https://github.com/leeoniya/uPlot). Data is
pushed on a fixed 4 Hz timer rather than per reading, so the browser's workload
and the network stay constant no matter how fast the instrument reports, and a
slow client can't stall the OPC-UA subscription. Multiple reactors are
supported — switch between them from the header; the view rebuilds instantly.

### Anomaly detection

Each sensor's stream is watched for points that sit implausibly far from its
recent trend. The detector fits a robust local line to a trailing window and
flags residuals past a MAD-scaled threshold, and it only reports an anomaly once
it has held for a few consecutive readings — so ordinary noise and drift don't
trip it. Detected anomalies land in a paginated, acknowledgeable table (with CSV
export) that persists across reloads. Detection runs for **all** reactors, so an
anomaly on one you aren't currently viewing still shows up.

### Watchlist

Set an explicit `min` and/or `max` for any sensor, per reactor. Crossing a limit
raises a `HIGH` or `LOW` row in the anomaly table and lights a sticky alarm strip
at the top of the page; a warning marker appears next to the reactor selector
when a reactor you aren't viewing has a breach. Watchlists can be saved as named
presets and are stored server-side, so every client sees the same configuration
and it survives a restart.

## Deploy with Docker Compose

**Prerequisites:** Docker with the Compose plugin (`docker compose version`).

1. **Clone the repository**

   ```bash
   git clone <repo-url>
   cd bioreactor-webapp-demo
   ```

2. **Build and start the stack**

   ```bash
   docker compose up --build
   ```

   This builds and runs two services:

   - `bioreactor-server` — the OPC-UA simulator, listening on `:4840`
   - `app` — the web app, which waits for the simulator's port to come up, then
     connects to `opc.tcp://bioreactor-server:4840/ganymede/server/`

   Add `-d` to run detached.

3. **Open the dashboard**

   <http://localhost:8000>

   Within a few seconds the connection indicator turns **live** and tiles appear
   for the default reactor. The simulated sensors take about a minute to build
   enough history for the charts and anomaly detection to be meaningful.

4. **Check status (optional)**

   ```bash
   docker compose exec app python -c "import urllib.request,json; \
     print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health')))"
   ```

   Look for `"connected": true`. If it's `false`, `last_error` says why.

5. **Stop it**

   ```bash
   docker compose down          # keep saved watchlists
   docker compose down -v       # also delete the watchlist volume
   ```

### Trying the features

- **See an anomaly fire** — open **Demo controls**, pick a sensor, and click
  *Push a fake reading*. A synthetic out-of-range value goes onto the same queue
  as real data and shows up in the anomaly table within a second.
- **Set a watchlist limit** — in the **Watchlist** panel, choose a sensor, type a
  `min` or `max`, and click *Add*. Set a limit the current value already violates
  and the alarm strip appears on the next reading.

### Pointing at a real instrument

The default `SERVER_URL` in `docker-compose.yml` points at the bundled simulator.
To use a real OPC-UA server, override it and drop the `bioreactor-server`
service:

```yaml
services:
  app:
    environment:
      SERVER_URL: opc.tcp://<host>:4840/<path>
      NAMESPACE_URI: <your-namespace-uri>
    depends_on: []
```

## Running locally without Docker

```bash
pip install -r requirements.txt
SERVER_URL=opc.tcp://localhost:4840/ganymede/server/ python -m app.main
# open http://localhost:8000
```

You'll need an OPC-UA server running separately — e.g.
`python bioreactor-server/serve.py`.

## Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `SERVER_URL` | `opc.tcp://localhost:4840/ganymede/server/` | OPC-UA server to connect to |
| `NAMESPACE_URI` | `http://examples.ganymede.github.io` | OPC-UA namespace |
| `REACTOR` | `Cytiva Wave` | reactor selected by default |
| `BROADCAST_HZ` | `4.0` | WebSocket frame rate |
| `ANOMALY_CONFIRM` | `3` | consecutive flagged readings before an anomaly is reported |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Or inside the image: `docker build --target test .`
