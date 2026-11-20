"""FastAPI service for combined provider / payer status lookups."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import base64
import os
import secrets


def _load_local_env() -> None:
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v:
            os.environ.setdefault(k, v)


_load_local_env()
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from clients.http_util import new_async_client
from clients.npi_util import validate_npi
from clients.stedi import is_configured
from models import BatchRequest, HealthResponse
from service import lookup_batch, lookup_provider_status, payers_catalog

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with new_async_client() as client:
        app.state.http = client
        yield


app = FastAPI(
    title="Provider Status API",
    description=(
        "Combines CMS NPPES, Medicare PECOS public enrollment, live public "
        "Plan-Net FHIR directories, and optional Stedi EDI transaction enrollment."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def password_gate(request: Request, call_next):
    expected = os.environ.get("APP_PASSWORD", "").strip()
    if not expected or request.url.path == "/health":
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    authorized = False
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
            _, _, password = decoded.partition(":")
            authorized = secrets.compare_digest(password, expected)
        except Exception:
            authorized = False
    elif auth.lower().startswith("bearer "):
        authorized = secrets.compare_digest(auth[7:].strip(), expected)

    if not authorized:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Provider Status"'},
        )
    return await call_next(request)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", response_model=HealthResponse)
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "provider-status-api",
        "stedi_configured": is_configured(),
    }


@app.get("/payers")
async def payers() -> dict[str, Any]:
    return payers_catalog()


@app.get("/provider-status")
async def provider_status(
    request: Request,
    npi: str = Query(..., description="10-digit National Provider Identifier"),
    payer: str | None = Query(None, description="Optional payer name or ID (e.g. cigna, humana, medicare)"),
    tin: str | None = Query(None, description="Optional provider tax ID for Stedi enrollment filters"),
) -> dict[str, Any]:
    try:
        npi = validate_npi(npi)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await lookup_provider_status(request.app.state.http, npi, payer=payer, tin=tin)


@app.post("/provider-status/batch")
async def provider_status_batch(payload: BatchRequest, request: Request) -> dict[str, Any]:
    items = [item.model_dump() for item in payload.items]
    results = await lookup_batch(request.app.state.http, items)
    return {"count": len(results), "results": results}
