"""CMS NPPES NPI Registry client (no authentication)."""

from __future__ import annotations

from typing import Any

import httpx

from .registry import NPPES_URL, NPPES_VERSION


def _display_name(basic: dict[str, Any], enumeration_type: str | None) -> str | None:
    if enumeration_type == "NPI-2" or basic.get("organization_name"):
        org = basic.get("organization_name") or basic.get("name")
        if org:
            return str(org).strip()
    parts = [
        basic.get("name_prefix"),
        basic.get("first_name"),
        basic.get("middle_name"),
        basic.get("last_name"),
        basic.get("name_suffix") or basic.get("credential"),
    ]
    name = " ".join(p for p in parts if p)
    return name or None


def _primary_taxonomy(taxonomies: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not taxonomies:
        return None
    for row in taxonomies:
        if row.get("primary") is True or str(row.get("primary")).lower() in {"true", "y", "yes"}:
            return {
                "code": row.get("code"),
                "description": row.get("desc") or row.get("taxonomy_description"),
                "state": row.get("state"),
                "license": row.get("license"),
                "primary": True,
            }
    row = taxonomies[0]
    return {
        "code": row.get("code"),
        "description": row.get("desc") or row.get("taxonomy_description"),
        "state": row.get("state"),
        "license": row.get("license"),
        "primary": False,
    }


def _addresses(addresses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in addresses or []:
        out.append(
            {
                "purpose": row.get("address_purpose"),
                "address_1": row.get("address_1"),
                "address_2": row.get("address_2"),
                "city": row.get("city"),
                "state": row.get("state"),
                "postal_code": row.get("postal_code"),
                "country_code": row.get("country_code"),
                "telephone": row.get("telephone_number"),
                "fax": row.get("fax_number"),
            }
        )
    return out


def parse_nppes_result(npi: str, payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results") or []
    if not results:
        return {
            "npi": npi,
            "found": False,
            "name": None,
            "enumeration_type": None,
            "active": None,
            "primary_taxonomy": None,
            "practice_addresses": [],
            "mailing_addresses": [],
            "first_name": None,
            "last_name": None,
            "organization_name": None,
            "source": NPPES_URL,
        }
    row = results[0]
    basic = row.get("basic") or {}
    enumeration_type = row.get("enumeration_type")
    status = (basic.get("status") or "").upper()
    addresses = _addresses(row.get("addresses") or [])
    practice = [a for a in addresses if (a.get("purpose") or "").upper() == "LOCATION"]
    mailing = [a for a in addresses if (a.get("purpose") or "").upper() == "MAILING"]
    return {
        "npi": row.get("number") or npi,
        "found": True,
        "name": _display_name(basic, enumeration_type),
        "enumeration_type": enumeration_type,
        "active": status in {"", "A", "ACTIVE"} if status else None,
        "status": status or None,
        "primary_taxonomy": _primary_taxonomy(row.get("taxonomies") or []),
        "practice_addresses": practice or addresses,
        "mailing_addresses": mailing,
        "first_name": basic.get("first_name"),
        "last_name": basic.get("last_name"),
        "organization_name": basic.get("organization_name"),
        "credential": basic.get("credential"),
        "source": NPPES_URL,
        "result_count": payload.get("result_count"),
    }


async def lookup_nppes(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    try:
        response = await client.get(NPPES_URL, params={"version": NPPES_VERSION, "number": npi})
        response.raise_for_status()
        payload = response.json()
        parsed = parse_nppes_result(npi, payload)
        parsed["http_status"] = response.status_code
        parsed["error"] = None
        return parsed
    except httpx.HTTPError as exc:
        return {
            "npi": npi,
            "found": False,
            "name": None,
            "enumeration_type": None,
            "active": None,
            "primary_taxonomy": None,
            "practice_addresses": [],
            "mailing_addresses": [],
            "first_name": None,
            "last_name": None,
            "organization_name": None,
            "source": NPPES_URL,
            "error": f"NPPES request failed: {exc.__class__.__name__}",
        }
