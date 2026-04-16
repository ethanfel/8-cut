import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

_connections: list[WebSocket] = []


async def connect(ws: WebSocket):
    await ws.accept()
    _connections.append(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        _connections.remove(ws)


def broadcast(msg: dict):
    """Send a message to all connected WebSocket clients.

    Called from sync code (export callbacks), so we schedule the coroutine
    on each connection's event loop.
    """
    data = json.dumps(msg)
    stale = []
    for ws in _connections:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
            else:
                loop.run_until_complete(ws.send_text(data))
        except Exception:
            stale.append(ws)
    for ws in stale:
        _connections.remove(ws)
