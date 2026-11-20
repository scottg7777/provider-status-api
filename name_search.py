"""Friendly provider name search against the CMS NPPES NPI Registry API.

Builds several NPPES query variants from free text ("Dr. Christine Snoke",
"Snoke, Christine", "Aeterna Aesthetics LLC"), runs them concurrently, merges and
dedupes by NPI, and ranks by name similarity, location match, and active status.
"""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import Any

import httpx

from clients.nppes import _primary_taxonomy
from clients.registry import NPPES_URL, NPPES_VERSION

TITLES = {
    "DR", "DOCTOR", "MR", "MRS", "MS", "MISS", "PROF",
    "MD", "DO", "NP", "FNP", "FNPC", "FNPBC", "APRN", "ARNP", "CNP", "CRNP", "DNP", "PA", "PAC",
    "RN", "LPN", "PHD", "PSYD", "DDS", "DMD", "DPM", "DC", "OD", "PT", "DPT", "OT", "LCSW", "LPC",
    "LMFT", "MSW", "MPH", "MBA", "FACP", "FACS", "JR", "SR", "II", "III", "IV", "CRNA", "PHARMD",
    "AGNP", "ANP", "PMHNP", "WHNP", "BC", "C",
}
ENTITY_SUFFIXES = {
    "LLC", "PLLC", "PC", "PA", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "LTD", "LLP",
    "LP", "PSC", "SC", "PLC", "COMPANY",
}
ORG_WORDS = {
    "CLINIC", "CLINICS", "CENTER", "CENTRE", "CENTERS", "HEALTH", "HEALTHCARE", "MEDICAL", "MEDICINE",
    "GROUP", "ASSOCIATES", "HOSPITAL", "PRACTICE", "PARTNERS", "SERVICES", "CARE", "AESTHETICS",
    "DERMATOLOGY", "PEDIATRICS", "FAMILY", "THERAPY", "WELLNESS", "INSTITUTE", "PHARMACY", "LAB",
    "LABS", "LABORATORY", "IMAGING", "SURGERY", "SURGICAL", "DENTAL", "ORTHOPEDICS", "SPA", "MED",
    "BEHAVIORAL", "COUNSELING", "URGENT", "REHAB", "REHABILITATION", "HOME", "HOSPICE", "SYSTEM",
    "NETWORK", "UNIVERSITY", "COUNTY", "OF", "THE", "AND", "&",
}
MIN_NAME_SIMILARITY = 0.5  # drop wildcard noise that barely resembles the query
MIN_WILDCARD_PREFIX = 2  # NPPES: "Wildcards require at least two leading characters"


def clean_text(value: str) -> str:
    v = value.upper().replace("&", " AND ")
    v = re.sub(r"[.'’`\"]", "", v)
    v = re.sub(r"[^A-Z0-9,\- ]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def strip_suffixes(tokens: list[str]) -> list[str]:
    out = list(tokens)
    while out and out[-1] in ENTITY_SUFFIXES:
        out.pop()
    return out


def _is_title(token: str) -> bool:
    return token.replace("-", "") in TITLES


def parse_query(q: str, kind: str = "auto") -> dict[str, Any]:
    """Normalize the free-text query and guess organization vs individual."""
    raw = clean_text(q)
    parts = [p.strip() for p in raw.split(",")]
    # "Christine Snoke, FNP-C" -> drop trailing credential-only segments.
    while len(parts) > 1 and all(_is_title(t) or t in ENTITY_SUFFIXES for t in parts[-1].split()):
        parts.pop()
    raw = ", ".join(parts)
    comma = len(parts) > 1
    tokens = [t for t in re.split(r"[\s,]+", raw.replace("-", " - ")) if t and t != "-"]
    had_title = any(_is_title(t) for t in tokens) or len(clean_text(q).split(",")) > len(parts)
    had_suffix = any(t in ENTITY_SUFFIXES for t in tokens)
    org_tokens = strip_suffixes([t for t in re.split(r"[\s,]+", raw) if t and not (_is_title(t) and t not in {"PA", "PC"})])
    person_tokens = [t for t in tokens if not _is_title(t) and t not in ENTITY_SUFFIXES and len(t) > 0]
    # Drop single-letter middle initials for person search but keep them for display.
    person_core = [t for t in person_tokens if len(t) > 1] or person_tokens
    has_org_word = any(t in ORG_WORDS for t in tokens)

    detected = kind
    if kind not in {"organization", "individual"}:
        if had_suffix or has_org_word or len(person_core) >= 4:
            detected = "organization"
        elif had_title or comma:
            detected = "individual"
        else:
            detected = "both"  # "Snoke Christine" or "Aeterna" could be either

    first = last = None
    reversed_pair = None
    if comma and len(parts) >= 2:
        last_tokens = [t for t in parts[0].split() if not _is_title(t)]
        first_tokens = [t for t in parts[1].split() if not _is_title(t) and len(t) > 1]
        last = " ".join(last_tokens) or None
        first = first_tokens[0] if first_tokens else None
    elif len(person_core) >= 2:
        first, last = person_core[0], person_core[-1]
        reversed_pair = (person_core[-1], person_core[0])
    elif len(person_core) == 1:
        last = person_core[0]

    return {
        "raw": q,
        "clean": raw,
        "kind": detected,
        "org_name": " ".join(org_tokens).replace(" - ", "-") or None,
        "first": first,
        "last": last,
        "reversed": reversed_pair,
        "had_title": had_title,
        "had_suffix": had_suffix,
    }


def _wild(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    return f"{v}*" if len(v) >= MIN_WILDCARD_PREFIX else None


def build_variants(parsed: dict[str, Any], *, state: str | None = None, city: str | None = None) -> list[dict[str, Any]]:
    """Return NPPES query param sets (deduped) with a short label each."""
    loc: dict[str, str] = {}
    if state:
        loc["state"] = state.upper()
    if city:
        loc["city"] = city.upper()
    variants: list[tuple[str, dict[str, str]]] = []
    kind = parsed["kind"]
    if kind in {"organization", "both"} and parsed.get("org_name"):
        org = parsed["org_name"]
        variants.append(("organization name", {"organization_name": org, "enumeration_type": "NPI-2"}))
        if _wild(org):
            variants.append(("organization name*", {"organization_name": _wild(org), "enumeration_type": "NPI-2"}))
        words = org.split()
        if kind == "organization" and len(words) > 1 and _wild(words[0]) and words[0] not in ORG_WORDS:
            variants.append(("first word*", {"organization_name": _wild(words[0]), "enumeration_type": "NPI-2"}))
    if kind in {"individual", "both"} and parsed.get("last"):
        first, last = parsed.get("first"), parsed["last"]
        if first:
            variants.append(("first + last", {"first_name": first, "last_name": last, "enumeration_type": "NPI-1"}))
            if _wild(first):
                variants.append(("first* + last", {"first_name": _wild(first), "last_name": last, "enumeration_type": "NPI-1"}))
        else:
            variants.append(("last name", {"last_name": last, "enumeration_type": "NPI-1"}))
        rev = parsed.get("reversed")
        if rev:
            variants.append(("last + first (reversed)", {"first_name": rev[0], "last_name": rev[1], "enumeration_type": "NPI-1"}))
        if _wild(last) and first:
            variants.append(("first + last*", {"first_name": first, "last_name": _wild(last), "enumeration_type": "NPI-1"}))
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for label, params in variants:
        p = {**params, **loc}
        key = tuple(sorted(p.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "params": p})
    return out


async def nppes_search(client: httpx.AsyncClient, params: dict[str, Any], *, limit: int = 50, skip: int = 0) -> tuple[list[dict[str, Any]], str | None]:
    query = {"version": NPPES_VERSION, "limit": limit, **params}
    if skip:
        query["skip"] = skip
    try:
        response = await client.get(NPPES_URL, params=query)
        response.raise_for_status()
        payload = response.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"NPPES search failed: {exc.__class__.__name__}"
    if payload.get("Errors"):
        return [], "; ".join(str(e.get("description")) for e in payload["Errors"])
    return payload.get("results") or [], None


def _location(row: dict[str, Any]) -> dict[str, Any] | None:
    addrs = row.get("addresses") or []
    loc = next((a for a in addrs if (a.get("address_purpose") or "").upper() == "LOCATION"), None) or (addrs[0] if addrs else None)
    if not loc:
        return None
    return {
        "address_1": loc.get("address_1"),
        "address_2": loc.get("address_2"),
        "city": loc.get("city"),
        "state": loc.get("state"),
        "postal_code": loc.get("postal_code"),
        "telephone": loc.get("telephone_number"),
    }


def summarize_result(row: dict[str, Any]) -> dict[str, Any]:
    basic = row.get("basic") or {}
    etype = row.get("enumeration_type")
    is_org = etype == "NPI-2"
    if is_org:
        name = basic.get("organization_name")
    else:
        name = " ".join(p for p in [basic.get("first_name"), basic.get("middle_name"), basic.get("last_name")] if p)
    other_names = []
    for o in row.get("other_names") or []:
        n = o.get("organization_name") or " ".join(p for p in [o.get("first_name"), o.get("last_name")] if p)
        if n:
            other_names.append({"name": n, "type": o.get("type") or o.get("code")})
    tax = _primary_taxonomy(row.get("taxonomies") or [])
    status = (basic.get("status") or "").upper()
    auth = None
    if is_org and basic.get("authorized_official_last_name"):
        auth = {
            "first_name": basic.get("authorized_official_first_name"),
            "last_name": basic.get("authorized_official_last_name"),
            "title": basic.get("authorized_official_title_or_position"),
        }
    return {
        "npi": str(row.get("number")),
        "enumeration_type": etype,
        "type": "organization" if is_org else "individual",
        "name": name,
        "first_name": None if is_org else basic.get("first_name"),
        "last_name": None if is_org else basic.get("last_name"),
        "organization_name": basic.get("organization_name") if is_org else None,
        "credential": basic.get("credential"),
        "other_names": other_names,
        "primary_taxonomy": tax,
        "practice_address": _location(row),
        "enumeration_date": basic.get("enumeration_date"),
        "last_updated": basic.get("last_updated"),
        "status": status or None,
        "active": status in {"", "A"},
        "authorized_official": auth,
    }


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    prefix = 1.0 if b.startswith(a) or a.startswith(b) else 0.0
    return max(ratio, jacc, 0.9 * prefix if prefix else 0.0)


def score_result(item: dict[str, Any], parsed: dict[str, Any], *, state: str | None, city: str | None) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if item["type"] == "organization":
        q = " ".join(strip_suffixes((parsed.get("org_name") or parsed["clean"]).split()))
        names = [item.get("organization_name") or ""] + [o["name"] for o in item.get("other_names") or []]
        best = max((_sim(q, " ".join(strip_suffixes(clean_text(n).split()))) for n in names if n), default=0.0)
        if best >= 0.999:
            reasons.append("exact organization name")
        elif best >= 0.75:
            reasons.append("close organization name")
        name_score = best
    else:
        first = clean_text(item.get("first_name") or "")
        last = clean_text(item.get("last_name") or "")
        qf, ql = parsed.get("first") or "", parsed.get("last") or ""
        direct = 0.6 * _sim(ql, last) + 0.4 * (_sim(qf, first) if qf else 0.5)
        rev = parsed.get("reversed")
        swapped = 0.6 * _sim(rev[1], last) + 0.4 * _sim(rev[0], first) if rev else 0.0
        name_score = max(direct, swapped)
        if name_score >= 0.999:
            reasons.append("exact first + last name" if direct >= swapped else "exact name (reversed order)")
        elif name_score >= 0.75:
            reasons.append("close name")
    item["_name_score"] = name_score
    score = 80 * name_score
    addr = item.get("practice_address") or {}
    if state and (addr.get("state") or "").upper() == state.upper():
        score += 8
        reasons.append(f"state {state.upper()}")
    if city and clean_text(addr.get("city") or "") == clean_text(city):
        score += 6
        reasons.append(f"city {city.title()}")
    if item.get("active"):
        score += 6
        reasons.append("active NPI")
    else:
        reasons.append("deactivated NPI")
    if parsed["kind"] in {"organization", "individual"} and item["type"] == parsed["kind"]:
        score += 4
    return int(round(min(score, 100))), reasons


async def search_by_name(
    client: httpx.AsyncClient,
    q: str,
    *,
    state: str | None = None,
    city: str | None = None,
    kind: str = "auto",
    limit: int = 25,
) -> dict[str, Any]:
    parsed = parse_query(q, kind)
    variants = build_variants(parsed, state=state, city=city)
    results = await asyncio.gather(*(nppes_search(client, v["params"]) for v in variants))
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for variant, (rows, error) in zip(variants, results):
        if error:
            errors.append(f"{variant['label']}: {error}")
        for row in rows:
            item = summarize_result(row)
            existing = merged.get(item["npi"])
            if existing:
                existing["matched_variants"].append(variant["label"])
            else:
                item["matched_variants"] = [variant["label"]]
                merged[item["npi"]] = item
    ranked = []
    for item in merged.values():
        score, reasons = score_result(item, parsed, state=state, city=city)
        item["score"] = score
        item["match_reasons"] = reasons
        if item.pop("_name_score", 0) >= MIN_NAME_SIMILARITY or len(merged) <= 3:
            ranked.append(item)
    ranked.sort(key=lambda r: (-r["score"], r.get("name") or ""))
    return {
        "query": q,
        "state": state,
        "city": city,
        "parsed": {k: parsed[k] for k in ("clean", "kind", "org_name", "first", "last")},
        "variants": [{"label": v["label"], "params": v["params"]} for v in variants],
        "total_candidates": len(ranked),
        "results": ranked[:limit],
        "errors": errors,
        "source": NPPES_URL,
    }


def _street_key(address_1: str | None) -> str:
    """'7242 TYLERS CORNER DR STE B' -> '7242 TYLERS CORNER'"""
    tokens = clean_text(address_1 or "").split()
    tokens = [t for t in tokens if t not in {"STE", "SUITE", "UNIT", "APT", "#", "FL", "FLOOR", "BLDG", "RM"}]
    return " ".join(tokens[:3])


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


async def find_affiliated(client: httpx.AsyncClient, org: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
    """Individual NPIs likely affiliated with an organization (same practice address/phone,
    or the authorized official). ``org`` is a summarize_result() dict."""
    addr = org.get("practice_address") or {}
    zip_full = _digits(addr.get("postal_code"))
    street = _street_key(addr.get("address_1"))
    phone = _digits(addr.get("telephone"))
    tasks = []
    labels = []
    if len(zip_full) >= 5:
        tasks.append(nppes_search(client, {"enumeration_type": "NPI-1", "postal_code": zip_full}, limit=200))
        labels.append("zip")
    auth = org.get("authorized_official") or {}
    if auth.get("last_name") and auth.get("first_name"):
        params = {"enumeration_type": "NPI-1", "first_name": auth["first_name"], "last_name": auth["last_name"]}
        if addr.get("state"):
            params["state"] = addr["state"]
        tasks.append(nppes_search(client, params, limit=20))
        labels.append("official")
    results = await asyncio.gather(*tasks)
    people: dict[str, dict[str, Any]] = {}
    errors = []
    for label, (rows, error) in zip(labels, results):
        if error:
            errors.append(error)
        for row in rows:
            item = summarize_result(row)
            reasons: list[str] = []
            for a in row.get("addresses") or []:
                if street and _street_key(a.get("address_1")) == street and _digits(a.get("postal_code"))[:5] == zip_full[:5]:
                    reasons.append("same practice address" if (a.get("address_purpose") or "").upper() == "LOCATION" else "same mailing address")
                    if clean_text(a.get("address_1") or "") == clean_text(addr.get("address_1") or ""):
                        reasons[-1] += " (same suite)"
                if phone and _digits(a.get("telephone_number")) == phone:
                    reasons.append("same phone")
            if label == "official":
                ia = item.get("practice_address") or {}
                if _street_key(ia.get("address_1")) == street or reasons:
                    reasons.append("authorized official of the organization")
                else:
                    reasons.append("same name as the organization's authorized official (different address)")
            if not reasons:
                continue
            existing = people.get(item["npi"])
            if existing:
                existing["affiliation_reasons"] = sorted(set(existing["affiliation_reasons"] + reasons))
            else:
                item["affiliation_reasons"] = sorted(set(reasons))
                people[item["npi"]] = item
    ranked = sorted(
        people.values(),
        key=lambda p: (-sum(2 if "suite" in r or "official of" in r else 1 for r in p["affiliation_reasons"]), p.get("name") or ""),
    )
    return {
        "organization_npi": org.get("npi"),
        "organization_name": org.get("name"),
        "basis": {"street": street or None, "postal_code": zip_full or None, "phone": phone or None},
        "results": ranked[:limit],
        "errors": errors,
        "note": "Likely affiliations from NPPES address/phone matches. NPPES doesn't record employment, so verify.",
    }
