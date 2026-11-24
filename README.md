# CineMind AI

A learning project for AI Systems Engineering. This milestone contains only a
small FastAPI backend and one endpoint test. The `backend/cinemind` package can
grow into a modular monolith as concrete features are added.

## Setup (Windows PowerShell)

Requires an existing Python 3.14 installation. Run these commands from the
project folder:

```powershell
Set-Location C:\Users\G\CineMindAI
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv==0.13.9
.\.venv\Scripts\uv.exe sync --locked --python .\.venv\Scripts\python.exe
```

Dependencies and the pinned `uv` tool stay inside the project-local `.venv`.
Activation is unnecessary, so PowerShell execution-policy changes are not needed.
`pyproject.toml` declares dependencies; `uv.lock` records the exact resolved
versions, including transitive dependencies. Use `uv sync --locked` to reproduce
them. The ignored `.uv-cache` stores local download/build caches. Setup downloads
free packages from PyPI; no cloud resources or AI API calls are used. Existing
Python installations are not changed, and automatic Python downloads are disabled.

## Run

```powershell
.\.venv\Scripts\python.exe -m uvicorn cinemind.main:app --app-dir backend --host 127.0.0.1 --port 8001
```

In a second PowerShell terminal:

```powershell
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8001/health
```

`GET /health` returns HTTP `200` with JSON `{"status":"ok"}`. Uvicorn receives
the HTTP request, FastAPI routes it to `health()`, and FastAPI serializes the
returned dictionary into JSON. This endpoint reports liveness only; it does not
check database or AI-provider readiness. Stop the server with `Ctrl+C`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The single endpoint test checks both the HTTP status and the complete JSON body.

No application configuration is used yet, so there is no `.env.example`.
Database infrastructure, authentication, document features, AI components,
frontend, Docker, and deployment are outside this milestone. Core AI engineering
will be implemented by the learner with mentor guidance.
