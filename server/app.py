from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from core.db import ProcessedDB
from .config import DB_PATH
from .routes import files, stream, markers, export, hidden
from . import ws

app = FastAPI(title="8-cut Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = ProcessedDB(DB_PATH)

app.include_router(files.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(markers.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(hidden.router, prefix="/api")


@app.websocket("/ws/export")
async def export_ws(websocket: WebSocket):
    await ws.connect(websocket)
