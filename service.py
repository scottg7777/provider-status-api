"""Orchestrates NPPES, PECOS, Plan-Net FHIR, and optional Stedi lookups."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from clients import stedi as stedi_client
from clients.fhir import lookup_payer_network
from clients.nppes import lookup_nppes
from clients.pecos import lookup_pecos
from clients.registry import (
    LIVE_FHIR_PAYERS,
    NPPES_URL,
    PECOS_DATA_URL,
    PECOS_DATASET_ID,
    PECOS_DATASET_NAME,
    STEDI_ENROLLMENTS_BASE,
    STEDI_PAYERS_BASE,
    UNSUPPORTED_OR_AUTH_REQUIRED,
    live_payer_by_id,
    normalize_payer,
    unsupported_by_id,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def payers_catalog() -> dict[str, Any]:
    return {
        "stedi": {
            "configured": stedi_client.is_configured(),
            "enrollments_url": f"{STEDI_ENROLLMENTS_BASE}/enrollments",
            "payers_search_url": f"{STEDI_PAYERS_BASE}/payers/search",
            "auth": "Authorization header set from STEDI_API_KEY when present",
        },
        "cms": {
            "nppes": {"url": NPPES_URL, "version": "2.1", "auth_required": False},
            "medicare_pecos": {
                "dataset": PECOS_DATASET_NAME,
                "dataset_id": PECOS_DATASET_ID,
                "url": PECOS_DATA_URL,
                "auth_required": False,
                "meaning": (
                    "A matching row means the NPI was actively approved to bill "
                    "Medicare FFS or had completed an 855O at the PECOS extract."
                ),
            },
        },
        "fhir_payers": [
            {
                "id": p.id,
                "name": p.name,
                "base_url": p.base_url,
                "search_strategy": p.search_strategy,
                "aliases": list(p.aliases),
                "live_tested": True,
                "notes": p.notes,
                "live_sample": p.live_sample,
            }
            for p in LIVE_FHIR_PAYERS
        ],
        "unsupported_or_auth_required": [
            {
                "id": p.id,
                "name": p.name,
                "aliases": list(p.aliases),
                "reason": p.reason,
                "references": list(p.references),
            }
            for p in UNSUPPORTED_OR_AUTH_REQUIRED
        ],
    }


def _stedi_summary(stedi: dict[str, Any]) -> str:
    if not stedi.get("configured"):
        return "not_configured"
    if stedi.get("error"):
        return "error"
    enrollments = stedi.get("enrollments") or []
    if not enrollments:
        return "no_enrollments"
    statuses = stedi.get("statuses") or sorted({e.get("status") for e in enrollments if e.get("status")})
    open_count = len(stedi.get("open_tasks") or [])
    suffix = f", {open_count} open task(s)" if open_count else ""
    return f"{len(enrollments)} enrollment(s): {', '.join(statuses)}{suffix}"


def _build_summary(
    *,
    npi: str,
    payer_query: str | None,
    provider: dict[str, Any],
    pecos: dict[str, Any],
    network_results: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    stedi: dict[str, Any],
) -> dict[str, Any]:
    in_network = [r["payer_name"] for r in network_results if r.get("in_network")]
    not_found = [r["payer_name"] for r in network_results if r.get("queried") and not r.get("in_network") and not r.get("error")]
    errors = [r["payer_name"] for r in network_results if r.get("error") and not r.get("found")]
    return {
        "npi": npi,
        "provider_name": provider.get("name"),
        "nppes_found": bool(provider.get("found")),
        "nppes_active": provider.get("active"),
        "medicare_enrolled": pecos.get("enrolled"),
        "medicare_provider_types": pecos.get("provider_types") or [],
        "in_network_payers": in_network,
        "not_found_payers": not_found,
        "fhir_errors": errors,
        "unsupported_or_skipped": [s.get("name") or s.get("payer") for s in skipped],
        "stedi": _stedi_summary(stedi),
        "payer_filter": payer_query,
    }


async def lookup_provider_status(
    client: httpx.AsyncClient,
    npi: str,
    payer: str | None = None,
    tin: str | None = None,
) -> dict[str, Any]:
    payer_key = normalize_payer(payer)
    queried_at = _now()

    nppes_task = asyncio.create_task(lookup_nppes(client, npi))
    pecos_task = asyncio.create_task(lookup_pecos(client, npi))
    stedi_task = asyncio.create_task(
        stedi_client.lookup_enrollments(client, npi, payer=payer, tin=tin)
    )

    # Start identifier-based FHIR lookups immediately; name-based payers wait for NPPES.
    fhir_targets: list[Any] = []
    skipped: list[dict[str, Any]] = []
    if payer_key == "medicare":
        skipped.append(
            {
                "payer": "commercial_fhir",
                "name": "Commercial Plan-Net FHIR",
                "reason": "Payer filter is Medicare/PECOS; commercial FHIR directories were not queried.",
            }
        )
    elif payer_key and payer_key.startswith("unsupported:"):
        unsupported_id = payer_key.split(":", 1)[1]
        info = unsupported_by_id(unsupported_id)
        skipped.append(
            {
                "payer": unsupported_id,
                "name": info.name if info else unsupported_id,
                "reason": info.reason if info else "This payer requires registration or OAuth and is not queried live.",
            }
        )
    elif payer_key and live_payer_by_id(payer_key):
        fhir_targets = [live_payer_by_id(payer_key)]
    elif payer_key:
        skipped.append(
            {
                "payer": payer_key,
                "name": payer,
                "reason": (
                    f"No live public Plan-Net endpoint is registered for '{payer}'. "
                    "See /payers for supported and unsupported directories."
                ),
            }
        )
    else:
        fhir_targets = list(LIVE_FHIR_PAYERS)

    identifier_payers = [p for p in fhir_targets if p and p.search_strategy.startswith("identifier")]
    name_payers = [p for p in fhir_targets if p and p.search_strategy == "name_then_npi"]

    identifier_tasks = {
        p.id: asyncio.create_task(lookup_payer_network(client, p, npi, None))
        for p in identifier_payers
    }

    provider = await nppes_task
    name_tasks = {
        p.id: asyncio.create_task(lookup_payer_network(client, p, npi, provider))
        for p in name_payers
    }

    network_results: list[dict[str, Any]] = []
    # Preserve registry order.
    task_map = {**identifier_tasks, **name_tasks}
    for p in fhir_targets:
        if p and p.id in task_map:
            network_results.append(await task_map[p.id])

    pecos = await pecos_task
    stedi = await stedi_task

    provider_out = {
        **provider,
        "queried_at": queried_at,
    }
    pecos_out = {**pecos, "queried_at": queried_at}
    stedi_out = {**stedi, "queried_at": queried_at}

    return {
        "npi": npi,
        "payer_query": payer,
        "tin_provided": bool(tin),
        "queried_at": queried_at,
        "provider": provider_out,
        "medicare_pecos": pecos_out,
        "network_status": {
            "results": network_results,
            "skipped": skipped,
        },
        "stedi_edi_enrollment": stedi_out,
        "summary": _build_summary(
            npi=npi,
            payer_query=payer,
            provider=provider,
            pecos=pecos,
            network_results=network_results,
            skipped=skipped,
            stedi=stedi,
        ),
    }


async def lookup_batch(
    client: httpx.AsyncClient,
    items: list[dict[str, Any]],
    *,
    concurrency: int = 8,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(item: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await lookup_provider_status(
                client,
                item["npi"],
                payer=item.get("payer"),
                tin=item.get("tin"),
            )

    return list(await asyncio.gather(*(_one(item) for item in items)))
