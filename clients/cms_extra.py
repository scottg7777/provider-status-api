"""Extra free CMS data.cms.gov sources: Order & Referring, reassignment, revalidation, opt-out."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from .registry import (
    CMS_DATA_API,
    CMS_OPT_OUT_ID,
    CMS_ORDER_REFERRING_ID,
    CMS_REASSIGNMENT_ID,
    CMS_REVALIDATION_DUE_ID,
)

SOURCES = {
    "order_referring": ("Medicare Order and Referring", CMS_ORDER_REFERRING_ID),
    "reassignment": ("Medicare Revalidation Reassignment List", CMS_REASSIGNMENT_ID),
    "revalidation": ("Medicare Revalidation Due Date List", CMS_REVALIDATION_DUE_ID),
    "opt_out": ("Medicare Opt Out Affidavits", CMS_OPT_OUT_ID),
}


def source_url(key: str) -> str:
    return f"{CMS_DATA_API}/{SOURCES[key][1]}/data"


async def _query(client: httpx.AsyncClient, key: str, field: str, value: str, size: int = 100) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = await client.get(source_url(key), params={f"filter[{field}]": value, "size": size})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("data") or []
        return [r for r in payload if isinstance(r, dict)], None
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"{SOURCES[key][0]} request failed: {exc.__class__.__name__}"


def _base(key: str) -> dict[str, Any]:
    return {"dataset": SOURCES[key][0], "source": source_url(key)}


def _yn(value: Any) -> bool | None:
    v = str(value or "").strip().upper()
    return True if v == "Y" else False if v == "N" else None


async def lookup_order_referring(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    rows, error = await _query(client, "order_referring", "NPI", npi, size=5)
    row = rows[0] if rows else {}
    return {
        **_base("order_referring"),
        "found": bool(rows) if not error else None,
        "part_b": _yn(row.get("PARTB")),
        "dme": _yn(row.get("DME")),
        "hha": _yn(row.get("HHA")),
        "pmd": _yn(row.get("PMD")),
        "hospice": _yn(row.get("HOSPICE")),
        "error": error,
    }


def _reassign_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_pac_id": row.get("Group PAC ID"),
        "group_enrollment_id": row.get("Group Enrollment ID"),
        "group_name": row.get("Group Legal Business Name"),
        "group_state": row.get("Group State Code"),
        "group_due_date": row.get("Group Due Date"),
        "record_type": row.get("Record Type"),
        "individual_npi": row.get("Individual NPI"),
        "individual_name": " ".join(p for p in [row.get("Individual First Name"), row.get("Individual Last Name")] if p) or None,
        "individual_specialty": row.get("Individual Specialty Description"),
        "individual_due_date": row.get("Individual Due Date"),
    }


async def lookup_reassignments(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    """Groups an individual NPI has reassigned Medicare benefits to."""
    rows, error = await _query(client, "reassignment", "Individual NPI", npi)
    items = [_reassign_row(r) for r in rows]
    return {**_base("reassignment"), "found": bool(items) if not error else None, "reassignments": items, "error": error}


async def lookup_group_reassignments(client: httpx.AsyncClient, pac_id: str) -> dict[str, Any]:
    """Individuals who reassign Medicare benefits to a group PAC ID."""
    rows, error = await _query(client, "reassignment", "Group PAC ID", pac_id, size=200)
    items = [_reassign_row(r) for r in rows]
    return {**_base("reassignment"), "found": bool(items) if not error else None, "members": items, "error": error}


async def lookup_revalidation(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    rows, error = await _query(client, "revalidation", "National Provider Identifier", npi, size=20)
    items = [
        {
            "enrollment_id": r.get("Enrollment ID"),
            "state": r.get("Enrollment State Code"),
            "provider_type": r.get("Provider Type Text"),
            "specialty": r.get("Enrollment Specialty"),
            "revalidation_due_date": r.get("Revalidation Due Date") or None,
            "adjusted_due_date": r.get("Adjusted Due Date") or None,
        }
        for r in rows
    ]
    return {**_base("revalidation"), "found": bool(items) if not error else None, "enrollments": items, "error": error}


def _parse_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


async def lookup_opt_out(client: httpx.AsyncClient, npi: str, *, today: date | None = None) -> dict[str, Any]:
    rows, error = await _query(client, "opt_out", "NPI", npi, size=10)
    today = today or date.today()
    items = []
    active = False
    for r in rows:
        start = _parse_date(r.get("Optout Effective Date"))
        end = _parse_date(r.get("Optout End Date"))
        is_active = bool(start and start <= today and (end is None or end >= today))
        active = active or is_active
        items.append(
            {
                "specialty": r.get("Specialty"),
                "effective_date": r.get("Optout Effective Date"),
                "end_date": r.get("Optout End Date"),
                "active": is_active,
                "eligible_to_order_and_refer": _yn(r.get("Eligible to Order and Refer")),
                "last_updated": r.get("Last Updated"),
            }
        )
    return {
        **_base("opt_out"),
        "found": bool(items) if not error else None,
        "active_opt_out": active if not error else None,
        "affidavits": items,
        "error": error,
    }
