"""Public HL7 Da Vinci PDEX Plan-Net FHIR directory lookups."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from .registry import FhirPayerConfig, LIVE_FHIR_PAYERS, live_payer_by_id

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
NETWORK_EXT_HINT = "network-reference"
MAX_ROLES = 50
MAX_NAME_MATCHES = 50
MAX_NETWORK_FOLLOWS = 12


def _entries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in bundle.get("entry") or []:
        resource = item.get("resource")
        if isinstance(resource, dict):
            out.append(resource)
    return out


def _npi_values(resource: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for ident in resource.get("identifier") or []:
        system = (ident.get("system") or "").lower()
        value = str(ident.get("value") or "").strip()
        if not value:
            continue
        if "npi" in system or (value.isdigit() and len(value) == 10):
            values.add(value)
    return values


def _human_name(name_field: Any) -> str | None:
    if not name_field:
        return None
    if isinstance(name_field, str):
        return name_field
    if isinstance(name_field, list) and name_field:
        name_field = name_field[0]
    if isinstance(name_field, dict):
        if name_field.get("text"):
            return str(name_field["text"])
        parts = list(name_field.get("given") or []) + [name_field.get("family") or ""]
        suffix = name_field.get("suffix") or []
        if isinstance(suffix, str):
            suffix = [suffix]
        parts.extend(suffix)
        return " ".join(p for p in parts if p) or None
    return None


def _ref_id(ref: Any) -> str | None:
    if not ref:
        return None
    if isinstance(ref, list):
        ref = ref[0] if ref else None
    if isinstance(ref, str):
        return ref.rstrip("/").split("/")[-1] or None
    if isinstance(ref, dict):
        reference = ref.get("reference") or ""
        if reference:
            return reference.rstrip("/").split("/")[-1]
    return None


def _ref_display(ref: Any) -> str | None:
    if isinstance(ref, dict):
        return ref.get("display")
    return None


def _collect_refs(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return [v for v in value if v]
    return [value]


def _network_refs(role: dict[str, Any]) -> list[Any]:
    refs = _collect_refs(role.get("network"))
    for ext in role.get("extension") or []:
        url = ext.get("url") or ""
        if NETWORK_EXT_HINT in url and ext.get("valueReference"):
            refs.append(ext["valueReference"])
    return refs


def _format_address(address: Any) -> dict[str, Any] | None:
    if not address:
        return None
    if isinstance(address, list):
        address = address[0] if address else None
    if not isinstance(address, dict):
        return None
    lines = address.get("line") or []
    if isinstance(lines, str):
        lines = [lines]
    return {
        "lines": lines,
        "city": address.get("city"),
        "state": address.get("state"),
        "postal_code": address.get("postalCode") or address.get("postal_code"),
        "country": address.get("country"),
        "text": address.get("text"),
    }


def _included_map(resources: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    for resource in resources:
        rtype = resource.get("resourceType") or ""
        rid = resource.get("id")
        if rtype and rid:
            mapping[(rtype, str(rid))] = resource
    return mapping


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any] | list[tuple[str, str]] | None = None) -> tuple[int, dict[str, Any] | None, str | None]:
    try:
        response = await client.get(url, params=params)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            issue = None
            if isinstance(payload, dict):
                issue = payload.get("issue")
            return response.status_code, payload if isinstance(payload, dict) else None, (
                f"HTTP {response.status_code}" + (f": {issue}" if issue else "")
            )
        if not isinstance(payload, dict):
            return response.status_code, None, "Non-JSON FHIR response"
        return response.status_code, payload, None
    except httpx.HTTPError as exc:
        return 0, None, f"{exc.__class__.__name__}"


def _match_practitioners(bundle: dict[str, Any], npi: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for resource in _entries(bundle):
        if resource.get("resourceType") != "Practitioner":
            continue
        if npi in _npi_values(resource):
            matched.append(resource)
    return matched


def _match_organizations(bundle: dict[str, Any], npi: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for resource in _entries(bundle):
        if resource.get("resourceType") != "Organization":
            continue
        if npi in _npi_values(resource):
            matched.append(resource)
    return matched


async def _search_practitioner(
    client: httpx.AsyncClient,
    payer: FhirPayerConfig,
    npi: str,
    provider: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    base = payer.base_url.rstrip("/") + "/"
    url = urljoin(base, "Practitioner")
    note = None

    if payer.search_strategy in {"identifier", "identifier_bare"}:
        queries: list[str] = []
        if payer.search_strategy == "identifier" and payer.use_identifier_system:
            queries.append(f"{NPI_SYSTEM}|{npi}")
        queries.append(npi)
        for ident in queries:
            status, bundle, error = await _get_json(client, url, {"identifier": ident, "_count": 25})
            if error:
                # Try the next identifier form before failing.
                note = error
                continue
            matched = _match_practitioners(bundle or {}, npi)
            if matched:
                return matched, None
        return [], note

    # name_then_npi
    first = (provider or {}).get("first_name")
    last = (provider or {}).get("last_name")
    org_name = (provider or {}).get("organization_name") or (provider or {}).get("name")
    params: dict[str, Any] = {"_count": 50}
    if last and first:
        params["family"] = last
        params["given"] = first
    elif last:
        params["family"] = last
    elif org_name:
        params["name"] = org_name
    else:
        return [], "NPPES name is required for this payer's Practitioner search"
    status, bundle, error = await _get_json(client, url, params)
    if error:
        return [], error
    matched = _match_practitioners(bundle or {}, npi)
    return matched[:MAX_NAME_MATCHES], None


async def _search_organization(
    client: httpx.AsyncClient,
    payer: FhirPayerConfig,
    npi: str,
    provider: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    base = payer.base_url.rstrip("/") + "/"
    url = urljoin(base, "Organization")
    if payer.search_strategy in {"identifier", "identifier_bare"}:
        ident = npi if payer.search_strategy == "identifier_bare" or not payer.use_identifier_system else f"{NPI_SYSTEM}|{npi}"
        status, bundle, error = await _get_json(client, url, {"identifier": ident, "_count": 10})
        if not error:
            matched = _match_organizations(bundle or {}, npi)
            if matched:
                return matched, None
            if payer.use_identifier_system and ident != npi:
                status, bundle, error = await _get_json(client, url, {"identifier": npi, "_count": 10})
                if not error:
                    return _match_organizations(bundle or {}, npi), None
        return [], error
    org_name = (provider or {}).get("organization_name") or (provider or {}).get("name")
    if not org_name:
        return [], None
    status, bundle, error = await _get_json(client, url, {"name": org_name, "_count": 25})
    if error:
        return [], error
    return _match_organizations(bundle or {}, npi), None


async def _search_roles(
    client: httpx.AsyncClient,
    payer: FhirPayerConfig,
    practitioner_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    base = payer.base_url.rstrip("/") + "/"
    url = urljoin(base, "PractitionerRole")
    prefixes = [f"Practitioner/{practitioner_id}"] if payer.practitioner_role_ref_prefix else [practitioner_id, f"Practitioner/{practitioner_id}"]
    last_error = None
    for value in prefixes:
        params: list[tuple[str, str]] = [("practitioner", value), ("_count", str(MAX_ROLES))]
        if payer.includes_supported:
            params.extend(
                [
                    ("_include", "PractitionerRole:network"),
                    ("_include", "PractitionerRole:organization"),
                    ("_include", "PractitionerRole:location"),
                ]
            )
        status, bundle, error = await _get_json(client, url, params)
        if error:
            last_error = error
            # Retry without includes if the server rejects them.
            if payer.includes_supported and status in {400, 404, 500, 501}:
                status, bundle, error = await _get_json(
                    client, url, {"practitioner": value, "_count": MAX_ROLES}
                )
                if error:
                    last_error = error
                    continue
            else:
                continue
        resources = _entries(bundle or {})
        roles = [r for r in resources if r.get("resourceType") == "PractitionerRole"]
        included = [r for r in resources if r.get("resourceType") != "PractitionerRole"]
        if roles or (bundle or {}).get("total") == 0:
            return roles[:MAX_ROLES], included, None
    return [], [], last_error


async def _resolve_networks(
    client: httpx.AsyncClient,
    payer: FhirPayerConfig,
    roles: list[dict[str, Any]],
    included: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    included_map = _included_map(included)
    networks: dict[str, dict[str, Any]] = {}

    def add(key: str, name: str | None, resource: dict[str, Any] | None = None) -> None:
        if key in networks:
            if name and not networks[key].get("name"):
                networks[key]["name"] = name
            return
        typ = None
        if resource:
            typings = resource.get("type") or []
            if typings:
                coding = typings[0].get("coding") if isinstance(typings[0], dict) else None
                if coding:
                    typ = coding[0].get("display") or coding[0].get("code")
        networks[key] = {"id": key, "name": name or key, "type": typ}

    follow_ids: list[str] = []
    for role in roles:
        for ref in _network_refs(role):
            rid = _ref_id(ref)
            display = _ref_display(ref)
            if not rid:
                if display:
                    add(display, display)
                continue
            resource = included_map.get(("Organization", rid)) or included_map.get(("Network", rid))
            if resource:
                add(rid, display or _human_name(resource.get("name")) or resource.get("name"), resource)
            elif display:
                add(rid, display)
            else:
                follow_ids.append(rid)

    # Follow a bounded number of unresolved network Organization reads.
    base = payer.base_url.rstrip("/") + "/"
    seen: set[str] = set()
    for rid in follow_ids:
        if rid in seen or len(seen) >= MAX_NETWORK_FOLLOWS:
            continue
        seen.add(rid)
        status, payload, error = await _get_json(client, urljoin(base, f"Organization/{rid}"))
        if error or not payload or payload.get("resourceType") not in {"Organization", "Network"}:
            add(rid, rid)
            continue
        add(rid, _human_name(payload.get("name")) or payload.get("name"), payload)

    return list(networks.values())


def _locations(roles: list[dict[str, Any]], included: list[dict[str, Any]]) -> list[dict[str, Any]]:
    included_map = _included_map(included)
    out: dict[str, dict[str, Any]] = {}
    for role in roles:
        for ref in _collect_refs(role.get("location")):
            rid = _ref_id(ref)
            display = _ref_display(ref)
            resource = included_map.get(("Location", rid or "")) if rid else None
            name = display or (resource.get("name") if resource else None)
            address = _format_address(resource.get("address") if resource else None)
            key = rid or name or display or "location"
            if key not in out:
                out[key] = {"id": rid, "name": name, "address": address}
    return list(out.values())[:40]


def _organizations(roles: list[dict[str, Any]], included: list[dict[str, Any]]) -> list[dict[str, Any]]:
    included_map = _included_map(included)
    out: dict[str, dict[str, Any]] = {}
    for role in roles:
        for ref in _collect_refs(role.get("organization")):
            rid = _ref_id(ref)
            display = _ref_display(ref)
            resource = included_map.get(("Organization", rid or "")) if rid else None
            # Skip resources typed as networks; those belong in networks[].
            typ = None
            if resource:
                typings = resource.get("type") or []
                codes = []
                for t in typings:
                    for c in t.get("coding") or []:
                        codes.append((c.get("code") or "").lower())
                if "ntwk" in codes or "network" in codes:
                    continue
            name = display or (resource.get("name") if resource else None)
            key = rid or name or "organization"
            if key not in out:
                out[key] = {"id": rid, "name": name}
    return list(out.values())[:20]


async def lookup_payer_network(
    client: httpx.AsyncClient,
    payer: FhirPayerConfig,
    npi: str,
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "payer_id": payer.id,
        "payer_name": payer.name,
        "base_url": payer.base_url,
        "search_strategy": payer.search_strategy,
        "queried": True,
        "found": False,
        "in_network": False,
        "active": None,
        "practitioner_id": None,
        "practitioner_name": None,
        "resource_type": None,
        "networks": [],
        "organizations": [],
        "locations": [],
        "roles_count": 0,
        "roles_truncated": False,
        "error": None,
        "notes": payer.notes or None,
    }
    practitioners, error = await _search_practitioner(client, payer, npi, provider)
    organizations: list[dict[str, Any]] = []
    if not practitioners:
        organizations, org_error = await _search_organization(client, payer, npi, provider)
        error = error or org_error

    if not practitioners and not organizations:
        result["error"] = error
        return result

    if practitioners:
        practitioner = practitioners[0]
        result["found"] = True
        result["in_network"] = True
        result["resource_type"] = "Practitioner"
        result["practitioner_id"] = practitioner.get("id")
        result["practitioner_name"] = _human_name(practitioner.get("name"))
        result["active"] = practitioner.get("active")
        roles, included, role_error = await _search_roles(client, payer, str(practitioner.get("id")))
        if role_error and not roles:
            result["error"] = f"Practitioner found, but PractitionerRole search failed: {role_error}"
        result["roles_count"] = len(roles)
        result["roles_truncated"] = len(roles) >= MAX_ROLES
        if roles:
            if any(role.get("active") is True for role in roles):
                result["active"] = True
            elif all(role.get("active") is False for role in roles):
                result["active"] = False
            result["networks"] = await _resolve_networks(client, payer, roles, included)
            result["organizations"] = _organizations(roles, included)
            result["locations"] = _locations(roles, included)
        return result

    organization = organizations[0]
    result["found"] = True
    result["in_network"] = True
    result["resource_type"] = "Organization"
    result["practitioner_id"] = organization.get("id")
    result["practitioner_name"] = organization.get("name") or _human_name(organization.get("name"))
    result["active"] = organization.get("active")
    result["organizations"] = [
        {"id": organization.get("id"), "name": organization.get("name")}
    ]
    return result


async def lookup_networks(
    client: httpx.AsyncClient,
    npi: str,
    *,
    payer_id: str | None = None,
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if payer_id:
        payer = live_payer_by_id(payer_id)
        payers = [payer] if payer else []
    else:
        payers = list(LIVE_FHIR_PAYERS)

    results: list[dict[str, Any]] = []
    # Query selected payers concurrently via a simple gather at the service layer
    # if needed; here we accept a list and the service will fan out.
    for payer in payers:
        results.append(await lookup_payer_network(client, payer, npi, provider))
    return {"results": results}
