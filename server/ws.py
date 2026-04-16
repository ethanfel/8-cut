import asyncio
import json
import threading

from fastapi import WebSocket, WebSocketDisconnect

_lock = threading.Lock()
_connections: list[WebSocket] = []
_loop: asyncio.AbstractEventLoop | None = None


async def connect(ws: WebSocket):
    global _loop
    _loop = asyncio.get_running_loop()
    await ws.accept()
    with _lock:
        _connections.append(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        with _lock:
            if ws in _connections:
                _connections.remove(ws)


def broadcast(msg: dict):
    """Send a message to all connected WebSocket clients.

    Called from sync code (export callbacks running in background threads),
    so we schedule sends on uvicorn's event loop.
    """
    if _loop is None:
        return
    data = json.dumps(msg)
    with _lock:
        for ws in list(_connections):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(data), _loop)
            except Exception:
                pass
