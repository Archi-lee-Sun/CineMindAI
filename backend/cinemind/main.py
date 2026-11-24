"""FastAPI entry point and process liveness endpoint."""

from fastapi import FastAPI

app = FastAPI(title="CineMind AI")


@app.get("/health")
async def health() -> dict[str, str]:
    """Report process liveness without checking external dependencies."""
    return {"status": "ok"}
