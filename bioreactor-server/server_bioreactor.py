"""Bioreactor OPC-UA simulator.

Originally copied from Ganymede's OPC-UA demo repo; see README.md for the list
of local changes. Each sensor is a small stochastic process whose value is
written to an OPC-UA variable node named ``"<Category> - <Sensor>"`` under one
object per reactor.

Environment variables:
  SERVER_URL   endpoint to bind (default opc.tcp://0.0.0.0:4840/ganymede/server/)
  SEED         int seed for the RNG; makes the generated signals reproducible
               across restarts (default: non-deterministic)
  FAULT_RATE   0..1 — fraction of readings emitted with a Bad OPC-UA
               StatusCode, so a client's bad-quality handling gets exercised
               (default 0 = every reading is Good)
  LOG_LEVEL    logging level when run directly (default INFO)
"""

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

import numpy as np
from asyncua import Node, Server, ua

_logger = logging.getLogger(__name__)

SERVER_NAME = "Ganymede OPC-UA Server"
SERVER_URI = "http://examples.ganymede.github.io"
# 0.0.0.0 binds every interface; override only if you need the advertised
# endpoint URL to carry a specific hostname.
SERVER_URL = os.getenv("SERVER_URL", "opc.tcp://0.0.0.0:4840/ganymede/server/")

_seed = os.getenv("SEED")
RNG = np.random.default_rng(int(_seed) if _seed not in (None, "") else None)

FAULT_RATE = max(0.0, min(1.0, float(os.getenv("FAULT_RATE", "0") or 0.0)))


class NoiseType(Enum):
    ADDITIVE = 1
    MULTIPLICATIVE = 2


@dataclass
class Sensor:
    sensor_name: str
    sensor_type: str = "Time Series"
    value_init: float = 0.0
    value_min_limit: float = 0.0
    value_max_limit: float = 100.0
    value_min: float = 0.0
    value_max: float = 100.0
    reporting_interval: float = 10.0
    sensor_node: Node | None = None

    def _clamp(self, v: float) -> float:
        return float(min(max(v, self.value_min), self.value_max))


@dataclass
class BrownianMotionSensor(Sensor):
    """Ornstein-Uhlenbeck process: the value is pulled back toward a setpoint
    (``value_init``, optionally ramping at ``value_drift`` units/second) with
    Gaussian process noise whose *stationary* standard deviation is
    ``value_std``, expressed in the sensor's own engineering units.

    This models a closed-loop-controlled process variable — it hugs its
    setpoint and returns to it after a disturbance. (The original model was a
    multiplicative random walk that wandered off until it hit a clamp and stuck
    there, and whose step size scaled with the absolute value.)

    ``reversion_seconds`` is the OU relaxation time: roughly how long it takes to
    pull a disturbance ~63% of the way back to setpoint.
    """

    value_std: float = 0.01
    value_drift: float = 0.0
    reversion_seconds: float = 90.0

    def step(self, dt: float, age: float) -> float:
        setpoint = self.value_init + self.value_drift * age
        theta = 1.0 / max(self.reversion_seconds, 1e-6)
        decay = math.exp(-theta * dt)                       # exact OU discretisation
        noise_sd = self.value_std * math.sqrt(max(1.0 - decay * decay, 0.0))
        nxt = setpoint + (self.value - setpoint) * decay + noise_sd * RNG.standard_normal()
        self.value = self._clamp(nxt)
        return self.value


@dataclass
class LogisticSensor(Sensor):
    """Stochastic logistic growth: ``dX = r·X·(1 - X/K)·dt + noise``, with the
    noise scaled by ``sqrt(dt)`` so it behaves as a Wiener process no matter the
    reporting interval. ``value_max`` is the carrying capacity K, ``value_std``
    the noise intensity, ``value_growth_rate`` is r in per-hour units.
    """

    noise_type: NoiseType = NoiseType.MULTIPLICATIVE
    value_growth_rate: float = 1e-3  # growth rate per hour
    value_std: float = 0.1

    def step(self, dt: float, age: float) -> float:
        x = self.value
        r = self.value_growth_rate / 3600.0                 # per hour -> per second
        growth = r * x * (1.0 - x / self.value_max) * dt
        scale = x if self.noise_type == NoiseType.MULTIPLICATIVE else 1.0
        noise = scale * self.value_std * math.sqrt(dt) * RNG.standard_normal()
        self.value = self._clamp(x + growth + noise)
        return self.value


@dataclass
class Instrument:
    node: Node
    name: str
    vars: list


async def create_bioreactor(
    server: Server,
    idx: int,
    bioreactor_name: str,
    sensors: list[BrownianMotionSensor | LogisticSensor],
) -> Instrument:
    obj = await server.nodes.objects.add_object(idx, bioreactor_name)

    live: list[BrownianMotionSensor | LogisticSensor] = []
    for spec in sensors:
        node = await obj.add_variable(
            idx, f"{spec.sensor_type} - {spec.sensor_name}", float(spec.value_init)
        )
        await node.set_writable()
        # replace() copies EVERY field of the spec (the old hand-written rebuild
        # silently dropped value_std, and value_max / value_min for logistic
        # sensors, so every sensor ran at the dataclass defaults).
        sensor = replace(spec, sensor_node=node)
        sensor.value = float(spec.value_init)               # live state
        live.append(sensor)

    return Instrument(node=obj, name=bioreactor_name, vars=live)


async def _publish(node: Node, value: float) -> None:
    """Write one reading. Most are Good; a FAULT_RATE fraction carry an
    ``Uncertain`` StatusCode (a questionable-but-present value, like a fouled or
    drifting probe) so a client's bad-quality path gets exercised. We keep the
    value on the DataValue — a full ``Bad`` status nulls it, which naive clients
    don't expect."""
    if FAULT_RATE and RNG.random() < FAULT_RATE:
        await node.write_value(
            ua.DataValue(
                Value=ua.Variant(float(value), ua.VariantType.Double),
                StatusCode=ua.StatusCode(ua.StatusCodes.UncertainSensorNotAccurate),
                SourceTimestamp=datetime.now(timezone.utc),
            )
        )
    else:
        await node.write_value(float(value))


async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(SERVER_URL)
    server.set_server_name(SERVER_NAME)

    # our own namespace, per spec
    idx = await server.register_namespace(SERVER_URI)

    bioreactors = [
        await create_bioreactor(
            server, idx, "Cytiva Wave",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=35.0, value_max=40.0, reporting_interval=5.5,
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=7.0, value_std=0.1,
                    value_min=6.8, value_max=7.2, reporting_interval=7.1,
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=98.0, value_std=0.1,
                    value_min=97.0, value_max=100.0, reporting_interval=3.3,
                ),
                BrownianMotionSensor(
                    sensor_name="Agitation Speed", sensor_type="Process",
                    value_init=400.0, value_std=1.0,
                    value_min=0.0, value_max=1000.0, reporting_interval=4.5,
                ),
                BrownianMotionSensor(
                    sensor_name="Air Flow Rate", sensor_type="Process",
                    value_init=0.5, value_std=0.005,
                    value_min=0.0, value_max=1.0, reporting_interval=6.0,
                ),
                LogisticSensor(
                    sensor_name="OD600", sensor_type="Cell Growth",
                    value_init=0.05, value_growth_rate=0.3, value_max=2.0,
                    value_std=1e-6, reporting_interval=1.1,
                    noise_type=NoiseType.ADDITIVE,
                ),
                BrownianMotionSensor(
                    sensor_name="Glucose Concentration", sensor_type="Cell Growth",
                    value_init=20.0, value_std=0.5,
                    value_min=0.0, value_max=100.0, reporting_interval=8.7,
                ),
                BrownianMotionSensor(
                    sensor_name="Lactate Concentration", sensor_type="Cell Growth",
                    value_init=0.1, value_std=0.2,
                    value_min=0.0, value_max=10.0, reporting_interval=10.3,
                ),
            ],
        ),
        await create_bioreactor(
            server, idx, "Cytiva XDR",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=35.0, value_max=39.0, reporting_interval=2.0,
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=6.2, value_std=0.1,
                    value_min=6.0, value_max=6.4, reporting_interval=5.1,
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=99.1, value_std=0.1,
                    value_min=98.0, value_max=100.0, reporting_interval=15.2,
                ),
            ],
        ),
        await create_bioreactor(
            server, idx, "Sartorius AMBR",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=0.0, value_max=41.0, reporting_interval=2.5,
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=5.7, value_std=0.1,
                    value_min=5.5, value_max=5.9, reporting_interval=8.8,
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=100.0, value_std=0.1,
                    value_min=99.0, value_max=100.0, reporting_interval=10.9,
                ),
            ],
        ),
        await create_bioreactor(
            server, idx, "Sartorius Biostat",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=0.0, value_max=41.0, reporting_interval=2.5,
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=5.7, value_std=0.1,
                    value_min=5.5, value_max=5.9, reporting_interval=8.8,
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=100.0, value_std=0.1,
                    value_min=99.0, value_max=100.0, reporting_interval=10.9,
                ),
            ],
        ),
        await create_bioreactor(
            server, idx, "Eppendorf BioFlo",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=0.0, value_max=41.0, reporting_interval=2.5,
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=5.7, value_std=0.1,
                    value_min=5.5, value_max=5.9, reporting_interval=8.8,
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=100.0, value_std=0.1,
                    value_min=99.0, value_max=100.0, reporting_interval=10.9,
                ),
            ],
        ),
        await create_bioreactor(
            server, idx, "Thermo Fisher HyPerforma",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=0.0, value_max=41.0,
                    reporting_interval=RNG.uniform(1.0, 20.0),
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=5.7, value_std=0.1,
                    value_min=5.5, value_max=5.9,
                    reporting_interval=RNG.uniform(1.0, 20.0),
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=100.0, value_std=0.1,
                    value_min=99.0, value_max=100.0,
                    reporting_interval=RNG.uniform(1.0, 20.0),
                ),
            ],
        ),
        await create_bioreactor(
            server, idx, "Millipore Sigma Mobius",
            [
                BrownianMotionSensor(
                    sensor_name="Temperature",
                    value_init=36.5, value_std=0.1,
                    value_min=0.0, value_max=41.0,
                    reporting_interval=RNG.uniform(1.0, 20.0),
                ),
                BrownianMotionSensor(
                    sensor_name="pH",
                    value_init=5.7, value_std=0.1,
                    value_min=5.5, value_max=5.9,
                    reporting_interval=RNG.uniform(1.0, 20.0),
                ),
                BrownianMotionSensor(
                    sensor_name="DO",
                    value_init=100.0, value_std=0.1,
                    value_min=99.0, value_max=100.0,
                    reporting_interval=RNG.uniform(1.0, 20.0),
                ),
            ],
        ),
    ]

    n_sensors = sum(len(b.vars) for b in bioreactors)
    _logger.info(
        "serving %d reactors / %d sensors  (seed=%s, fault_rate=%s)",
        len(bioreactors), n_sensors, _seed or "random", FAULT_RATE,
    )

    start = time.perf_counter()
    last_report = {id(s): start for b in bioreactors for s in b.vars}

    async with server:
        while True:
            now = time.perf_counter()
            for bioreactor in bioreactors:
                for sensor in bioreactor.vars:
                    dt = now - last_report[id(sensor)]
                    if dt < sensor.reporting_interval:
                        continue
                    value = sensor.step(dt, age=now - start)
                    await _publish(sensor.sensor_node, value)
                    last_report[id(sensor)] = now
                    _logger.debug(
                        "%s / %s -> %.4f", bioreactor.name, sensor.sensor_name, value
                    )
            await asyncio.sleep(0.05)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    asyncio.run(main())
