"""Container entrypoint for the simulator.

`server_bioreactor.py`'s own `__main__` block hard-codes DEBUG logging, which is
a firehose in a container. We configure the root logger first (a later
`logging.basicConfig` is then a no-op), honouring LOG_LEVEL, and hand off.
"""

import logging
import os
import pathlib
import runpy

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
)
logging.getLogger("asyncua").setLevel(os.getenv("ASYNCUA_LOG_LEVEL", "WARNING").upper())

runpy.run_path(
    str(pathlib.Path(__file__).with_name("server_bioreactor.py")),
    run_name="__main__",
)
