from fastapi import FastAPI

from .routes import files, stream, markers, export, hidden

app = FastAPI(title="8-cut Server")

app.include_router(files.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(markers.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(hidden.router, prefix="/api")
