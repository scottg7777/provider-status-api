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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from clients.http_util import new_async_client
from clients.npi_util import validate_npi
from clients import stedi as stedi_client
from clients.stedi import is_configured
from eligibility import mock_catalog, pick_payer, provider_prefill, run_readiness_check, _payer_option
from models import BatchRequest, EligibilityReadinessRequest, HealthResponse
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


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo submitted values (member ID, names, DOB) back in validation errors.
    detail = [
        {"loc": list(err.get("loc") or []), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


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


@app.get("/payers/search")
async def payers_search(
    request: Request,
    q: str = Query(..., min_length=2, max_length=120, description="Payer name, Stedi payer ID, or alias"),
) -> dict[str, Any]:
    """Stedi Payer Network search (typeahead for the eligibility readiness check)."""
    if not is_configured():
        return {"configured": False, "query": q, "results": [], "message": stedi_client.NOT_CONFIGURED_MESSAGE}
    try:
        matches = await stedi_client.search_payers(request.app.state.http, q, limit=15)
    except PermissionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # httpx errors
        raise HTTPException(status_code=502, detail=f"Stedi payer search failed: {exc.__class__.__name__}") from exc
    return {"configured": True, "query": q, "results": [_payer_option(m) for m in matches]}


@app.get("/eligibility-readiness/prefill")
async def eligibility_prefill(request: Request, npi: str = Query(...)) -> dict[str, Any]:
    """Provider name from NPPES, used to prefill the eligibility readiness form."""
    try:
        npi = validate_npi(npi)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await provider_prefill(request.app.state.http, npi)


@app.get("/eligibility-readiness/mock-requests")
async def eligibility_mock_requests() -> dict[str, Any]:
    """Stedi-documented PHI-free mock eligibility requests (work with a Stedi test API key only)."""
    return mock_catalog()


@app.post("/eligibility-readiness")
async def eligibility_readiness(payload: EligibilityReadinessRequest, request: Request) -> dict[str, Any]:
    """Send one Stedi real-time 270 under the NPI and classify whether the payer recognizes the provider.

    Classifications: RECOGNIZED, PROVIDER_NOT_RECOGNIZED, PATIENT_ISSUE, PAYER_UNAVAILABLE,
    INCONCLUSIVE (plus PAYER_AMBIGUOUS when the payer search needs a pick). Member data is
    not logged or stored, and the response omits member name/DOB and masks the member ID.
    """
    return await run_readiness_check(request.app.state.http, payload.model_dump())


@app.get("/search/name")
async def search_name(
    request: Request,
    q: str = Query(..., min_length=2, max_length=120, description="Practice or provider name, e.g. 'Aeterna Aesthetics' or 'Dr. Christine Snoke'"),
    state: str | None = Query(None, min_length=2, max_length=2, description="2-letter state"),
    city: str | None = Query(None, max_length=60),
    kind: str = Query("auto", pattern="^(auto|organization|individual)$"),
    limit: int = Query(25, ge=1, le=50),
) -> dict[str, Any]:
    """Friendly name -> probable NPI matches from the CMS NPPES NPI Registry."""
    from name_search import search_by_name

    return await search_by_name(request.app.state.http, q, state=state, city=(city or "").strip() or None, kind=kind, limit=limit)


@app.get("/search/affiliated")
async def search_affiliated(request: Request, npi: str = Query(...)) -> dict[str, Any]:
    """Individual NPIs likely affiliated with an organization NPI (same address/phone, authorized official)."""
    from name_search import find_affiliated, nppes_search, summarize_result

    try:
        npi = validate_npi(npi)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows, error = await nppes_search(request.app.state.http, {"number": npi}, limit=1)
    if error or not rows:
        raise HTTPException(status_code=404, detail=error or "NPI not found in NPPES")
    org = summarize_result(rows[0])
    if org["type"] != "organization":
        raise HTTPException(status_code=400, detail="Affiliated-provider search needs an organization (NPI-2)")
    return await find_affiliated(request.app.state.http, org)


@app.get("/readiness-report")
async def readiness_report(request: Request, npi: str = Query(...)) -> dict[str, Any]:
    """Readiness-to-bill report: all public sources + Stedi enrollment/payer support, with verdicts."""
    from readiness import build_report

    try:
        npi = validate_npi(npi)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await build_report(request.app.state.http, npi)
