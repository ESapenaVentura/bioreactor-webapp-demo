import asyncio
import logging

from asyncua import Client

from app import config
from app.models import Reading

log = logging.getLogger(__name__)


def split_display_name(text: str) -> tuple[str, str]:
    """'Time Series - Temperature' -> ('Time Series', 'Temperature')."""
    if " - " not in text:
        return "Unknown", text.strip()
    category, sensor = text.split(" - ", 1)
    return category.strip(), sensor.strip()


class _SubHandler:
    """Runs inline with subscription dispatch — must not block."""

    def __init__(self, names, queue):
        self._names = names          # node -> (reactor, category, sensor)
        self._queue = queue
        self.dropped = 0

    async def datachange_notification(self, node, value, data):
        reactor, category, sensor = self._names[node]
        dv = data.monitored_item.Value
        reading = Reading(
            reactor=reactor,
            category=category,
            sensor=sensor,
            value=float(value),
            ts=dv.SourceTimestamp,
            status_ok=dv.StatusCode.is_good(),
        )
        try:
            self._queue.put_nowait(reading)
        except asyncio.QueueFull:
            self.dropped += 1


async def _discover(client):
    """Every reactor on the server -> {sensor_node: (reactor, category, sensor)}.

    A "reactor" is any Objects child that owns sensor nodes, i.e. children whose
    display name matches the "Category - Sensor" pattern. That filter skips the
    server's own housekeeping objects (Server, Aliases, Locations).
    """
    names = {}
    reactors = set()
    for obj in await client.nodes.objects.get_children():
        reactor = (await obj.read_display_name()).Text
        for child in await obj.get_children():
            display = (await child.read_display_name()).Text
            if " - " not in display:
                continue
            category, sensor = split_display_name(display)
            names[child] = (reactor, category, sensor)
            reactors.add(reactor)
    return names, sorted(reactors)


async def run_source(queue, status=None) -> None:
    """Subscribe to every sensor on every reactor. Runs until cancelled.

    If ``status`` is given it is a shared dict the web layer reads: ``connected``
    flips True once the subscription is live and False again on the way out, and
    ``reactors`` lists what was discovered.
    """
    client = Client(
        url=config.SERVER_URL,
        auto_reconnect=True,
        reconnect_max_delay=config.RECONNECT_MAX_SECONDS,
    )
    async with client:
        await client.get_namespace_index(uri=config.NAMESPACE_URI)
        names, reactors = await _discover(client)
        if not names:
            raise RuntimeError("no reactor sensors found on the server")

        sensors = list(names)
        handler = _SubHandler(names, queue)
        subscription = await client.create_subscription(
            config.PUBLISHING_INTERVAL_MS, handler
        )
        await subscription.subscribe_data_change(sensors)
        log.info(
            "subscribed to %d sensors across %d reactors: %s",
            len(sensors), len(reactors), ", ".join(reactors),
        )

        if status is not None:
            status["connected"] = True
            status["last_error"] = None           # a live link is not "broken"
            status["reactors"] = reactors

        try:
            await asyncio.Event().wait()          # park until cancelled
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(subscription.delete(), timeout=2)
            except Exception:
                pass                              # server already gone
            raise
        finally:
            if status is not None:
                status["connected"] = False


async def run_source_forever(queue, status) -> None:
    """Keep a source subscription alive across outages.

    asyncua's auto_reconnect only supervises a link that was already up; it does
    not retry the *initial* connect. So we do, with exponential backoff, and we
    report what happened through the shared ``status`` dict rather than crashing
    the app — a monitor should start whether or not the instrument is up.
    """
    delay = 1.0
    while True:
        try:
            await run_source(queue, status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                  # noqa: BLE001 — report anything
            status["connected"] = False
            status["last_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("source failed (%s) — retrying in %.0fs", exc, delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, float(config.RECONNECT_MAX_SECONDS))
        else:
            delay = 1.0                           # clean exit: reset the backoff


# --------------------------------------------------------------- smoke test

async def _consume(queue, limit: int) -> None:
    """Take Readings off the queue and print them. The other half of the pair."""
    for _ in range(limit):
        reading = await queue.get()
        unit = config.UNITS.get(reading.sensor, "")
        print(
            f"{reading.ts:%H:%M:%S}  {reading.sensor:<24} "
            f"{reading.value:>10.4f} {unit:<6} ok={reading.status_ok}"
        )


async def _smoke_test(limit: int = 15) -> None:
    """python -m app.source — prove the layer works before building on it."""
    queue: asyncio.Queue[Reading] = asyncio.Queue(maxsize=1000)

    producer = asyncio.create_task(run_source(queue))
    consumer = asyncio.create_task(_consume(queue, limit))

    # Whichever finishes first ends the test. If the producer finishes, it can
    # only be because it failed — so surface that instead of letting the
    # consumer sit waiting for readings that are never coming.
    done, _ = await asyncio.wait(
        {producer, consumer}, return_when=asyncio.FIRST_COMPLETED
    )

    for task in (producer, consumer):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    if producer in done and producer.exception() is not None:
        raise producer.exception()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # asyncua logs every publish packet at INFO. Unreadable. Turn it down.
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    asyncio.run(_smoke_test())