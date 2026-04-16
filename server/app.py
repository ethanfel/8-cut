from fastapi import FastAPI

from core.db import ProcessedDB
from .config import DB_PATH
from .routes import files, stream, markers, export, hidden

app = FastAPI(title="8-cut Server")

db = ProcessedDB(DB_PATH)

app.include_router(files.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(markers.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(hidden.router, prefix="/api")
