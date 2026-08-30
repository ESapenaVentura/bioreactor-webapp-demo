import os
import time
import asyncio
import logging

from asyncua import Server, Node
from asyncua.common.methods import uamethod

import numpy as np
from dataclasses import dataclass
from enum import Enum

_logger = logging.getLogger(__name__)
SERVER_NAME = "Ganymede OPC-UA Server"
SERVER_URI = "http://examples.ganymede.github.io"
# 0.0.0.0 binds every interface; override only if you need the advertised
# endpoint URL to carry a specific hostname.
SERVER_URL = os.getenv("SERVER_URL", "opc.tcp://0.0.0.0:4840/ganymede/server/")


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


@dataclass
class BrownianMotionSensor(Sensor):
    """Brownian motion model for sensor value

    Parameters
    ----------
    Sensor : Sensor
        Sensor object

    Notes
    -----
    # dN/dt = mu + sigma * chi(t)
    """

    value_std: float = 1e-2
    value_drift: float = 0.0


@dataclass
class LogisticSensor(Sensor):
    """Stochastic logistic growth model for sensor value

    Parameters
    ----------
    Sensor : Sensor
        Sensor object

    Notes
    -----
    # if additive, dN/dt = rN(1 - N/K) + sigma * chi(t)
    # if multiplicative, dN/dt = rN(1 - N/K) + sigma * N * chi(t)
    #
    # Where
    # N is the value of the sensor,
    # r is the growth rate,
    # K is the carrying capacity,
    # sigma is the standard deviation of the noise, and
    # chi(t) is a white noise process
    """

    noise_type: NoiseType = NoiseType.MULTIPLICATIVE
    value_growth_rate: float = 1e-3  # growth rate per hour
    value_std: float = 0.1


class Instrument:
    def __init__(
        self,
        obj: str,
        name: str,
        vars: list[BrownianMotionSensor | LogisticSensor],
        instrument_type: str,
    ):
        """
        Bioreactor object

        Parameters
        ----------
        obj
            OPC-UA object
        name: str
            Name of instrument
        vars : dict[str, Sensor]
            Sensors on instrument, key is sensor name, value is Sensor object
        instrument_type : str
            Type of instrument
        """
        self.obj = obj
        self.name = name
        self.vars = vars


async def create_bioreactor(
    server,
    idx: int,
    bioreactor_name: str,
    sensors: list[BrownianMotionSensor | LogisticSensor],
) -> Instrument:

    obj = await server.nodes.objects.add_object(idx, bioreactor_name)

    sensor_list = []
    for sensor in sensors:
        sensor_var = await obj.add_variable(
            idx, f"{sensor.sensor_type} - {sensor.sensor_name}", sensor.value_init
        )
        await sensor_var.set_writable()

        s: BrownianMotionSensor | LogisticSensor

        if isinstance(sensor, BrownianMotionSensor):
            s = BrownianMotionSensor(
                sensor_name=sensor.sensor_name,
                sensor_type=sensor.sensor_type,
                value_init=sensor.value_init,
                value_drift=sensor.value_drift,
                value_min=sensor.value_min,
                value_max=sensor.value_max,
                value_min_limit=sensor.value_min_limit,
                value_max_limit=sensor.value_max_limit,
                reporting_interval=sensor.reporting_interval,
                sensor_node=sensor_var,
            )
        elif isinstance(sensor, LogisticSensor):
            s = LogisticSensor(
                sensor_name=sensor.sensor_name,
                sensor_type=sensor.sensor_type,
                value_init=sensor.value_init,
                value_growth_rate=sensor.value_growth_rate,
                value_min_limit=sensor.value_min_limit,
                value_max_limit=sensor.value_max_limit,
                reporting_interval=sensor.reporting_interval,
                sensor_node=sensor_var,
                noise_type=sensor.noise_type,
            )
        else:
            raise ValueError("Sensor type not supported")
        sensor_list.append(s)

    return Instrument(
        obj=obj, name=bioreactor_name, vars=sensor_list, instrument_type="bioreactor"
    )


async def main():
    # setup our server
    server = Server()
    await server.init()
    server.set_endpoint(SERVER_URL)
    server.set_server_name(SERVER_NAME)

    # set up our own namespace, not really necessary but should as spec
    idx = await server.register_namespace(SERVER_URI)

    obj_cytiva_wave = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Cytiva Wave",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=35.0,
                value_max=40.0,
                reporting_interval=5.5,
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=7.0,
                value_std=0.1,
                value_min=6.8,
                value_max=7.2,
                reporting_interval=7.1,
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=98.0,
                value_std=0.1,
                value_min=97.0,
                value_max=100.0,
                reporting_interval=3.3,
            ),
            BrownianMotionSensor(
                sensor_name="Agitation Speed",
                sensor_type="Process",
                value_init=400.0,
                value_std=1,
                value_min=0.0,
                value_max=1000.0,
                reporting_interval=4.5,
            ),
            BrownianMotionSensor(
                sensor_name="Air Flow Rate",
                sensor_type="Process",
                value_init=0.5,
                value_std=0.005,
                value_min=0.0,
                value_max=1.0,
                reporting_interval=6.0,
            ),
            LogisticSensor(
                sensor_name="OD600",
                sensor_type="Cell Growth",
                value_init=0.05,
                value_growth_rate=0.3,
                value_max=2,
                value_std=1e-6,
                reporting_interval=1.1,
                noise_type=NoiseType.ADDITIVE,
            ),
            BrownianMotionSensor(
                sensor_name="Glucose Concentration",
                sensor_type="Cell Growth",
                value_init=20.0,
                value_std=0.5,
                value_min=0.0,
                value_max=100.0,
                reporting_interval=8.7,
            ),
            BrownianMotionSensor(
                sensor_name="Lactate Concentration",
                sensor_type="Cell Growth",
                value_init=0.1,
                value_std=0.2,
                value_min=0.0,
                value_max=10.0,
                reporting_interval=10.3,
            ),
        ],
    )

    obj_cytiva_xdr = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Cytiva XDR",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=35.0,
                value_max=39.0,
                reporting_interval=2.0,
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=6.2,
                value_std=0.1,
                value_min=6.0,
                value_max=6.4,
                reporting_interval=5.1,
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=99.1,
                value_std=0.1,
                value_min=98.0,
                value_max=100.0,
                reporting_interval=15.2,
            ),
        ],
    )

    obj_sartorius_ambr = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Sartorius AMBR",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=0.0,
                value_max=41.0,
                reporting_interval=2.5,
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=5.7,
                value_std=0.1,
                value_min=5.5,
                value_max=5.9,
                reporting_interval=8.8,
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=100.0,
                value_std=0.1,
                value_min=99.0,
                value_max=100.0,
                reporting_interval=10.9,
            ),
        ],
    )

    obj_sartorius_biostat = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Sartorius Biostat",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=0.0,
                value_max=41.0,
                reporting_interval=2.5,
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=5.7,
                value_std=0.1,
                value_min=5.5,
                value_max=5.9,
                reporting_interval=8.8,
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=100.0,
                value_std=0.1,
                value_min=99.0,
                value_max=100.0,
                reporting_interval=10.9,
            ),
        ],
    )

    obj_eppendorf_bioflo = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Eppendorf BioFlo",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=0.0,
                value_max=41.0,
                reporting_interval=2.5,
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=5.7,
                value_std=0.1,
                value_min=5.5,
                value_max=5.9,
                reporting_interval=8.8,
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=100.0,
                value_std=0.1,
                value_min=99.0,
                value_max=100.0,
                reporting_interval=10.9,
            ),
        ],
    )

    obj_thermo_fisher_hyperforma = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Thermo Fisher HyPerforma",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=0.0,
                value_max=41.0,
                reporting_interval=np.random.uniform(1.0, 20.0),
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=5.7,
                value_std=0.1,
                value_min=5.5,
                value_max=5.9,
                reporting_interval=np.random.uniform(1.0, 20.0),
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=100.0,
                value_std=0.1,
                value_min=99.0,
                value_max=100.0,
                reporting_interval=np.random.uniform(1.0, 20.0),
            ),
        ],
    )

    obj_millipore_sigma_mobius = await create_bioreactor(
        server=server,
        idx=idx,
        bioreactor_name="Millipore Sigma Mobius",
        sensors=[
            BrownianMotionSensor(
                sensor_name="Temperature",
                value_init=36.5,
                value_std=0.1,
                value_min=0.0,
                value_max=41.0,
                reporting_interval=np.random.uniform(1.0, 20.0),
            ),
            BrownianMotionSensor(
                sensor_name="pH",
                value_init=5.7,
                value_std=0.1,
                value_min=5.5,
                value_max=5.9,
                reporting_interval=np.random.uniform(1.0, 20.0),
            ),
            BrownianMotionSensor(
                sensor_name="DO",
                value_init=100.0,
                value_std=0.1,
                value_min=99.0,
                value_max=100.0,
                reporting_interval=np.random.uniform(1.0, 20.0),
            ),
        ],
    )

    bioreactors = [
        obj_cytiva_wave,
        obj_cytiva_xdr,
        obj_sartorius_ambr,
        obj_sartorius_biostat,
        obj_eppendorf_bioflo,
        obj_thermo_fisher_hyperforma,
        obj_millipore_sigma_mobius,
    ]
    last_report_times = {
        f"{bioreactor.name}: {sensor.sensor_name}": time.perf_counter()
        for bioreactor in bioreactors
        for sensor in bioreactor.vars
    }

    _logger.info("Starting server!")
    async with server:
        while True:
            current_time = time.perf_counter()

            for bioreactor in bioreactors:
                for sensor in bioreactor.vars:
                    last_report_time = last_report_times[
                        f"{bioreactor.name}: {sensor.sensor_name}"
                    ]
                    elapsed_time = current_time - last_report_time

                    if elapsed_time >= sensor.reporting_interval:
                        sensor_node_value = await sensor.sensor_node.get_value()

                        if isinstance(sensor, LogisticSensor):
                            noise_multiplier = (
                                sensor_node_value
                                if sensor.noise_type == NoiseType.MULTIPLICATIVE
                                else 1
                            )

                            deterministic_growth = (
                                (sensor.value_growth_rate / 60 / 60)
                                * sensor_node_value
                                * (1 - sensor_node_value / sensor.value_max)
                                * (sensor.reporting_interval / 60)
                            )
                            noise = (
                                noise_multiplier
                                * sensor.value_std
                                * np.random.normal(
                                    0, np.sqrt(sensor.reporting_interval / 60)
                                )
                            )

                            new_val = float(
                                np.minimum(
                                    np.maximum(
                                        sensor_node_value
                                        + deterministic_growth
                                        + noise,
                                        sensor.value_min,
                                    ),
                                    sensor.value_max,
                                )
                            )
                        else:
                            new_val = float(
                                np.maximum(
                                    np.minimum(
                                        sensor_node_value
                                        * np.random.normal(
                                            1.0,
                                            sensor.value_std,
                                        )
                                        + sensor.value_drift,
                                        sensor.value_max,
                                    ),
                                    sensor.value_min,
                                )
                            )

                        await sensor.sensor_node.write_value(new_val)

                        _logger.info(
                            "Bioreactor %s, Sensor %s updated from %.4f to %.4f",
                            bioreactor.name,
                            f"{sensor.sensor_type}: {sensor.sensor_name}",
                            sensor_node_value,
                            new_val,
                        )

                        last_report_times[
                            f"{bioreactor.name}: {sensor.sensor_name}"
                        ] = current_time

            await asyncio.sleep(0.01)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main(), debug=True)
