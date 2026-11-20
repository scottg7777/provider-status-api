"""Eligibility Readiness Check: does a payer recognize a provider NPI?

Sends one Stedi real-time eligibility check (270/271) under the provider's NPI for a
single member and classifies the payer's response. A clean, active-coverage response
is a strong signal the payer has the NPI loaded; AAA provider errors (e.g. 51 Provider
Not on File) are a strong signal it doesn't.

PHI safety rules enforced here:
- Request/response bodies and member data are never logged or persisted.
- The returned payload never includes the member's name, DOB, or full member ID,
  and never includes raw X12.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from clients import stedi as stedi_client
from clients.nppes import lookup_nppes

RECOGNIZED = "RECOGNIZED"
PROVIDER_NOT_RECOGNIZED = "PROVIDER_NOT_RECOGNIZED"
PATIENT_ISSUE = "PATIENT_ISSUE"
PAYER_UNAVAILABLE = "PAYER_UNAVAILABLE"
INCONCLUSIVE = "INCONCLUSIVE"
PAYER_AMBIGUOUS = "PAYER_AMBIGUOUS"

CLASSIFICATIONS = (RECOGNIZED, PROVIDER_NOT_RECOGNIZED, PATIENT_ISSUE, PAYER_UNAVAILABLE, INCONCLUSIVE)

# AAA codes and descriptions per Stedi "Payer AAA errors"
# (stedi.com/docs/healthcare/eligibility-troubleshooting#payer-aaa-errors, checked 2026-11-20).
AAA_DESCRIPTIONS: dict[str, str] = {
    "04": "Authorized Quantity Exceeded",
    "15": "Required application data missing",
    "33": "Input Errors",
    "35": "Out of Network",
    "41": "Authorization/Access Restrictions",
    "42": "Unable to Respond at Current Time",
    "43": "Invalid/Missing Provider Identification",
    "44": "Invalid/Missing Provider Name",
    "45": "Invalid/Missing Provider Specialty",
    "46": "Invalid/Missing Provider Phone Number",
    "47": "Invalid/Missing Provider State",
    "48": "Invalid/Missing Referring Provider Identification Number",
    "49": "Provider is Not Primary Care Physician",
    "50": "Provider Ineligible for Inquiries",
    "51": "Provider Not on File",
    "52": "Service Dates Not Within Provider Plan Enrollment",
    "53": "Inquired Benefit Inconsistent with Provider Type Enrollment",
    "54": "Inappropriate Product/Service ID Qualifier",
    "55": "Inappropriate Product/Service ID",
    "56": "Inappropriate Date",
    "57": "Invalid/Missing Date(s) of Service",
    "58": "Invalid/Missing Date-of-Birth",
    "60": "Date of Birth Follows Date(s) of Service",
    "61": "Date of Death Precedes Date(s) of Service",
    "62": "Date of Service Not Within Allowable Inquiry Period",
    "63": "Date of Service in Future",
    "64": "Invalid/Missing Patient ID",
    "65": "Invalid/Missing Patient Name",
    "66": "Invalid/Missing Patient Gender Code",
    "67": "Patient Not Found",
    "68": "Duplicate Patient ID Number",
    "69": "Inconsistent with Patient's Age",
    "70": "Inconsistent with Patient's Gender",
    "71": "Patient Birth Date Does Not Match That for the Patient on the Database",
    "72": "Invalid/Missing Subscriber/Insured ID",
    "73": "Invalid/Missing Subscriber/Insured Name",
    "74": "Invalid/Missing Subscriber/Insured Gender Code",
    "75": "Subscriber/Insured Not Found",
    "76": "Duplicate Subscriber/Insured ID Number",
    "77": "Subscriber Found, Patient Not Found",
    "78": "Subscriber/Insured Not in Group/Plan identified",
    "79": "Invalid Participant Identification",
    "80": "No Response received - Transaction Terminated",
    "97": "Invalid or Missing Provider Address",
    "98": "Experimental Service or Procedure",
    "AA": "Authorization Number Not Found",
    "AE": "Requires Primary Care Physician Authorization",
    "AF": "Invalid/Missing Diagnosis Code(s)",
    "T4": "Payer Name or Identifier Missing",
}

# The payer rejected or limited the inquiry because of how the provider is (or isn't)
# registered: NPI, name, specialty, address, enrollment, network.
PROVIDER_CODES = {"35", "41", "43", "44", "45", "46", "47", "49", "50", "51", "52", "53", "97"}
# Member/patient data problems: inconclusive for the provider.
PATIENT_CODES = {
    "58", "60", "61", "64", "65", "66", "67", "68", "69", "70", "71",
    "72", "73", "74", "75", "76", "77", "78",
}
# Payer/clearinghouse connectivity. Stedi documents 79 as "There is a problem
# connecting with this payer", so it is treated as connectivity, not provider identity.
PAYER_CODES = {"42", "79", "80"}
# Follow-up actions on a 42 that mean the request itself is wrong (don't retry).
NON_RETRY_FOLLOWUPS = {
    "please correct and resubmit",
    "resubmission not allowed",
    "do not resubmit; inquiry initiated to a third party",
}

CAVEAT = (
    "A RECOGNIZED result is a strong signal that the payer has this NPI loaded for "
    "eligibility inquiries, but it is not proof of contracting, credentialing, network "
    "participation, or an effective date. Some payers answer eligibility for any valid "
    "NPI. Confirm participation with the payer's welcome letter or provider services."
)

EXPLANATIONS = {
    RECOGNIZED: (
        "The payer returned active coverage for this member under this NPI with no "
        "provider-related rejections. The payer appears to recognize the provider."
    ),
    PROVIDER_NOT_RECOGNIZED: (
        "The payer rejected the inquiry with a provider-related AAA error. The NPI is "
        "likely not on file, not registered under this name/specialty/address, or not "
        "enrolled with this payer for these services."
    ),
    PATIENT_ISSUE: (
        "The payer couldn't match the member (ID, name, or date of birth). This says "
        "nothing about the provider. Re-check the member details against the insurance "
        "card, or try a different member on this plan."
    ),
    PAYER_UNAVAILABLE: (
        "The payer or the connection to it was unavailable (timeout, downtime, or "
        "routing problem). Retry later; this result says nothing about the provider."
    ),
    INCONCLUSIVE: (
        "The response didn't clearly show whether the payer recognizes this provider."
    ),
}

ACTIVE_CODE = "1"
INACTIVE_CODE = "6"


def normalize_date(value: str | None, *, field: str) -> str | None:
    """Accept YYYY-MM-DD, YYYYMMDD, or MM/DD/YYYY; return Stedi legacy YYYYMMDD."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"{field} must be a date like YYYY-MM-DD")


def mask_member_id(member_id: str | None) -> str | None:
    if not member_id:
        return None
    tail = member_id[-4:] if len(member_id) > 4 else ""
    return f"{'•' * 4}{tail}" if tail else "•" * 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def pick_payer(query: str, matches: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (selected payer, candidates). Exact ID/alias/name match wins; a single result wins."""
    q = _norm(query)
    if not matches:
        return None, []
    for item in matches:
        ids = [item.get("stedi_payer_id"), item.get("primary_payer_id"), *(item.get("aliases") or [])]
        if any(_norm(i) == q for i in ids if i):
            return item, matches
    for item in matches:
        names = [item.get("name"), *(item.get("names") or [])]
        if any(_norm(n) == q for n in names if n):
            return item, matches
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def _payer_option(item: dict[str, Any]) -> dict[str, Any]:
    support = item.get("transaction_support") or {}
    return {
        "stedi_payer_id": item.get("stedi_payer_id"),
        "primary_payer_id": item.get("primary_payer_id"),
        "name": item.get("name"),
        "aliases": (item.get("aliases") or [])[:8],
        "eligibility_support": support.get("eligibilityCheck"),
        "professional_claims_support": support.get("professionalClaimSubmission"),
        "era_support": support.get("claimPayment"),
    }


def _extract_errors(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect AAA errors with the loop they came from; dedupe the top-level superset."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(err: dict[str, Any], level: str) -> None:
        if not isinstance(err, dict):
            return
        code = str(err.get("code") or "").strip().upper()
        if not code:
            return
        key = (code, str(err.get("followupAction") or ""))
        if key in seen:
            return
        seen.add(key)
        found.append(
            {
                "code": code,
                "description": err.get("description") or AAA_DESCRIPTIONS.get(code) or "Unknown AAA code",
                "level": level,
                "followup_action": err.get("followupAction"),
                "possible_resolutions": err.get("possibleResolutions"),
            }
        )

    for level in ("provider", "payer", "subscriber"):
        section = resp.get(level) or {}
        if isinstance(section, dict):
            for err in section.get("aaaErrors") or []:
                add(err, level)
    for dep in resp.get("dependents") or []:
        if isinstance(dep, dict):
            for err in dep.get("aaaErrors") or []:
                add(err, "dependent")
            rp = dep.get("responseProvider") or {}
            for err in (rp.get("aaaErrors") if isinstance(rp, dict) else None) or []:
                add(err, "provider")
    for err in resp.get("errors") or []:
        add(err, "request")
    return found


def classify_errors(errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    codes = {e["code"] for e in errors}
    if codes & PROVIDER_CODES:
        return PROVIDER_NOT_RECOGNIZED
    if codes & PAYER_CODES:
        for e in errors:
            if e["code"] == "42" and str(e.get("followup_action") or "").lower() in NON_RETRY_FOLLOWUPS:
                return INCONCLUSIVE
        return PAYER_UNAVAILABLE
    if codes & PATIENT_CODES:
        return PATIENT_ISSUE
    return INCONCLUSIVE


def _coverage_summary(resp: dict[str, Any]) -> dict[str, Any]:
    benefits = [b for b in (resp.get("benefitsInformation") or []) if isinstance(b, dict)]
    active = [b for b in benefits if str(b.get("code")) == ACTIVE_CODE]
    inactive = [b for b in benefits if str(b.get("code")) == INACTIVE_CODE]
    plan_status = []
    for ps in resp.get("planStatus") or []:
        if isinstance(ps, dict):
            plan_status.append(
                {
                    "status": ps.get("status"),
                    "status_code": ps.get("statusCode"),
                    "service_type_codes": ps.get("serviceTypeCodes") or [],
                }
            )
    ps_codes = {str(p.get("status_code")) for p in plan_status}
    if active or ACTIVE_CODE in ps_codes:
        coverage_active: bool | None = True
    elif inactive or INACTIVE_CODE in ps_codes:
        coverage_active = False
    else:
        coverage_active = None

    tiers: dict[str, dict[str, Any]] = {}
    for b in benefits:
        code = b.get("inPlanNetworkIndicatorCode")
        if not code:
            continue
        row = tiers.setdefault(
            str(code), {"code": str(code), "label": b.get("inPlanNetworkIndicator"), "benefit_rows": 0}
        )
        row["benefit_rows"] += 1

    plan_names = sorted({str(b.get("planCoverage")) for b in active if b.get("planCoverage")})
    return {
        "coverage_active": coverage_active,
        "plan_status": plan_status,
        "plan_names": plan_names[:5],
        "benefit_rows": len(benefits),
        "network_tier_indicators": sorted(tiers.values(), key=lambda r: r["code"]),
        "network_tier_note": (
            "In/out-of-network indicators describe which benefit tier the payer returned "
            "(in-plan-network Y/N). They are benefit-tier info, not proof that this "
            "provider participates in the network."
        ),
    }


def _http_error_result(exc: stedi_client.StediEligibilityHTTPError) -> tuple[str, str, list[dict[str, Any]]]:
    payload = exc.payload or {}
    errors = []
    for err in payload.get("errors") or []:
        if isinstance(err, dict) and err.get("code"):
            code = str(err.get("code")).upper()
            errors.append(
                {
                    "code": code,
                    "description": err.get("description") or AAA_DESCRIPTIONS.get(code) or "Unknown AAA code",
                    "level": "request",
                    "followup_action": err.get("followupAction"),
                    "possible_resolutions": err.get("possibleResolutions"),
                }
            )
    message = str(payload.get("message") or "")[:300]
    status = exc.status_code
    if status == 429 or status >= 500:
        return PAYER_UNAVAILABLE, f"Stedi/payer unavailable (HTTP {status}). {message}".strip(), errors
    classification = classify_errors(errors) if errors else INCONCLUSIVE
    note = f"Stedi rejected the request before a payer response (HTTP {status})."
    if message:
        note += f" {message}"
    return classification or INCONCLUSIVE, note, errors


async def _enrollment_status(client: httpx.AsyncClient, npi: str, payer: dict[str, Any]) -> dict[str, Any]:
    payer_ids = [p for p in {payer.get("stedi_payer_id")} if p]
    try:
        records = await stedi_client.list_enrollments(client, npi=npi, payer_ids=payer_ids or None)
    except PermissionError as exc:
        return {"checked": True, "error": str(exc), "enrollments": [], "summary": "error"}
    except httpx.HTTPError as exc:
        return {"checked": True, "error": f"Stedi request failed: {exc.__class__.__name__}", "enrollments": [], "summary": "error"}
    rows = []
    for item in records:
        s = stedi_client._summarize_enrollment(item)
        rows.append(
            {
                "id": s.get("id"),
                "status": s.get("status"),
                "payer_name": (s.get("payer") or {}).get("name"),
                "transactions": [t["label"] for t in s.get("transactions") or []],
                "open_tasks": len(s.get("open_tasks") or []),
                "status_last_updated_at": s.get("status_last_updated_at"),
            }
        )
    statuses = sorted({r["status"] for r in rows if r.get("status")})
    return {
        "checked": True,
        "error": None,
        "enrollments": rows,
        "summary": f"{len(rows)} enrollment(s): {', '.join(statuses)}" if rows else "no_enrollments",
    }


async def provider_prefill(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    nppes = await lookup_nppes(client, npi)
    is_org = nppes.get("enumeration_type") == "NPI-2"
    return {
        "npi": npi,
        "found": bool(nppes.get("found")),
        "name": nppes.get("name"),
        "enumeration_type": nppes.get("enumeration_type"),
        "provider_type": "organization" if is_org else ("individual" if nppes.get("found") else None),
        "organization_name": nppes.get("organization_name") if is_org else None,
        "first_name": None if is_org else nppes.get("first_name"),
        "last_name": None if is_org else nppes.get("last_name"),
        "error": nppes.get("error"),
    }


def _build_request_body(req: dict[str, Any], payer_id: str, provider: dict[str, Any]) -> dict[str, Any]:
    subscriber = {
        "memberId": req["member_id"],
        "firstName": req["first_name"],
        "lastName": req["last_name"],
        "dateOfBirth": req["date_of_birth"],
    }
    encounter: dict[str, Any] = {"serviceTypeCodes": [req.get("service_type_code") or "30"]}
    if req.get("date_of_service"):
        encounter["dateOfService"] = req["date_of_service"]
    body: dict[str, Any] = {
        "tradingPartnerServiceId": payer_id,
        "provider": provider,
        "subscriber": subscriber,
        "encounter": encounter,
    }
    if req.get("dependent_first_name") or req.get("dependent_last_name"):
        dep = {
            "firstName": req.get("dependent_first_name"),
            "lastName": req.get("dependent_last_name"),
        }
        if req.get("dependent_date_of_birth"):
            dep["dateOfBirth"] = req["dependent_date_of_birth"]
        body["dependents"] = [{k: v for k, v in dep.items() if v}]
    return body


async def run_readiness_check(client: httpx.AsyncClient, req: dict[str, Any]) -> dict[str, Any]:
    """``req`` is a validated EligibilityReadinessRequest.model_dump()."""
    npi = req["npi"]
    checked_at = _now()
    base: dict[str, Any] = {
        "npi": npi,
        "checked_at": checked_at,
        "caveat": CAVEAT,
        "member": {"member_id_masked": mask_member_id(req.get("member_id"))},
    }
    if not stedi_client.is_configured():
        return {
            **base,
            "classification": INCONCLUSIVE,
            "explanation": stedi_client.NOT_CONFIGURED_MESSAGE,
            "stedi_configured": False,
            "aaa_errors": [],
        }

    # 1) Resolve payer -> trading partner service ID.
    try:
        matches = await stedi_client.search_payers(client, req["payer"], limit=10)
    except PermissionError as exc:
        return {**base, "classification": INCONCLUSIVE, "explanation": str(exc), "stedi_configured": True, "aaa_errors": []}
    except httpx.HTTPError as exc:
        return {
            **base,
            "classification": PAYER_UNAVAILABLE,
            "explanation": f"Stedi payer search failed: {exc.__class__.__name__}",
            "stedi_configured": True,
            "aaa_errors": [],
        }
    selected, candidates = pick_payer(req["payer"], matches)
    if selected is None:
        return {
            **base,
            "classification": PAYER_AMBIGUOUS if candidates else INCONCLUSIVE,
            "explanation": (
                "More than one Stedi payer matches. Pick one and run again."
                if candidates
                else f"No Stedi payer matches '{req['payer']}'."
            ),
            "stedi_configured": True,
            "payer_matches": [_payer_option(c) for c in candidates],
            "aaa_errors": [],
        }
    payer_opt = _payer_option(selected)
    payer_id = selected.get("primary_payer_id") or selected.get("stedi_payer_id")
    base["payer"] = {**payer_opt, "trading_partner_service_id": payer_id}

    # 2) Provider name (from request, else NPPES).
    org = req.get("provider_organization_name")
    first = req.get("provider_first_name")
    last = req.get("provider_last_name")
    if not org and not (first and last):
        prefill = await provider_prefill(client, npi)
        org = prefill.get("organization_name")
        first = prefill.get("first_name")
        last = prefill.get("last_name")
    provider: dict[str, Any] = {"npi": npi}
    if org:
        provider["organizationName"] = org
    elif first and last:
        provider["firstName"] = first
        provider["lastName"] = last
    else:
        raise ValueError("Provider name is required (organization name, or first and last name); NPPES had none.")
    base["provider"] = {
        "npi": npi,
        "name_sent": org or f"{first} {last}",
        "type": "organization" if org else "individual",
    }

    enrollment_task = asyncio.create_task(_enrollment_status(client, npi, selected))

    if str(payer_opt.get("eligibility_support") or "").upper() == "NOT_SUPPORTED":
        return {
            **base,
            "classification": INCONCLUSIVE,
            "explanation": (
                f"{payer_opt.get('name')} doesn't support real-time eligibility checks through "
                "Stedi, so readiness can't be tested this way. Pick a different payer ID for "
                "this plan if one exists."
            ),
            "stedi_configured": True,
            "aaa_errors": [],
            "stedi_enrollment": await enrollment_task,
        }

    body = _build_request_body(req, payer_id, provider)

    # 3) Send the 270. Nothing about body/response is logged.
    response: dict[str, Any] | None = None
    aaa: list[dict[str, Any]] = []
    note = None
    try:
        response = await stedi_client.eligibility_check(client, body)
    except PermissionError as exc:
        classification, note = INCONCLUSIVE, str(exc)
    except stedi_client.StediEligibilityHTTPError as exc:
        classification, note, aaa = _http_error_result(exc)
    except httpx.TimeoutException:
        classification, note = PAYER_UNAVAILABLE, "The payer didn't respond in time (timeout)."
    except httpx.HTTPError as exc:
        classification, note = PAYER_UNAVAILABLE, f"Connection to Stedi failed: {exc.__class__.__name__}"
    finally:
        body = {}  # drop member data from memory as soon as possible

    coverage: dict[str, Any] = {"coverage_active": None, "plan_status": [], "network_tier_indicators": []}
    payer_echo = None
    meta: dict[str, Any] = {}
    if response is not None:
        aaa = _extract_errors(response)
        coverage = _coverage_summary(response)
        payer_section = response.get("payer") or {}
        payer_echo = payer_section.get("name") if isinstance(payer_section, dict) else None
        provider_section = response.get("provider") or {}
        if isinstance(provider_section, dict):
            base["provider"]["payer_echo_name"] = provider_section.get("providerName") or provider_section.get("providerOrgName")
            base["provider"]["payer_echo_npi"] = provider_section.get("npi")
        m = response.get("meta") or {}
        meta = {"application_mode": m.get("applicationMode")} if isinstance(m, dict) else {}
        meta["stedi_check_id"] = response.get("id")
        classification = classify_errors(aaa) or ""
        if not classification:
            if coverage["coverage_active"] is True:
                classification = RECOGNIZED
            elif coverage["coverage_active"] is False:
                classification = INCONCLUSIVE
                note = (
                    "The payer answered without provider errors, but the member's coverage is "
                    "inactive. That suggests the payer accepted the NPI, but re-run with a member "
                    "who has active coverage for a definitive result."
                )
            else:
                classification = INCONCLUSIVE
                note = "The payer returned no AAA errors but also no clear active/inactive coverage."
        response = None

    explanation = EXPLANATIONS.get(classification, EXPLANATIONS[INCONCLUSIVE])
    if note:
        explanation = note if classification == INCONCLUSIVE else f"{explanation} Detail: {note}"
    return {
        **base,
        "classification": classification,
        "explanation": explanation,
        "stedi_configured": True,
        "aaa_errors": aaa,
        "coverage_active": coverage.get("coverage_active"),
        "plan_status": coverage.get("plan_status"),
        "plan_names": coverage.get("plan_names", []),
        "network_tier_indicators": coverage.get("network_tier_indicators"),
        "network_tier_note": coverage.get("network_tier_note"),
        "payer_response_name": payer_echo,
        "stedi_meta": meta,
        "stedi_enrollment": await enrollment_task,
    }


# Stedi-documented mock eligibility requests (test API key only). PHI-free, from
# stedi.com/docs/healthcare/api-reference/mock-requests-eligibility-checks.
MOCK_PROVIDER = {"npi": "1999999984", "organization_name": "Provider Name"}
MOCK_REQUESTS: list[dict[str, Any]] = [
    {"label": "UHC – active coverage", "payer": "87726", "member_id": "UHC123456", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1971-01-01", "expected": RECOGNIZED},
    {"label": "Aetna – active coverage", "payer": "60054", "member_id": "AETNA12345", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "2004-04-04", "expected": RECOGNIZED},
    {"label": "Humana – active coverage", "payer": "61101", "member_id": "HUMANA123", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1975-05-05", "expected": RECOGNIZED},
    {"label": "UHC – inactive coverage", "payer": "87726", "member_id": "UHCINACTIVE", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1971-01-01", "expected": INCONCLUSIVE},
    {"label": "UHC – AAA 43 provider ID", "payer": "87726", "member_id": "UHCAAA43", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1970-01-01", "expected": PROVIDER_NOT_RECOGNIZED},
    {"label": "UHC – AAA 72 member ID", "payer": "87726", "member_id": "UHCAAA72", "first_name": "John", "last_name": "Doe", "date_of_birth": "1990-01-01", "expected": PATIENT_ISSUE},
    {"label": "UHC – AAA 75 not found", "payer": "87726", "member_id": "UHCAAA75", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1990-01-01", "expected": PATIENT_ISSUE},
    {"label": "UHC – AAA 42 payer down", "payer": "87726", "member_id": "UHCAAA42", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "2001-01-01", "expected": PAYER_UNAVAILABLE},
    {"label": "UHC – AAA 79 routing", "payer": "87726", "member_id": "UHCAAA79", "first_name": "John", "last_name": "Doe", "date_of_birth": "1970-01-01", "expected": PAYER_UNAVAILABLE},
]


def mock_catalog() -> dict[str, Any]:
    return {
        "note": (
            "Stedi mock eligibility requests only work with a Stedi TEST API key. With a "
            "production key they would be sent to the real payer, so don't use them there."
        ),
        "source": "https://www.stedi.com/docs/healthcare/api-reference/mock-requests-eligibility-checks",
        "provider": MOCK_PROVIDER,
        "requests": MOCK_REQUESTS,
    }
