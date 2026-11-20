"""Readiness-to-bill report for one NPI.

Runs every existing check (NPPES, Medicare PECOS, Plan-Net FHIR directories, Stedi
transaction enrollments) plus extra free public sources (OIG LEIE exclusions, CMS
opt-out affidavits, Order & Referring, Medicare reassignment, revalidation dates,
Stedi payer transaction support) in parallel, then derives per-payer and overall
verdicts. No eligibility (270) calls are made here and no PHI is involved.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from clients import cms_extra
from clients import stedi as stedi_client
from clients.leie import LEIE
from clients.registry import LIVE_FHIR_PAYERS, NPPES_URL, PECOS_DATA_URL
from service import lookup_provider_status

READY = "READY"
LIKELY_READY = "LIKELY_READY"
NOT_READY = "NOT_READY"
UNKNOWN = "UNKNOWN"
STATUS_LABELS = {READY: "Ready", LIKELY_READY: "Likely ready", NOT_READY: "Not ready", UNKNOWN: "Unknown"}

# Stedi payer IDs for national payers that also have a public FHIR directory here.
FHIR_TO_STEDI_PAYER = {"cigna": "62308", "unitedhealthcare": "87726", "humana": "61101"}

LIVE_ENROLLMENT = {"LIVE"}
PENDING_ENROLLMENT = {"SUBMITTED", "PROVISIONING", "DRAFT", "STEDI_ACTION_REQUIRED", "PROVIDER_ACTION_REQUIRED"}
CLAIM_TX = {"837P Professional Claims", "837I Institutional Claims", "837D Dental Claims"}
ERA_TX = {"835 ERA"}

CAVEAT = (
    "Verdicts combine public directories and EDI enrollment records. They are strong signals, "
    "not proof of a payer contract, credentialing approval, or effective date. Public files lag "
    "(PECOS and the reassignment list are periodic extracts; payer directories can be weeks behind)."
)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _fhir_aliases(payer_id: str) -> set[str]:
    cfg = next((p for p in LIVE_FHIR_PAYERS if p.id == payer_id), None)
    if not cfg:
        return {_norm(payer_id)}
    return {_norm(a) for a in (cfg.id, cfg.name, *cfg.aliases)}


def _enrollment_matches_fhir(enrollment: dict[str, Any], payer_id: str) -> bool:
    name = _norm((enrollment.get("payer") or {}).get("name"))
    stedi_id = (enrollment.get("payer") or {}).get("stedi_payer_id")
    if FHIR_TO_STEDI_PAYER.get(payer_id) and stedi_id == FHIR_TO_STEDI_PAYER.get(payer_id):
        return True
    return any(alias and alias in name for alias in _fhir_aliases(payer_id) if len(alias) >= 3)


def _enrollment_evidence(enrollments: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """Best Stedi enrollment status for a payer + reasons."""
    if not enrollments:
        return None, []
    reasons = []
    statuses = set()
    for e in enrollments:
        status = (e.get("status") or "").upper()
        statuses.add(status)
        tx = ", ".join(t.get("label") for t in e.get("transactions") or []) or "transactions not listed"
        reasons.append(f"Stedi enrollment {status or 'UNKNOWN'} ({tx})")
    if statuses & LIVE_ENROLLMENT:
        return "LIVE", reasons
    if statuses & PENDING_ENROLLMENT:
        return "PENDING", reasons
    if "REJECTED" in statuses:
        return "REJECTED", reasons
    return "OTHER", reasons


def _support_note(support: dict[str, Any] | None) -> list[str]:
    if not support:
        return []
    notes = []
    claims = str(support.get("professional_claims_support") or "").upper()
    era = str(support.get("era_support") or "").upper()
    if claims == "ENROLLMENT_REQUIRED":
        notes.append("Stedi: payer requires enrollment before 837P claims")
    elif claims == "NOT_SUPPORTED":
        notes.append("Stedi: 837P claims not supported for this payer ID")
    if era == "ENROLLMENT_REQUIRED":
        notes.append("Stedi: ERA (835) requires enrollment")
    return notes


def medicare_verdict(
    *, pecos: dict[str, Any], opt_out: dict[str, Any], reassign: dict[str, Any], revalidation: dict[str, Any],
    is_org: bool, group_members: dict[str, Any] | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    sources = [PECOS_DATA_URL, opt_out.get("source")]
    if opt_out.get("active_opt_out"):
        return {"status": NOT_READY, "reasons": ["Active Medicare opt-out affidavit: can't bill Medicare FFS"], "sources": sources}
    if pecos.get("error"):
        return {"status": UNKNOWN, "reasons": [pecos["error"]], "sources": sources}
    if pecos.get("enrolled"):
        types = ", ".join(pecos.get("provider_types") or []) or "enrolled"
        reasons.append(f"Found in Medicare FFS PECOS public enrollment ({types})")
        groups = [r.get("group_name") for r in reassign.get("reassignments") or [] if r.get("group_name")]
        if groups:
            reasons.append("Reassigns Medicare benefits to: " + "; ".join(sorted(set(groups))[:5]))
            sources.append(reassign.get("source"))
        if is_org and group_members and group_members.get("members"):
            reasons.append(f"{len(group_members['members'])} individual(s) reassign benefits to this group")
            sources.append(group_members.get("source"))
        due = [e.get("adjusted_due_date") or e.get("revalidation_due_date") for e in revalidation.get("enrollments") or []]
        due = [d for d in due if d and d != "TBD"]
        if due:
            reasons.append(f"Revalidation due: {', '.join(sorted(set(due)))}")
            sources.append(revalidation.get("source"))
        return {"status": READY, "reasons": reasons, "sources": sources}
    reasons.append("Not in the Medicare FFS PECOS public enrollment file")
    if not is_org and not (reassign.get("reassignments")):
        reasons.append("No Medicare reassignment on file")
    reasons.append("New enrollments can take weeks to appear in the public extract")
    return {"status": NOT_READY, "reasons": reasons, "sources": sources}


def payer_verdict(
    *, fhir: dict[str, Any] | None, enrollments: list[dict[str, Any]], support: dict[str, Any] | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    sources: list[str] = []
    listed = None
    if fhir is not None:
        sources.append(fhir.get("base_url"))
        if fhir.get("in_network") and fhir.get("active") is not False:
            listed = True
            nets = ", ".join((n.get("name") or n.get("id") or "") for n in (fhir.get("networks") or [])[:3])
            reasons.append("Listed in payer's public provider directory" + (f" ({nets})" if nets else ""))
        elif fhir.get("in_network") and fhir.get("active") is False:
            listed = False
            reasons.append("Listed in payer directory but marked inactive")
        elif fhir.get("error") and not fhir.get("found"):
            reasons.append(f"Directory lookup failed: {fhir['error']}")
        else:
            listed = False
            reasons.append("Not found in payer's public directory (absence isn't proof)")
    edi, edi_reasons = _enrollment_evidence(enrollments)
    reasons.extend(edi_reasons)
    if enrollments:
        sources.append("Stedi Enrollments API")
    if not enrollments and support:
        reasons.append("No Stedi transaction enrollment on file")
    reasons.extend(_support_note(support))

    if edi == "REJECTED":
        status = NOT_READY
    elif edi == "LIVE" and listed:
        status = READY
    elif edi == "LIVE" or listed:
        status = LIKELY_READY
    elif edi == "PENDING":
        status = NOT_READY
        reasons.append("EDI enrollment still in progress")
    elif listed is False and fhir and fhir.get("active") is False:
        status = NOT_READY
    else:
        status = UNKNOWN
    return {"status": status, "reasons": reasons, "sources": [s for s in sources if s]}


def overall_verdict(*, provider: dict[str, Any], leie: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    blockers: list[str] = []
    if not provider.get("found"):
        blockers.append("NPI not found in NPPES")
    elif provider.get("active") is False:
        blockers.append("NPI is deactivated in NPPES")
    if leie.get("excluded"):
        blockers.append("NPI appears on the OIG LEIE exclusion list (can't bill federal health programs)")
    if blockers:
        return {"status": NOT_READY, "headline": "Not ready to bill", "blockers": blockers}
    ready = [r["payer"] for r in rows if r["status"] == READY]
    likely = [r["payer"] for r in rows if r["status"] == LIKELY_READY]
    if ready:
        head = "Ready to bill " + ", ".join(ready)
        if likely:
            head += "; likely ready for " + ", ".join(likely)
        return {"status": READY, "headline": head, "blockers": []}
    if likely:
        return {"status": LIKELY_READY, "headline": "Likely ready for " + ", ".join(likely), "blockers": []}
    if all(r["status"] == UNKNOWN for r in rows):
        return {"status": UNKNOWN, "headline": "Not enough public evidence to tell", "blockers": []}
    return {"status": NOT_READY, "headline": "No payer shows this NPI ready to bill yet", "blockers": []}


async def _stedi_support(client: httpx.AsyncClient, payer_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not stedi_client.is_configured() or not payer_ids:
        return {}
    from eligibility import _payer_option, pick_payer

    async def one(pid: str):
        try:
            matches = await stedi_client.search_payers(client, pid, limit=10)
        except (PermissionError, httpx.HTTPError):
            return pid, None
        selected, _ = pick_payer(pid, matches)
        return pid, _payer_option(selected) if selected else None

    pairs = await asyncio.gather(*(one(p) for p in payer_ids))
    return {pid: opt for pid, opt in pairs if opt}


async def _leie_lookup(client: httpx.AsyncClient, provider_task: asyncio.Task) -> dict[str, Any]:
    await LEIE.ensure_loaded(client)
    provider = await provider_task
    addr = (provider.get("practice_addresses") or [{}])[0] if provider.get("practice_addresses") else {}
    return LEIE.lookup(
        provider.get("npi") or "",
        first_name=provider.get("first_name"),
        last_name=provider.get("last_name"),
        organization_name=provider.get("organization_name"),
        state=addr.get("state"),
    )


async def build_report(client: httpx.AsyncClient, npi: str) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    status_task = asyncio.create_task(lookup_provider_status(client, npi))

    async def provider_only() -> dict[str, Any]:
        from clients.nppes import lookup_nppes

        return await lookup_nppes(client, npi)

    provider_task = asyncio.create_task(provider_only())
    extra = await asyncio.gather(
        cms_extra.lookup_opt_out(client, npi),
        cms_extra.lookup_order_referring(client, npi),
        cms_extra.lookup_reassignments(client, npi),
        cms_extra.lookup_revalidation(client, npi),
        _leie_lookup(client, provider_task),
    )
    opt_out, order_ref, reassign, revalidation, leie = extra
    status = await status_task
    provider = status.get("provider") or {}
    pecos = status.get("medicare_pecos") or {}
    is_org = provider.get("enumeration_type") == "NPI-2"

    group_members = None
    pac_ids = sorted({e.get("pac_id") for e in pecos.get("enrollments") or [] if e.get("pac_id")})
    if is_org and pac_ids:
        group_members = await cms_extra.lookup_group_reassignments(client, pac_ids[0])

    stedi = status.get("stedi_edi_enrollment") or {}
    enrollments = stedi.get("enrollments") or []
    support_ids = sorted(
        set(FHIR_TO_STEDI_PAYER.values())
        | {(e.get("payer") or {}).get("stedi_payer_id") for e in enrollments if (e.get("payer") or {}).get("stedi_payer_id")}
    )
    support = await _stedi_support(client, support_ids)

    rows: list[dict[str, Any]] = []
    med = medicare_verdict(
        pecos=pecos, opt_out=opt_out, reassign=reassign, revalidation=revalidation,
        is_org=is_org, group_members=group_members,
    )
    rows.append({"payer": "Medicare (FFS)", "payer_key": "medicare", "eligibility_payer": "CMS", **med})

    used: set[str] = set()
    for fhir in (status.get("network_status") or {}).get("results") or []:
        pid = fhir.get("payer_id")
        matched = [e for e in enrollments if _enrollment_matches_fhir(e, pid)]
        used.update(e.get("id") for e in matched)
        sid = FHIR_TO_STEDI_PAYER.get(pid) or next(((e.get("payer") or {}).get("stedi_payer_id") for e in matched), None)
        verdict = payer_verdict(fhir=fhir, enrollments=matched, support=support.get(sid) if sid else None)
        rows.append({
            "payer": fhir.get("payer_name") or pid,
            "payer_key": pid,
            "eligibility_payer": sid or fhir.get("payer_name"),
            "stedi_support": support.get(sid) if sid else None,
            **verdict,
        })
    by_payer: dict[str, list[dict[str, Any]]] = {}
    for e in enrollments:
        if e.get("id") in used:
            continue
        key = (e.get("payer") or {}).get("stedi_payer_id") or (e.get("payer") or {}).get("name") or "unknown"
        by_payer.setdefault(key, []).append(e)
    for key, items in by_payer.items():
        verdict = payer_verdict(fhir=None, enrollments=items, support=support.get(key))
        rows.append({
            "payer": (items[0].get("payer") or {}).get("name") or key,
            "payer_key": key,
            "eligibility_payer": key,
            "stedi_support": support.get(key),
            **verdict,
        })
    for r in rows:
        r["status_label"] = STATUS_LABELS[r["status"]]

    overall = overall_verdict(provider=provider, leie=leie, rows=rows)
    warnings: list[str] = []
    if stedi.get("configured") and not enrollments and not stedi.get("error"):
        warnings.append("No Stedi transaction enrollments for this NPI. Most payers require ERA (835) enrollment, and some require claims enrollment, before EDI billing through Stedi.")
    if not stedi.get("configured"):
        warnings.append("Stedi isn't configured, so EDI enrollment and payer support weren't checked.")
    if leie.get("possible_name_matches"):
        warnings.append("Possible OIG LEIE name match with no NPI on the exclusion record. Verify at exclusions.oig.hhs.gov.")
    if is_org and not pecos.get("enrolled"):
        warnings.append("Group NPI isn't in Medicare PECOS. A group billing Medicare under its own NPI needs its own enrollment (855B).")
    if order_ref.get("found") is False and not is_org:
        warnings.append("Not on the Medicare Order and Referring list.")
    if leie.get("error"):
        warnings.append(leie["error"])

    return {
        "npi": npi,
        "checked_at": checked_at,
        "provider": provider,
        "overall": {**overall, "status_label": STATUS_LABELS[overall["status"]]},
        "payers": rows,
        "warnings": warnings,
        "caveat": CAVEAT,
        "checks": {
            "nppes": {"source": NPPES_URL, "found": provider.get("found"), "active": provider.get("active"), "error": provider.get("error")},
            "medicare_pecos": pecos,
            "opt_out": opt_out,
            "order_referring": order_ref,
            "reassignment": reassign,
            "group_reassignment": group_members,
            "revalidation": revalidation,
            "oig_leie": leie,
            "fhir_directories": status.get("network_status"),
            "stedi_enrollment": stedi,
            "stedi_payer_support": support,
        },
        "skipped_sources": [
            {"name": "SAM.gov exclusions", "reason": "Requires a SAM.gov API key (free but needs account registration); not configured."},
            {"name": "State Medicaid exclusion / enrollment lists", "reason": "Vary by state with no common free API."},
            {"name": "Aetna, Anthem/Elevance, Kaiser and other directories", "reason": "Their FHIR directories need OAuth/registration (see /payers)."},
        ],
    }
