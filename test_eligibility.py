"""Eligibility Readiness Check unit tests (all Stedi calls mocked; no PHI, no network)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import eligibility
from clients.registry import STEDI_ELIGIBILITY_URL, STEDI_ENROLLMENTS_BASE, STEDI_PAYERS_BASE

NPI = "1999999984"
MEMBER = {
    "member_id": "UHCSECRET987",
    "first_name": "Zelda",
    "last_name": "Quuxworth",
    "date_of_birth": "1971-01-01",
}
PAYER_ITEM = {
    "stediId": "KMQTZ",
    "displayName": "UnitedHealthcare",
    "primaryPayerId": "87726",
    "aliases": ["87726", "UHC"],
    "transactionSupport": {"eligibilityCheck": "SUPPORTED", "professionalClaimSubmission": "SUPPORTED"},
}


def _req(**overrides: Any) -> dict[str, Any]:
    return {"npi": NPI, "payer": "87726", "provider_organization_name": "Provider Name", **MEMBER, **overrides}


def _active_271() -> dict[str, Any]:
    return {
        "id": "ec_test",
        "meta": {"applicationMode": "test"},
        "payer": {"name": "UnitedHealthcare"},
        "provider": {"npi": NPI, "providerName": "PROVIDER NAME"},
        "subscriber": {"memberId": MEMBER["member_id"], "firstName": "ZELDA", "lastName": "QUUXWORTH", "dateOfBirth": "19710101"},
        "planStatus": [{"status": "Active Coverage", "statusCode": "1", "serviceTypeCodes": ["30"]}],
        "benefitsInformation": [
            {"code": "1", "name": "Active Coverage", "serviceTypeCodes": ["30"], "planCoverage": "Choice Plus"},
            {"code": "C", "name": "Deductible", "inPlanNetworkIndicatorCode": "Y", "inPlanNetworkIndicator": "Yes"},
            {"code": "C", "name": "Deductible", "inPlanNetworkIndicatorCode": "N", "inPlanNetworkIndicator": "No"},
        ],
        "errors": [],
        "x12": "ISA*00*...NM1*IL*1*QUUXWORTH*ZELDA****MI*UHCSECRET987~",
    }


def _aaa(code: str, level: str = "provider", followup: str = "Please Correct and Resubmit") -> dict[str, Any]:
    err = {"field": "AAA", "code": code, "description": eligibility.AAA_DESCRIPTIONS.get(code, "x"), "followupAction": followup}
    body: dict[str, Any] = {"payer": {"name": "UnitedHealthcare"}, "errors": [err]}
    body[level] = {"aaaErrors": [err]}
    return body


def _transport(elig_status: int, elig_body: Any, *, seen: dict[str, Any] | None = None, payer_items=None, raise_exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{STEDI_PAYERS_BASE}/payers/search"):
            return httpx.Response(200, json={"items": payer_items if payer_items is not None else [{"payer": PAYER_ITEM}]})
        if url.startswith(f"{STEDI_ENROLLMENTS_BASE}/enrollments"):
            return httpx.Response(200, json={"items": [{
                "id": "enr-1", "status": "LIVE", "payer": {"name": "UnitedHealthcare", "stediPayerId": "KMQTZ"},
                "provider": {"npi": NPI, "name": "Provider Name"},
                "transactions": {"claimPayment": {"enroll": True}}, "tasks": [],
            }]})
        if url == STEDI_ELIGIBILITY_URL:
            if seen is not None:
                seen["body"] = json.loads(request.content)
                seen["auth"] = bool(request.headers.get("Authorization"))
            if raise_exc:
                raise raise_exc
            return httpx.Response(elig_status, json=elig_body)
        return httpx.Response(404, json={"message": "unexpected"})

    return httpx.MockTransport(handler)


async def _run(monkeypatch, status, body, **kw) -> dict[str, Any]:
    monkeypatch.setenv("STEDI_API_KEY", "test-key-not-real")
    req = kw.pop("req", None) or _req()
    from models import EligibilityReadinessRequest

    payload = EligibilityReadinessRequest(**req).model_dump()
    async with httpx.AsyncClient(transport=_transport(status, body, **kw)) as client:
        return await eligibility.run_readiness_check(client, payload)


def _no_phi(result: dict[str, Any]) -> None:
    dumped = json.dumps(result)
    for value in ("UHCSECRET987", "Zelda", "ZELDA", "Quuxworth", "QUUXWORTH", "1971-01-01", "19710101", "ISA*"):
        assert value not in dumped, value


async def test_recognized_active_coverage(monkeypatch):
    seen: dict[str, Any] = {}
    result = await _run(monkeypatch, 200, _active_271(), seen=seen)
    assert result["classification"] == "RECOGNIZED"
    assert result["coverage_active"] is True
    assert result["payer_response_name"] == "UnitedHealthcare"
    assert {t["code"] for t in result["network_tier_indicators"]} == {"Y", "N"}
    assert "not proof" in result["caveat"]
    assert "benefit-tier" in result["network_tier_note"]
    assert result["stedi_enrollment"]["summary"].startswith("1 enrollment")
    assert result["member"]["member_id_masked"].endswith("Y987") or result["member"]["member_id_masked"].endswith("987")
    # Request shape matches Stedi legacy v3.
    body = seen["body"]
    assert seen["auth"] is True
    assert body["tradingPartnerServiceId"] == "87726"
    assert body["provider"] == {"npi": NPI, "organizationName": "Provider Name"}
    assert body["subscriber"]["dateOfBirth"] == "19710101"
    assert body["encounter"] == {"serviceTypeCodes": ["30"]}
    _no_phi(result)


@pytest.mark.parametrize("code", ["41", "43", "44", "45", "50", "51"])
async def test_provider_not_recognized(monkeypatch, code):
    result = await _run(monkeypatch, 200, _aaa(code, "provider"))
    assert result["classification"] == "PROVIDER_NOT_RECOGNIZED"
    assert result["aaa_errors"][0]["code"] == code
    assert result["aaa_errors"][0]["level"] == "provider"
    _no_phi(result)


@pytest.mark.parametrize("code", ["72", "73", "75", "76"])
async def test_patient_issue(monkeypatch, code):
    result = await _run(monkeypatch, 200, _aaa(code, "subscriber"))
    assert result["classification"] == "PATIENT_ISSUE"
    _no_phi(result)


@pytest.mark.parametrize("code", ["42", "79", "80"])
async def test_payer_unavailable_codes(monkeypatch, code):
    result = await _run(monkeypatch, 200, _aaa(code, "payer", followup="Resubmission Allowed"))
    assert result["classification"] == "PAYER_UNAVAILABLE"


async def test_42_with_correct_and_resubmit_is_inconclusive(monkeypatch):
    result = await _run(monkeypatch, 200, _aaa("42", "payer", followup="Please Correct and Resubmit"))
    assert result["classification"] == "INCONCLUSIVE"


async def test_provider_error_wins_over_patient_error(monkeypatch):
    body = _aaa("75", "subscriber")
    body["provider"] = {"aaaErrors": [{"code": "51", "description": "Provider Not on File"}]}
    result = await _run(monkeypatch, 200, body)
    assert result["classification"] == "PROVIDER_NOT_RECOGNIZED"


@pytest.mark.parametrize("status", [429, 500, 503, 504])
async def test_http_unavailable(monkeypatch, status):
    result = await _run(monkeypatch, status, {"message": "Unavailable"})
    assert result["classification"] == "PAYER_UNAVAILABLE"


async def test_timeout_is_payer_unavailable(monkeypatch):
    result = await _run(monkeypatch, 200, {}, raise_exc=httpx.ReadTimeout("slow"))
    assert result["classification"] == "PAYER_UNAVAILABLE"
    assert "timeout" in result["explanation"].lower()


async def test_http_400_routing_79(monkeypatch):
    body = {"message": "routing", "errors": [{"code": "79", "description": "Invalid Participant Identification"}]}
    result = await _run(monkeypatch, 400, body)
    assert result["classification"] == "PAYER_UNAVAILABLE"


async def test_http_400_validation_inconclusive(monkeypatch):
    result = await _run(monkeypatch, 400, {"message": "invalid request body", "code": "INVALID_REQUEST_BODY"})
    assert result["classification"] == "INCONCLUSIVE"
    assert "HTTP 400" in result["explanation"]


async def test_inactive_coverage_inconclusive(monkeypatch):
    body = _active_271()
    body["planStatus"] = [{"status": "Inactive", "statusCode": "6"}]
    body["benefitsInformation"] = [{"code": "6", "name": "Inactive"}]
    result = await _run(monkeypatch, 200, body)
    assert result["classification"] == "INCONCLUSIVE"
    assert result["coverage_active"] is False
    assert "inactive" in result["explanation"].lower()


async def test_unknown_response_inconclusive(monkeypatch):
    result = await _run(monkeypatch, 200, {"payer": {"name": "X"}})
    assert result["classification"] == "INCONCLUSIVE"


async def test_ambiguous_payer_returns_matches_without_sending(monkeypatch):
    seen: dict[str, Any] = {}
    items = [
        {"stediId": "AAA01", "displayName": "Anthem Ohio", "primaryPayerId": "00834", "aliases": []},
        {"stediId": "AAA02", "displayName": "Anthem Ohio Medicaid", "primaryPayerId": "0002937", "aliases": []},
    ]
    result = await _run(monkeypatch, 200, _active_271(), seen=seen, payer_items=items, req=_req(payer="anthem"))
    assert result["classification"] == "PAYER_AMBIGUOUS"
    assert len(result["payer_matches"]) == 2
    assert "body" not in seen


async def test_payer_not_supported_short_circuits(monkeypatch):
    seen: dict[str, Any] = {}
    item = {**PAYER_ITEM, "transactionSupport": {"eligibilityCheck": "NOT_SUPPORTED"}}
    result = await _run(monkeypatch, 200, _active_271(), seen=seen, payer_items=[item])
    assert result["classification"] == "INCONCLUSIVE"
    assert "body" not in seen


async def test_not_configured(monkeypatch):
    monkeypatch.delenv("STEDI_API_KEY", raising=False)
    from models import EligibilityReadinessRequest

    async with httpx.AsyncClient(transport=_transport(200, {})) as client:
        result = await eligibility.run_readiness_check(client, EligibilityReadinessRequest(**_req()).model_dump())
    assert result["stedi_configured"] is False
    assert result["classification"] == "INCONCLUSIVE"


async def test_phi_and_key_never_logged(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("STEDI_API_KEY", "sk-super-secret-value")
    from models import EligibilityReadinessRequest

    payload = EligibilityReadinessRequest(**_req()).model_dump()
    async with httpx.AsyncClient(transport=_transport(200, _active_271())) as client:
        result = await eligibility.run_readiness_check(client, payload)
    out = capsys.readouterr()
    logged = caplog.text + out.out + out.err
    for value in ("UHCSECRET987", "Zelda", "Quuxworth", "1971", "sk-super-secret-value"):
        assert value not in logged, value
    assert "sk-super-secret-value" not in json.dumps(result)
    _no_phi(result)


def test_endpoint_validation_does_not_echo_member_data(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from app import app

    with TestClient(app) as client:
        bad = client.post("/eligibility-readiness", json=_req(date_of_birth="not-a-date"))
        assert bad.status_code == 422
        assert "UHCSECRET987" not in bad.text and "Zelda" not in bad.text and "not-a-date" not in bad.text
        bad_npi = client.post("/eligibility-readiness", json=_req(npi="123"))
        assert bad_npi.status_code == 422


def test_endpoint_end_to_end_mocked(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("STEDI_API_KEY", "test-key-not-real")
    from app import app

    with TestClient(app) as client:
        client.app.state.http = httpx.AsyncClient(transport=_transport(200, _aaa("51")))
        response = client.post("/eligibility-readiness", json=_req())
        assert response.status_code == 200
        assert response.json()["classification"] == "PROVIDER_NOT_RECOGNIZED"
        _no_phi(response.json())
        mocks = client.get("/eligibility-readiness/mock-requests").json()
        assert mocks["provider"]["npi"] == "1999999984"
        assert any(m["member_id"] == "UHCAAA43" for m in mocks["requests"])
        search = client.get("/payers/search", params={"q": "uhc"}).json()
        assert search["results"][0]["primary_payer_id"] == "87726"


def test_normalize_date_and_mask():
    assert eligibility.normalize_date("2026-11-20", field="d") == "20261120"
    assert eligibility.normalize_date("11/20/2026", field="d") == "20261120"
    assert eligibility.normalize_date("20261120", field="d") == "20261120"
    with pytest.raises(ValueError):
        eligibility.normalize_date("Nov 20", field="d")
    assert eligibility.mask_member_id("ABC123456") == "••••3456"
