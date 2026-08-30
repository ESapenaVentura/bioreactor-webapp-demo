import asyncio


class ConnectionManager:
    def __init__(self):
        self._connections = set()

    def __len__(self) -> int:
        return len(self._connections)

    async def connect(self, websocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        for websocket in list(self._connections):     # iterate a COPY
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(websocket)


async def broadcast_loop(hub, store, status, hz: float) -> None:
    interval = 1.0 / hz
    while True:
        await asyncio.sleep(interval)
        if len(hub) and store.sensors:
            await hub.broadcast(store.tick(status.get("connected", False)))
