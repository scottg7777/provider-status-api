"""Find Provider (name search) + Readiness Report tests. HTTP is mocked except the live NPPES checks."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import name_search as ns
import readiness
from clients import cms_extra
from clients.http_util import new_async_client
from clients.leie import LeieIndex
from clients.registry import CMS_DATA_API, NPPES_URL, OIG_LEIE_CSV_URL


def _nppes_row(npi, *, org=None, first=None, last=None, state="OH", city="WEST CHESTER", status="A",
               address="7242 TYLERS CORNER DR STE B", zip_="450696335", phone="513-644-9164", official=None):
    basic: dict[str, Any] = {"status": status, "enumeration_date": "2026-01-30", "last_updated": "2026-01-30"}
    if org:
        basic["organization_name"] = org
        if official:
            basic.update({"authorized_official_first_name": official[0], "authorized_official_last_name": official[1]})
    else:
        basic.update({"first_name": first, "last_name": last, "credential": "FNP-C"})
    return {
        "number": npi,
        "enumeration_type": "NPI-2" if org else "NPI-1",
        "basic": basic,
        "addresses": [{"address_purpose": "LOCATION", "address_1": address, "city": city, "state": state,
                       "postal_code": zip_, "telephone_number": phone}],
        "taxonomies": [{"code": "207N00000X", "desc": "Dermatology", "primary": True}],
        "other_names": [],
    }


# ---------- normalization / variants ----------

@pytest.mark.parametrize(
    "q,kind,org,first,last",
    [
        ("Aeterna Aesthetics, LLC", "organization", "AETERNA AESTHETICS", None, None),
        ("Dr. Christine Snoke", "individual", None, "CHRISTINE", "SNOKE"),
        ("Snoke, Christine", "individual", None, "CHRISTINE", "SNOKE"),
        ("Christine Snoke, FNP-C", "individual", None, "CHRISTINE", "SNOKE"),
        ("Snoke Christine", "both", None, "SNOKE", "CHRISTINE"),
    ],
)
def test_parse_query(q, kind, org, first, last):
    p = ns.parse_query(q)
    assert p["kind"] == kind
    if org:
        assert p["org_name"] == org
    if first:
        assert (p["first"], p["last"]) == (first, last)


def test_clean_text_strips_punctuation():
    assert ns.clean_text("St. Mary's  Health & Wellness!") == "ST MARYS HEALTH AND WELLNESS"
    assert ns.strip_suffixes("ACME MEDICAL GROUP PLLC".split()) == ["ACME", "MEDICAL", "GROUP"]


def test_variants_both_orders_and_wildcards():
    p = ns.parse_query("Snoke Christine")
    labels = {v["label"]: v["params"] for v in ns.build_variants(p, state="oh", city="West Chester")}
    assert labels["first + last"]["first_name"] == "SNOKE"
    assert labels["last + first (reversed)"] == {
        "first_name": "CHRISTINE", "last_name": "SNOKE", "enumeration_type": "NPI-1", "state": "OH", "city": "WEST CHESTER",
    }
    assert labels["organization name*"]["organization_name"] == "SNOKE CHRISTINE*"
    for v in ns.build_variants(p):
        for key in ("organization_name", "first_name", "last_name"):
            val = v["params"].get(key, "")
            if val.endswith("*"):
                assert len(val) - 1 >= ns.MIN_WILDCARD_PREFIX


def test_wildcard_needs_two_chars():
    assert ns._wild("A") is None
    assert ns._wild("AE") == "AE*"


# ---------- ranking + merge ----------

def test_score_prefers_exact_and_location():
    p = ns.parse_query("Christine Snoke")
    exact = ns.summarize_result(_nppes_row("1699443499", first="CHRISTINE", last="SNOKE"))
    other = ns.summarize_result(_nppes_row("1111111112", first="CHRISTINA", last="SNOW", state="TX", city="AUSTIN"))
    s1, r1 = ns.score_result(exact, p, state="OH", city="West Chester")
    s2, _ = ns.score_result(other, p, state="OH", city="West Chester")
    assert s1 > s2
    assert s1 == 100
    assert "state OH" in r1 and "active NPI" in r1


def test_deactivated_scores_lower():
    p = ns.parse_query("Christine Snoke")
    a = ns.summarize_result(_nppes_row("1699443499", first="CHRISTINE", last="SNOKE"))
    d = ns.summarize_result(_nppes_row("1699443499", first="CHRISTINE", last="SNOKE", status="D"))
    assert ns.score_result(a, p, state=None, city=None)[0] > ns.score_result(d, p, state=None, city=None)[0]


async def test_search_merges_dedupes_and_ranks():
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        calls.append(params)
        rows = []
        if params.get("enumeration_type") == "NPI-1":
            rows = [_nppes_row("1699443499", first="CHRISTINE", last="SNOKE")]
            if params.get("last_name", "").endswith("*"):
                rows.append(_nppes_row("1234567893", first="CHRISTINE", last="SNOKEWELL", state="CA", city="LA"))
        elif params.get("organization_name") == "SNOKE CHRISTINE*":
            return httpx.Response(200, json={"Errors": [{"description": "Wildcards require at least two leading characters"}]})
        return httpx.Response(200, json={"result_count": len(rows), "results": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await ns.search_by_name(client, "Snoke Christine", state="OH")
    npis = [r["npi"] for r in out["results"]]
    assert npis[0] == "1699443499"
    assert npis.count("1699443499") == 1
    assert len(out["results"][0]["matched_variants"]) >= 2
    assert any("Wildcards" in e for e in out["errors"])
    assert all(c.get("state") == "OH" for c in calls)


async def test_affiliated_by_address_phone_and_official():
    org = ns.summarize_result(_nppes_row("1538019484", org="AETERNA AESTHETICS LLC", official=("CHRISTINE", "SNOKE")))

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        if params.get("postal_code"):
            return httpx.Response(200, json={"results": [
                _nppes_row("1699443499", first="CHRISTINE", last="SNOKE"),
                _nppes_row("1962335943", first="TRICIA", last="POWERS", phone="000"),
                _nppes_row("1111111112", first="FAR", last="AWAY", address="8039 CINCINNATI DAYTON RD", phone="999"),
            ]})
        return httpx.Response(200, json={"results": [_nppes_row("1699443499", first="CHRISTINE", last="SNOKE")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await ns.find_affiliated(client, org)
    by = {r["npi"]: r for r in out["results"]}
    assert "1111111112" not in by
    assert out["results"][0]["npi"] == "1699443499"
    assert "authorized official of the organization" in by["1699443499"]["affiliation_reasons"]
    assert "same practice address (same suite)" in by["1962335943"]["affiliation_reasons"]


# ---------- extra public-source clients ----------

def _cms_transport(rows_by_dataset: dict[str, list[dict[str, Any]]]):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for ds, rows in rows_by_dataset.items():
            if url.startswith(f"{CMS_DATA_API}/{ds}/data"):
                return httpx.Response(200, json=rows)
        return httpx.Response(500)
    return httpx.MockTransport(handler)


async def test_opt_out_active_and_expired():
    ds = cms_extra.SOURCES["opt_out"][1]
    rows = [{"NPI": "1", "Optout Effective Date": "2024-01-01", "Optout End Date": "2028-01-01", "Eligible to Order and Refer": "Y"}]
    async with httpx.AsyncClient(transport=_cms_transport({ds: rows})) as c:
        r = await cms_extra.lookup_opt_out(c, "1699443499", today=date(2026, 11, 20))
    assert r["active_opt_out"] is True and r["affidavits"][0]["eligible_to_order_and_refer"] is True
    rows[0]["Optout End Date"] = "2025-01-01"
    async with httpx.AsyncClient(transport=_cms_transport({ds: rows})) as c:
        r = await cms_extra.lookup_opt_out(c, "1699443499", today=date(2026, 11, 20))
    assert r["active_opt_out"] is False and r["found"] is True


async def test_order_referring_reassignment_revalidation_and_errors():
    t = _cms_transport({
        cms_extra.SOURCES["order_referring"][1]: [{"NPI": "1699443499", "PARTB": "Y", "DME": "N", "HHA": "Y", "PMD": "N", "HOSPICE": "N"}],
        cms_extra.SOURCES["reassignment"][1]: [{"Group PAC ID": "9931571874", "Group Legal Business Name": "Ohio Med Llc", "Individual NPI": "1699443499"}],
    })
    async with httpx.AsyncClient(transport=t) as c:
        o = await cms_extra.lookup_order_referring(c, "1699443499")
        r = await cms_extra.lookup_reassignments(c, "1699443499")
        v = await cms_extra.lookup_revalidation(c, "1699443499")  # 500 -> error
    assert o["found"] and o["part_b"] is True and o["dme"] is False
    assert r["reassignments"][0]["group_name"] == "Ohio Med Llc"
    assert v["found"] is None and "failed" in v["error"]


LEIE_CSV = (
    "LASTNAME,FIRSTNAME,MIDNAME,BUSNAME,GENERAL,SPECIALTY,UPIN,NPI,DOB,ADDRESS,CITY,STATE,ZIP,EXCLTYPE,EXCLDATE,REINDATE,WAIVERDATE,WVRSTATE\n"
    "DOE,JANE,,,,NURSE,,1234567893,19700101,1 MAIN,COLUMBUS,OH,43004,1128a1,20200101,0,0,\n"
    "SMITH,JOHN,,,,MD,,0,19600101,2 MAIN,DAYTON,OH,45402,1128b4,20210101,0,0,\n"
    ",,,\"BAD CLINIC, LLC\",OTHER BUSINESS,CLINIC,,0,,3 MAIN,AKRON,OH,44301,1128a1,20220101,0,0,\n"
    "OLD,REINSTATED,,,,PT,,1003000126,19500101,4 MAIN,TOLEDO,OH,43601,1128b4,20100101,20150101,0,\n"
)


def test_leie_index_lookup_and_no_dob():
    idx = LeieIndex()
    idx.load_csv_text(LEIE_CSV, "Tue, 10 Nov 2026 10:18:38 GMT")
    assert idx.row_count == 4
    hit = idx.lookup("1234567893", first_name="Jane", last_name="Doe")
    assert hit["excluded"] is True
    assert "19700101" not in json.dumps(hit)
    name_only = idx.lookup("1699443499", first_name="John", last_name="Smith", state="OH")
    assert name_only["excluded"] is False and len(name_only["possible_name_matches"]) == 1
    org = idx.lookup("1538019484", organization_name="Bad Clinic LLC")
    assert org["possible_name_matches"][0]["business_name"] == "BAD CLINIC, LLC"
    reinstated = idx.lookup("1003000126")
    assert reinstated["excluded"] is False and reinstated["npi_matches"]


async def test_leie_download_failure_reports_error():
    idx = LeieIndex()
    t = httpx.MockTransport(lambda r: httpx.Response(503) if str(r.url) == OIG_LEIE_CSV_URL else httpx.Response(404))
    async with httpx.AsyncClient(transport=t) as c:
        await idx.ensure_loaded(c)
    out = idx.lookup("1699443499")
    assert out["excluded"] is None and "LEIE" in out["error"]


# ---------- verdict logic ----------

def _pecos(enrolled=True):
    return {"enrolled": enrolled, "provider_types": ["PRACTITIONER - NURSE PRACTITIONER"], "enrollments": [], "error": None}


def test_medicare_verdicts():
    none = {"reassignments": [], "enrollments": [], "source": "x"}
    ok = readiness.medicare_verdict(pecos=_pecos(), opt_out={"active_opt_out": False}, reassign={"reassignments": [{"group_name": "Ohio Med Llc"}], "source": "r"},
                                    revalidation=none, is_org=False, group_members=None)
    assert ok["status"] == readiness.READY and any("Ohio Med" in r for r in ok["reasons"])
    opted = readiness.medicare_verdict(pecos=_pecos(), opt_out={"active_opt_out": True}, reassign=none, revalidation=none, is_org=False, group_members=None)
    assert opted["status"] == readiness.NOT_READY
    missing = readiness.medicare_verdict(pecos=_pecos(False), opt_out={"active_opt_out": False}, reassign=none, revalidation=none, is_org=True, group_members=None)
    assert missing["status"] == readiness.NOT_READY
    err = readiness.medicare_verdict(pecos={"error": "CMS PECOS request failed"}, opt_out={}, reassign=none, revalidation=none, is_org=False, group_members=None)
    assert err["status"] == readiness.UNKNOWN


def _enr(status, tx=("837P Professional Claims",)):
    return {"id": f"e-{status}", "status": status, "payer": {"name": "Cigna", "stedi_payer_id": "62308"},
            "transactions": [{"label": t} for t in tx]}


@pytest.mark.parametrize(
    "fhir,enrollments,expected",
    [
        ({"in_network": True, "active": True, "base_url": "u"}, [_enr("LIVE")], readiness.READY),
        ({"in_network": True, "active": True, "base_url": "u"}, [], readiness.LIKELY_READY),
        (None, [_enr("LIVE")], readiness.LIKELY_READY),
        ({"in_network": False, "queried": True, "base_url": "u"}, [], readiness.UNKNOWN),
        ({"in_network": False, "base_url": "u"}, [_enr("PROVISIONING")], readiness.NOT_READY),
        ({"in_network": True, "base_url": "u"}, [_enr("REJECTED")], readiness.NOT_READY),
        ({"in_network": True, "active": False, "found": True, "base_url": "u"}, [], readiness.NOT_READY),
        ({"found": False, "error": "ReadTimeout", "base_url": "u"}, [], readiness.UNKNOWN),
    ],
)
def test_payer_verdicts(fhir, enrollments, expected):
    assert readiness.payer_verdict(fhir=fhir, enrollments=enrollments, support=None)["status"] == expected


def test_support_notes():
    v = readiness.payer_verdict(fhir={"in_network": True, "base_url": "u"}, enrollments=[],
                                support={"professional_claims_support": "ENROLLMENT_REQUIRED", "era_support": "ENROLLMENT_REQUIRED"})
    assert any("837P" in r for r in v["reasons"]) and any("ERA" in r for r in v["reasons"])


def test_overall_verdicts():
    prov = {"found": True, "active": True}
    rows = [{"payer": "Medicare (FFS)", "status": readiness.READY}, {"payer": "Cigna", "status": readiness.LIKELY_READY}]
    o = readiness.overall_verdict(provider=prov, leie={"excluded": False}, rows=rows)
    assert o["status"] == readiness.READY and "Medicare" in o["headline"] and "Cigna" in o["headline"]
    assert readiness.overall_verdict(provider=prov, leie={"excluded": True}, rows=rows)["status"] == readiness.NOT_READY
    assert readiness.overall_verdict(provider={"found": True, "active": False}, leie={}, rows=rows)["status"] == readiness.NOT_READY
    assert readiness.overall_verdict(provider=prov, leie={}, rows=[{"payer": "x", "status": readiness.UNKNOWN}])["status"] == readiness.UNKNOWN
    assert readiness.overall_verdict(provider=prov, leie={}, rows=[{"payer": "Medicare", "status": readiness.NOT_READY}, {"payer": "x", "status": readiness.UNKNOWN}])["status"] == readiness.NOT_READY
    assert readiness.overall_verdict(provider=prov, leie={}, rows=[{"payer": "Cigna", "status": readiness.LIKELY_READY}])["status"] == readiness.LIKELY_READY


def test_enrollment_matches_fhir_alias():
    assert readiness._enrollment_matches_fhir({"payer": {"name": "UHC Community Plan", "stedi_payer_id": "X"}}, "unitedhealthcare")
    assert readiness._enrollment_matches_fhir({"payer": {"name": "Something", "stedi_payer_id": "62308"}}, "cigna")
    assert not readiness._enrollment_matches_fhir({"payer": {"name": "Aetna", "stedi_payer_id": "60054"}}, "cigna")


# ---------- endpoints (mocked) ----------

async def test_build_report_mocked(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("STEDI_API_KEY", "sk-should-never-appear")
    from clients import leie as leie_mod

    idx = LeieIndex()
    idx.load_csv_text(LEIE_CSV)
    monkeypatch.setattr(readiness, "LEIE", idx)
    monkeypatch.setattr(leie_mod, "LEIE", idx)

    async def fake_status(client, npi, payer=None, tin=None):
        return {
            "provider": {"npi": npi, "found": True, "active": True, "enumeration_type": "NPI-1", "first_name": "CHRISTINE", "last_name": "SNOKE",
                         "practice_addresses": [{"state": "OH"}]},
            "medicare_pecos": _pecos(),
            "network_status": {"results": [{"payer_id": "cigna", "payer_name": "Cigna", "in_network": True, "active": True, "base_url": "https://fhir.cigna.com"}]},
            "stedi_edi_enrollment": {"configured": True, "enrollments": [_enr("LIVE", ("835 ERA",)), {**_enr("LIVE"), "id": "e2", "payer": {"name": "Aetna", "stedi_payer_id": "60054"}}], "error": None},
        }

    async def fake_nppes(client, npi):
        return {"npi": npi, "found": True, "first_name": "CHRISTINE", "last_name": "SNOKE", "practice_addresses": [{"state": "OH"}]}

    async def fake_support(client, ids):
        return {"62308": {"stedi_payer_id": "62308", "era_support": "ENROLLMENT_REQUIRED"}}

    monkeypatch.setattr(readiness, "lookup_provider_status", fake_status)
    monkeypatch.setattr(readiness, "_stedi_support", fake_support)
    import clients.nppes as nppes_mod
    monkeypatch.setattr(nppes_mod, "lookup_nppes", fake_nppes)
    t = _cms_transport({ds: [] for _, ds in cms_extra.SOURCES.values()})
    async with httpx.AsyncClient(transport=t) as c:
        report = await readiness.build_report(c, "1699443499")
    payers = {r["payer"]: r for r in report["payers"]}
    assert payers["Medicare (FFS)"]["status"] == readiness.READY
    assert payers["Cigna"]["status"] == readiness.READY  # directory + LIVE enrollment
    assert payers["Aetna"]["status"] == readiness.LIKELY_READY  # Stedi-only row
    assert payers["Cigna"]["eligibility_payer"] == "62308"
    assert report["overall"]["status"] == readiness.READY
    assert report["checks"]["oig_leie"]["excluded"] is False
    assert any(s["name"].startswith("SAM.gov") for s in report["skipped_sources"])
    assert "sk-should-never-appear" not in json.dumps(report) + caplog.text


def test_name_endpoint_validation():
    from app import app

    with TestClient(app) as client:
        assert client.get("/search/name", params={"q": "a"}).status_code == 422
        assert client.get("/search/name", params={"q": "abc", "kind": "robot"}).status_code == 422
        assert client.get("/readiness-report", params={"npi": "123"}).status_code == 400
        assert client.get("/search/affiliated", params={"npi": "123"}).status_code == 400


def test_password_never_logged(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("APP_PASSWORD", "pw-never-logged-123")
    from app import app

    with TestClient(app) as client:
        assert client.get("/search/name", params={"q": "aeterna"}).status_code == 401
        assert client.get("/health").status_code == 200
    out = capsys.readouterr()
    assert "pw-never-logged-123" not in caplog.text + out.out + out.err


# ---------- live NPPES ----------

async def test_live_nppes_aeterna_group():
    async with new_async_client() as client:
        out = await ns.search_by_name(client, "Aeterna Aesthetics")
    assert out["results"][0]["npi"] == "1538019484"
    assert out["results"][0]["type"] == "organization"


async def test_live_nppes_christine_snoke():
    async with new_async_client() as client:
        for q in ("Christine Snoke", "Dr. Christine Snoke", "Snoke Christine"):
            out = await ns.search_by_name(client, q)
            assert out["results"][0]["npi"] == "1699443499", q


async def test_live_affiliated_aeterna():
    async with new_async_client() as client:
        org = (await ns.search_by_name(client, "Aeterna Aesthetics", state="OH"))["results"][0]
        out = await ns.find_affiliated(client, org)
    assert "1699443499" in {r["npi"] for r in out["results"]}
