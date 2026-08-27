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
        self._names = names
        self._queue = queue
        self.dropped = 0

    async def datachange_notification(self, node, value, data):
        category, sensor = self._names[node]
        dv = data.monitored_item.Value
        reading = Reading(
            reactor=config.REACTOR,
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


async def run_source(queue) -> None:
    """Subscribe to every sensor on the configured reactor. Runs until cancelled."""
    client = Client(
        url=config.SERVER_URL,
        auto_reconnect=True,
        reconnect_max_delay=config.RECONNECT_MAX_SECONDS,
    )
    async with client:
        idx = await client.get_namespace_index(uri=config.NAMESPACE_URI)
        reactor = await client.nodes.objects.get_child(f"{idx}:{config.REACTOR}")
        sensors = await reactor.get_children()

        names = {}
        for sensor in sensors:
            display = await sensor.read_display_name()
            names[sensor] = split_display_name(display.Text)

        handler = _SubHandler(names, queue)
        subscription = await client.create_subscription(
            config.PUBLISHING_INTERVAL_MS, handler
        )
        await subscription.subscribe_data_change(sensors)
        log.info("subscribed to %d sensors on %s", len(sensors), config.REACTOR)

        try:
            await asyncio.Event().wait()          # park until cancelled
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(subscription.delete(), timeout=2)
            except Exception:
                pass                              # server already gone
            raise


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