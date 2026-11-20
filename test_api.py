"""Unit tests (mocked Stedi) and live smoke tests (NPPES, PECOS, FHIR)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from clients.npi_util import npi_checksum_ok, validate_npi
from clients.registry import LIVE_FHIR_PAYERS, STEDI_ENROLLMENTS_BASE, STEDI_PAYERS_BASE, live_payer_by_id
from clients.stedi import lookup_enrollments
from clients.http_util import new_async_client
from clients.nppes import lookup_nppes
from clients.pecos import lookup_pecos
from clients.fhir import lookup_payer_network
from service import payers_catalog

# Live-tested Medicare-enrolled internist (confirmed in NPPES + PECOS).
ENKESHAFI_NPI = "1003000126"
# Cigna / Humana / Centene in-network sample.
FONTE_NPI = "1710975354"
# Valid checksum, used for negative / format tests only when needed.
VALID_EXAMPLE_NPI = "1234567893"

KNOWN_LIVE_SAMPLES = {
    "cigna": (ENKESHAFI_NPI, "ENKESHAFI"),
    "unitedhealthcare": ("1437170412", "Lanza"),
    "humana": ("1679052898", "Sanchez"),
    "centene": (ENKESHAFI_NPI, "Enkeshafi"),
    "molina": ("1427458553", "Mowdy"),
    "bcbsri": ("1013231570", "SEIDLER"),
}


def test_npi_checksum_and_validation() -> None:
    assert npi_checksum_ok(ENKESHAFI_NPI)
    assert validate_npi(f"  {ENKESHAFI_NPI}  ") == ENKESHAFI_NPI
    with pytest.raises(ValueError, match="10-digit"):
        validate_npi("123")
    with pytest.raises(ValueError, match="checksum"):
        validate_npi("0000000000")


def test_payers_catalog_includes_live_and_unsupported() -> None:
    catalog = payers_catalog()
    ids = {p["id"] for p in catalog["fhir_payers"]}
    assert {"cigna", "unitedhealthcare", "humana", "centene", "molina", "bcbsri"} <= ids
    unsupported = {p["id"] for p in catalog["unsupported_or_auth_required"]}
    assert {"aetna", "anthem", "kaiser"} <= unsupported
    assert catalog["cms"]["medicare_pecos"]["dataset_id"]
    assert catalog["stedi"]["enrollments_url"].startswith("https://enrollments.us.stedi.com/")


def test_health_and_payers_endpoints() -> None:
    # Ensure a leftover key from the test runner cannot leak into this assertion.
    os.environ.pop("STEDI_API_KEY", None)
    from app import app

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert body["stedi_configured"] is False
        payers = client.get("/payers")
        assert payers.status_code == 200
        assert len(payers.json()["fhir_payers"]) == len(LIVE_FHIR_PAYERS)


def test_invalid_npi_rejected() -> None:
    from app import app

    with TestClient(app) as client:
        response = client.get("/provider-status", params={"npi": "123"})
        assert response.status_code == 400
        assert "10-digit" in response.json()["detail"]


def test_batch_limit() -> None:
    from app import app

    with TestClient(app) as client:
        payload = {"items": [{"npi": ENKESHAFI_NPI}] * 51}
        response = client.post("/provider-status/batch", json=payload)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_stedi_missing_key_returns_configured_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEDI_API_KEY", raising=False)
    async with new_async_client() as client:
        result = await lookup_enrollments(client, ENKESHAFI_NPI, payer="aetna")
    assert result["configured"] is False
    assert "STEDI_API_KEY" in (result["message"] or "")
    assert result["enrollments"] == []


@pytest.mark.asyncio
async def test_stedi_enrollments_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEDI_API_KEY", "test-key-not-real")

    def handler(request: httpx.Request) -> httpx.Response:
        # The key must be used, but tests never print or assert the secret value in output.
        assert request.headers.get("Authorization")
        url = str(request.url)
        if url.startswith(f"{STEDI_PAYERS_BASE}/payers/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "stediId": "CIGNA",
                            "name": "Cigna",
                            "primaryPayerId": "62308",
                            "aliases": ["CIGNA", "SX071"],
                        }
                    ]
                },
            )
        if url.startswith(f"{STEDI_ENROLLMENTS_BASE}/enrollments"):
            assert "providerNpis=1003000126" in url
            assert "payerIds=CIGNA" in url
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "enr-1",
                            "status": "LIVE",
                            "statusLastUpdatedAt": "2026-01-01T00:00:00Z",
                            "payer": {"name": "Cigna", "stediPayerId": "CIGNA"},
                            "provider": {"id": "prov-1", "name": "Test Clinic", "npi": ENKESHAFI_NPI, "taxId": "12-3456789"},
                            "transactions": {"eligibilityCheck": {"enroll": True}, "claimPayment": {"enroll": True}},
                            "tasks": [
                                {
                                    "id": "task-1",
                                    "isComplete": False,
                                    "rank": 1,
                                    "responsibleParty": "PROVIDER",
                                    "definition": {
                                        "manualTask": {"instructions": "Upload W-9", "fields": []}
                                    },
                                }
                            ],
                        },
                        {
                            "id": "enr-2",
                            "status": "PROVIDER_ACTION_REQUIRED",
                            "payer": {"name": "Cigna", "stediPayerId": "CIGNA"},
                            "provider": {"id": "prov-1", "name": "Test Clinic", "npi": ENKESHAFI_NPI},
                            "transactions": {"professionalClaimSubmission": {"enroll": True}},
                            "tasks": [{"id": "task-2", "isComplete": True, "rank": 1, "responsibleParty": "PROVIDER"}],
                        },
                    ]
                },
            )
        return httpx.Response(404, json={"message": "unexpected url"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await lookup_enrollments(client, ENKESHAFI_NPI, payer="cigna")

    assert result["configured"] is True
    assert result["error"] is None
    assert result["enrollment_count"] == 2
    assert "LIVE" in result["statuses"]
    assert "PROVIDER_ACTION_REQUIRED" in result["statuses"]
    assert result["enrollments"][0]["transactions"][0]["code"] in {"eligibilityCheck", "claimPayment"}
    # Tax ID value must not be copied into the response.
    assert "taxId" not in json.dumps(result)
    assert "12-3456789" not in json.dumps(result)
    assert result["open_tasks"][0]["instructions"] == "Upload W-9"
    assert result["matched_payers"][0]["stedi_payer_id"] == "CIGNA"


@pytest.mark.asyncio
async def test_stedi_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEDI_API_KEY", "test-key-not-real")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await lookup_enrollments(client, ENKESHAFI_NPI)
    assert result["configured"] is True
    assert "authentication failed" in (result["error"] or "").lower()


@pytest.mark.asyncio
async def test_live_nppes_enkeshafi() -> None:
    async with new_async_client() as client:
        result = await lookup_nppes(client, ENKESHAFI_NPI)
    assert result["error"] is None
    assert result["found"] is True
    assert result["enumeration_type"] == "NPI-1"
    assert result["active"] is True
    assert result["last_name"] and result["last_name"].upper() == "ENKESHAFI"
    assert result["primary_taxonomy"]
    assert result["practice_addresses"]


@pytest.mark.asyncio
async def test_live_pecos_enkeshafi_enrolled() -> None:
    async with new_async_client() as client:
        result = await lookup_pecos(client, ENKESHAFI_NPI)
    assert result["error"] is None
    assert result["enrolled"] is True
    assert result["enrollment_count"] >= 1
    types = " ".join(result["provider_types"]).upper()
    assert "INTERNAL MEDICINE" in types or "HOSPITALIST" in types
    assert "MD" in result["states"] or "DC" in result["states"] or "VA" in result["states"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payer_id", [p.id for p in LIVE_FHIR_PAYERS])
async def test_live_fhir_payer_sample(payer_id: str) -> None:
    payer = live_payer_by_id(payer_id)
    assert payer is not None
    npi, expected_name_fragment = KNOWN_LIVE_SAMPLES[payer_id]
    # Name-based payers need NPPES (or equivalent) name fields.
    provider = None
    if payer.search_strategy == "name_then_npi":
        if npi == ENKESHAFI_NPI:
            provider = {"first_name": "ARDALAN", "last_name": "ENKESHAFI"}
        elif npi == "1013231570":
            provider = {"last_name": "SEIDLER"}
        elif npi == FONTE_NPI:
            provider = {"first_name": "NELAYDA", "last_name": "FONTE"}
    async with new_async_client() as client:
        result = await lookup_payer_network(client, payer, npi, provider)
    assert result["error"] is None, result
    assert result["found"] is True
    assert result["in_network"] is True
    assert expected_name_fragment.lower() in (result.get("practitioner_name") or "").lower()


@pytest.mark.asyncio
async def test_live_combined_lookup_aetna_is_skipped() -> None:
    from service import lookup_provider_status

    async with new_async_client() as client:
        result = await lookup_provider_status(client, ENKESHAFI_NPI, payer="aetna")
    assert result["provider"]["found"] is True
    assert result["medicare_pecos"]["enrolled"] is True
    assert result["network_status"]["results"] == []
    skipped = result["network_status"]["skipped"]
    assert skipped and skipped[0]["payer"] == "aetna"
    assert "OAuth" in skipped[0]["reason"] or "registration" in skipped[0]["reason"].lower()
    assert result["stedi_edi_enrollment"]["configured"] is False



def test_web_ui_assets_served() -> None:
    from app import app

    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "text/html" in home.headers.get("content-type", "")
        assert "CHS Provider Status Lookup" in home.text
        assert "/static/styles.css" in home.text
        assert "/static/app.js" in home.text

        css = client.get("/static/styles.css")
        assert css.status_code == 200
        assert "text/css" in css.headers.get("content-type", "")
        assert ".health-badge" in css.text
        assert "--navy" in css.text

        js = client.get("/static/app.js")
        assert js.status_code == 200
        ctype = js.headers.get("content-type", "")
        assert "javascript" in ctype or "ecmascript" in ctype or ctype.startswith("text/plain")
        assert "/provider-status" in js.text
        assert "transaction_support" in js.text
        assert "chs-provider-status-history" in js.text


def test_password_gate(monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    from app import app

    monkeypatch.setenv("APP_PASSWORD", "test-gate-pass")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 401
        assert client.get("/payers").status_code == 401
        token = base64.b64encode(b"scott:test-gate-pass").decode()
        assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
        assert client.get("/payers", headers={"Authorization": "Bearer test-gate-pass"}).status_code == 200
