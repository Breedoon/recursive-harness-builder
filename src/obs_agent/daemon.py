"""FastAPI server for OBS Agent daemon."""

from fastapi import FastAPI

app = FastAPI(title="OBS Agent", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}
