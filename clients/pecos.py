"""CMS Medicare FFS Public Provider Enrollment (PECOS) client. No authentication."""

from __future__ import annotations

from typing import Any

import httpx

from .registry import PECOS_DATA_URL, PECOS_DATASET_ID, PECOS_DATASET_NAME


def _enrollment_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "npi": row.get("NPI"),
        "pac_id": row.get("PECOS_ASCT_CNTL_ID"),
        "enrollment_id": row.get("ENRLMT_ID"),
        "provider_type_code": row.get("PROVIDER_TYPE_CD"),
        "provider_type": row.get("PROVIDER_TYPE_DESC"),
        "state": row.get("STATE_CD"),
        "first_name": row.get("FIRST_NAME") or None,
        "middle_name": row.get("MDL_NAME") or None,
        "last_name": row.get("LAST_NAME") or None,
        "organization_name": row.get("ORG_NAME") or None,
        "multiple_npi_flag": row.get("MULTIPLE_NPI_FLAG") or None,
    }


async def lookup_pecos(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    """
    Look up an NPI in the current Medicare FFS Public Provider Enrollment dataset.

    Presence in this dataset means the provider was actively approved to bill
    Medicare FFS or had completed an 855O at the time CMS extracted PECOS.
    """
    try:
        response = await client.get(
            PECOS_DATA_URL,
            params={"filter[NPI]": npi, "size": 100},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            payload = payload.get("data") or payload.get("items") or []
        enrollments = [_enrollment_row(row) for row in payload if isinstance(row, dict)]
        types = sorted({e["provider_type"] for e in enrollments if e.get("provider_type")})
        states = sorted({e["state"] for e in enrollments if e.get("state")})
        name = None
        if enrollments:
            first = enrollments[0]
            name = " ".join(
                p for p in [first.get("first_name"), first.get("middle_name"), first.get("last_name")] if p
            ) or first.get("organization_name")
        return {
            "npi": npi,
            "enrolled": bool(enrollments),
            "enrollment_count": len(enrollments),
            "enrollments": enrollments,
            "provider_types": types,
            "states": states,
            "name": name,
            "dataset": PECOS_DATASET_NAME,
            "dataset_id": PECOS_DATASET_ID,
            "source": PECOS_DATA_URL,
            "http_status": response.status_code,
            "error": None,
        }
    except httpx.HTTPError as exc:
        return {
            "npi": npi,
            "enrolled": None,
            "enrollment_count": 0,
            "enrollments": [],
            "provider_types": [],
            "states": [],
            "name": None,
            "dataset": PECOS_DATASET_NAME,
            "dataset_id": PECOS_DATASET_ID,
            "source": PECOS_DATA_URL,
            "error": f"CMS PECOS request failed: {exc.__class__.__name__}",
        }
