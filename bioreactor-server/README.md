# Bioreactor server

An OPC-UA server that simulates a fleet of bioreactors. Originally copied from
Ganymede's [OPC-UA demo repository](https://github.com/Ganymede-Bio/opcua-demo/tree/main);
it has since been reworked so the signals behave like closed-loop-controlled
instruments (see **Changelog** below).

Each sensor is a small stochastic process whose value is written to an OPC-UA
variable node named `"<Category> - <Sensor>"` under one object per reactor. The
node layout, namespace (`http://examples.ganymede.github.io`) and endpoint are
unchanged from upstream, so any OPC-UA client still discovers it the same way.

## Run it

```bash
pip install -r requirements.txt
python serve.py            # binds opc.tcp://0.0.0.0:4840/ganymede/server/
```

or `docker build -t bioreactor-server . && docker run -p 4840:4840 bioreactor-server`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `SERVER_URL` | `opc.tcp://0.0.0.0:4840/ganymede/server/` | endpoint to bind |
| `SEED` | *(random)* | integer RNG seed — makes the generated signal streams reproducible across restarts |
| `FAULT_RATE` | `0` | fraction (0–1) of readings emitted with an **Uncertain** OPC-UA `StatusCode` (value present but flagged questionable), to exercise a client's bad-quality handling |
| `LOG_LEVEL` | `WARNING` (`serve.py`) / `INFO` (direct) | logging level; `DEBUG` logs every reading |
| `ASYNCUA_LOG_LEVEL` | `WARNING` | quiets the asyncua publish firehose independently |

## Models

Sensor values come from one of two processes, both advanced by the *actual*
elapsed time since the sensor last reported (so `reporting_interval` only sets
the sample rate, not the dynamics):

- **`BrownianMotionSensor` — Ornstein–Uhlenbeck.** The value is pulled back
  toward a setpoint (`value_init`, optionally ramping at `value_drift` units/s)
  with Gaussian process noise. Models a PID-controlled process variable:
  temperature, pH, DO, agitation, air flow — it hugs its setpoint and returns
  to it after a disturbance.
  - `value_std` — the *stationary* standard deviation, in the sensor's own
    engineering units (°C, pH, rpm, …).
  - `reversion_seconds` — the relaxation time: roughly how long to pull a
    disturbance ~63% of the way back to setpoint. Default `90`.
  - `value_min` / `value_max` — hard clamps (instrument range).
- **`LogisticSensor` — stochastic logistic growth.** `dX = r·X·(1 − X/K)·dt +
  noise`, noise scaled by `√dt`. Models cell density (OD600): `value_max` is the
  carrying capacity K, `value_growth_rate` is r in per-hour units, `value_std`
  the noise intensity.

The reactor and sensor roster, and every configured number (`value_init`,
intervals, ranges, `value_std`), are unchanged from upstream — only the *model*
that consumes them changed.

## Changelog

### Signal model

- **Multiplicative random walk → Ornstein–Uhlenbeck.** The previous
  `BrownianMotionSensor` did `new = old · N(1, value_std) + drift`, clamped —
  a geometric random walk whose step size scaled with the absolute value and
  which had no mean reversion, so it wandered off until it hit `value_min` /
  `value_max` and stuck against the clamp. It's now a proper OU process that
  reverts to a setpoint. `value_std` changed meaning: it was a multiplicative
  fraction, it is now the stationary standard deviation in engineering units.
  New field `reversion_seconds`.
- **Real elapsed-time integration.** Both models now step by the true `dt` since
  the sensor last reported, with noise scaled by `√dt`. Previously the Brownian
  branch ignored `dt` entirely and the logistic branch used `√(interval/60)`, so
  changing `reporting_interval` silently changed a sensor's volatility.
- **Logistic growth was ~60× too slow.** The growth term carried an extra `/60`
  (`(r/3600)·X·(1−X/K)·(interval/60)`); OD600 barely moved off its seed value.
  Fixed to integrate in real seconds.

### Bug fixes

- **`create_bioreactor` silently dropped fields.** It rebuilt every sensor
  dataclass by hand and forgot `value_std` for both sensor types, plus
  `value_max` and `value_min` for `LogisticSensor`. So *every* sensor ran at the
  dataclass defaults: OD600's carrying capacity was 100 instead of 2 and its
  noise `0.1` instead of `1e-6` (hence the wild, non-monotonic "growth" curve);
  Agitation Speed's `value_std=1` was ignored. Now uses `dataclasses.replace()`,
  which copies every field.
- **Unseeded `np.random.uniform(1.0, 20.0)`** for four reactors' reporting
  intervals now goes through the seeded RNG, so `SEED` makes a run fully
  reproducible.

### New capabilities

- **`SEED`** — reproducible signal streams (`np.random.default_rng`), needed to
  test a downstream anomaly detector against a known input.
- **`FAULT_RATE`** — emit a fraction of readings with an `Uncertain` `StatusCode`
  and an explicit `SourceTimestamp`, so a client's quality path isn't dead code.
  `Uncertain` (not `Bad`) keeps the numeric value on the wire — a full `Bad`
  status nulls it, and `app/source.py` would currently `float(None)` and drop the
  reading. Default `0` (every reading Good, as before).

### Cleanup

- Removed the unused `uamethod` import and the dead `instrument_type` field;
  `Instrument` is now a small dataclass.
- Per-reading log line moved from `INFO` to `DEBUG`.
- `__main__` honours `LOG_LEVEL` instead of hard-coding `DEBUG` and asyncio
  debug mode. (`serve.py` still configures the root logger first, so nothing
  changes when run through it.)

## Not done (possible follow-ups)

- **Coupled signals.** Real reactor variables move together — DO falls as OD600
  rises (oxygen uptake), pH drifts down with lactate and sawtooths on base
  addition, glucose is consumed. Today each sensor is independent.
- **Batch phases.** A run has a timeline (lag → exponential → stationary, feed
  events, harvest); sensors are currently in a memoryless steady state forever.
- **OPC-UA fidelity.** Expose `EngineeringUnits` / `EURange` as standard
  `AnalogItemType` properties (the web app currently hard-codes units), and a
  real `BioreactorType` object type.
- **Structure.** One `asyncio` task per sensor instead of the 10 ms poll loop;
  move the ~250 lines of reactor definitions to a YAML/JSON config.

## Note for the web app

Because the signals are now tight around their setpoints, the dashboard's
**Push a fake reading** button (`sigmas: 10`, scaled by the signal's *own*
recent scatter) produces a much smaller absolute excursion than it used to. It
still trips the anomaly detector for genuinely large `sigmas`; for a reliably
visible demo spike, raise `sigmas` or rescale `/api/debug/inject` against the
detector's own threshold rather than the raw signal scatter.
