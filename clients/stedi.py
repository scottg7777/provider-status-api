"""Stedi EDI enrollment and payer-search client.

The API key is read only from STEDI_API_KEY and is never logged or returned.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import httpx

from .registry import STEDI_ENROLLMENTS_BASE, STEDI_PAYERS_BASE

# Transaction object keys as documented on List Enrollments.
TRANSACTION_LABELS = {
    "eligibilityCheck": "270/271 Eligibility",
    "claimStatus": "276/277 Claim Status",
    "professionalClaimSubmission": "837P Professional Claims",
    "institutionalClaimSubmission": "837I Institutional Claims",
    "dentalClaimSubmission": "837D Dental Claims",
    "claimPayment": "835 ERA",
    "coordinationOfBenefits": "Coordination of Benefits",
    "unsolicitedClaimAttachment": "275 Claim Attachments",
    "claimAttachment": "Claim Attachments",
}

NOT_CONFIGURED_MESSAGE = (
    "STEDI_API_KEY is not set. Public CMS and FHIR results are still returned. "
    "Export STEDI_API_KEY in the environment to enable Stedi EDI enrollment lookups."
)


def _api_key() -> str | None:
    value = os.environ.get("STEDI_API_KEY")
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def is_configured() -> bool:
    return _api_key() is not None


def _auth_headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise RuntimeError("STEDI_API_KEY is not set")
    # Current Stedi docs: pass the raw API key in Authorization.
    # The legacy "Key <api_key>" prefix remains accepted by Stedi.
    return {"Authorization": key, "Accept": "application/json"}


def _transaction_types(transactions: Any) -> list[dict[str, str]]:
    if not transactions:
        return []
    out: list[dict[str, str]] = []
    if isinstance(transactions, dict):
        for key, value in transactions.items():
            enroll = True
            if isinstance(value, dict) and "enroll" in value:
                enroll = bool(value.get("enroll"))
            if enroll:
                out.append({"code": key, "label": TRANSACTION_LABELS.get(key, key)})
    elif isinstance(transactions, list):
        for item in transactions:
            if isinstance(item, str):
                out.append({"code": item, "label": TRANSACTION_LABELS.get(item, item)})
            elif isinstance(item, dict):
                code = item.get("type") or item.get("code") or item.get("transaction")
                if code:
                    out.append({"code": code, "label": TRANSACTION_LABELS.get(code, code)})
    return out


def _task_instructions(task: dict[str, Any]) -> str | None:
    definition = task.get("definition") or {}
    manual = definition.get("manualTask") if isinstance(definition, dict) else None
    if isinstance(manual, dict):
        return manual.get("instructions")
    return None


def _open_tasks(tasks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    open_tasks: list[dict[str, Any]] = []
    for task in tasks or []:
        if task.get("isComplete") is True:
            continue
        open_tasks.append(
            {
                "id": task.get("id"),
                "is_complete": bool(task.get("isComplete")),
                "rank": task.get("rank"),
                "responsible_party": task.get("responsibleParty"),
                "instructions": _task_instructions(task),
                "completed_at": task.get("completedAt"),
            }
        )
    return open_tasks


def _summarize_enrollment(item: dict[str, Any]) -> dict[str, Any]:
    payer = item.get("payer") or {}
    provider = item.get("provider") or {}
    open_tasks = _open_tasks(item.get("tasks") or [])
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "status_last_updated_at": item.get("statusLastUpdatedAt"),
        "created_at": item.get("createdAt"),
        "updated_at": item.get("updatedAt"),
        "submitted_at": item.get("submittedAt"),
        "reason": item.get("reason"),
        "source": item.get("source"),
        "payer": {
            "name": payer.get("name"),
            "stedi_payer_id": payer.get("stediPayerId"),
            "submitted_payer_id_or_alias": payer.get("submittedPayerIdOrAlias"),
        },
        "provider": {
            "id": provider.get("id"),
            "name": provider.get("name"),
            "npi": provider.get("npi"),
            "tax_id_type": provider.get("taxIdType"),
            # Never echo a tax ID value back in API output.
            "tax_id_present": bool(provider.get("taxId")),
        },
        "transactions": _transaction_types(item.get("transactions")),
        "open_tasks": open_tasks,
        "last_era_received_at": item.get("lastEraReceivedAt"),
        "requested_effective_date": item.get("requestedEffectiveDate"),
    }


async def search_payers(client: httpx.AsyncClient, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Search Stedi's Payer Network by name, payer ID, or alias (fuzzy)."""
    url = f"{STEDI_PAYERS_BASE}/payers/search"
    response = await client.get(
        url,
        params={"query": query, "pageSize": max(10, min(limit, 50))},
        headers=_auth_headers(),
    )
    if response.status_code in {401, 403}:
        raise PermissionError("Stedi authentication failed. Check STEDI_API_KEY.")
    response.raise_for_status()
    payload = response.json() or {}
    items = payload.get("items") or payload.get("results") or []
    out: list[dict[str, Any]] = []
    for raw in items:
        item = raw.get("payer") if isinstance(raw, dict) and isinstance(raw.get("payer"), dict) else raw
        enrollment_info = item.get("enrollment") or {}
        out.append(
            {
                "stedi_payer_id": item.get("stediId") or item.get("stediPayerId") or item.get("id"),
                "name": item.get("displayName") or item.get("name"),
                "primary_payer_id": item.get("primaryPayerId"),
                "aliases": item.get("aliases") or [],
                "names": item.get("names") or item.get("possibleNames") or [],
                "ptan_required": enrollment_info.get("ptanRequired"),
                "transaction_support": item.get("transactionSupport") or {},
            }
        )
    return out


async def list_enrollments(
    client: httpx.AsyncClient,
    *,
    npi: str,
    tin: str | None = None,
    payer_ids: list[str] | None = None,
    page_size: int = 100,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """List Stedi transaction enrollments for a provider NPI (and optional TIN / payer IDs)."""
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params: list[tuple[str, str]] = [("providerNpis", npi), ("pageSize", str(page_size))]
        if tin:
            params.append(("providerTaxIds", tin))
        for payer_id in payer_ids or []:
            params.append(("payerIds", payer_id))
        if page_token:
            params.append(("pageToken", page_token))
        url = f"{STEDI_ENROLLMENTS_BASE}/enrollments?{urlencode(params)}"
        response = await client.get(url, headers=_auth_headers())
        if response.status_code in {401, 403}:
            raise PermissionError("Stedi authentication failed. Check STEDI_API_KEY.")
        response.raise_for_status()
        payload = response.json() or {}
        items.extend(payload.get("items") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return items


async def lookup_enrollments(
    client: httpx.AsyncClient,
    npi: str,
    *,
    payer: str | None = None,
    tin: str | None = None,
) -> dict[str, Any]:
    if not is_configured():
        return {
            "configured": False,
            "message": NOT_CONFIGURED_MESSAGE,
            "matched_payers": [],
            "enrollments": [],
            "open_tasks": [],
            "error": None,
        }

    matched_payers: list[dict[str, Any]] = []
    payer_ids: list[str] = []
    try:
        if payer:
            # If the caller already passed a Stedi payer ID, still try search for confirmation.
            matched_payers = await search_payers(client, payer)
            payer_ids = [p["stedi_payer_id"] for p in matched_payers if p.get("stedi_payer_id")]
            if not payer_ids:
                # Fall back to using the raw value as a Stedi payer ID filter.
                payer_ids = [payer]
        records = await list_enrollments(client, npi=npi, tin=tin, payer_ids=payer_ids or None)
        enrollments = [_summarize_enrollment(item) for item in records]
        open_tasks: list[dict[str, Any]] = []
        for enrollment in enrollments:
            for task in enrollment.get("open_tasks") or []:
                open_tasks.append(
                    {
                        **task,
                        "enrollment_id": enrollment.get("id"),
                        "enrollment_status": enrollment.get("status"),
                        "payer_name": (enrollment.get("payer") or {}).get("name"),
                        "stedi_payer_id": (enrollment.get("payer") or {}).get("stedi_payer_id"),
                    }
                )
        statuses = sorted({e.get("status") for e in enrollments if e.get("status")})
        return {
            "configured": True,
            "message": None,
            "matched_payers": matched_payers,
            "enrollments": enrollments,
            "open_tasks": open_tasks,
            "enrollment_count": len(enrollments),
            "statuses": statuses,
            "error": None,
        }
    except PermissionError as exc:
        return {
            "configured": True,
            "message": str(exc),
            "matched_payers": matched_payers,
            "enrollments": [],
            "open_tasks": [],
            "error": str(exc),
        }
    except httpx.HTTPError as exc:
        return {
            "configured": True,
            "message": f"Stedi request failed: {exc.__class__.__name__}",
            "matched_payers": matched_payers,
            "enrollments": [],
            "open_tasks": [],
            "error": f"Stedi request failed: {exc.__class__.__name__}",
        }
