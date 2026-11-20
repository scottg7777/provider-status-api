#!/usr/bin/env python3
"""Command-line lookups for provider / payer status."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from clients.http_util import new_async_client
from clients.npi_util import validate_npi
from service import lookup_batch, lookup_provider_status, payers_catalog


def _print(data: Any, as_json: bool) -> None:
    if as_json or not isinstance(data, dict) or "summary" not in data:
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    summary = data.get("summary") or {}
    provider = data.get("provider") or {}
    pecos = data.get("medicare_pecos") or {}
    stedi = data.get("stedi_edi_enrollment") or {}
    print(f"NPI: {data.get('npi')}")
    print(f"Name: {provider.get('name') or 'not found in NPPES'}")
    print(
        "NPPES: "
        + (
            f"{'active' if provider.get('active') else 'inactive'} "
            f"{provider.get('enumeration_type') or ''}".strip()
            if provider.get("found")
            else "not found"
        )
    )
    if provider.get("primary_taxonomy"):
        tax = provider["primary_taxonomy"]
        print(f"Taxonomy: {tax.get('description') or tax.get('code')}")
    enrolled = pecos.get("enrolled")
    if enrolled is True:
        types = ", ".join(pecos.get("provider_types") or []) or "Medicare FFS"
        states = ", ".join(pecos.get("states") or [])
        print(f"Medicare PECOS: enrolled ({types}" + (f"; {states}" if states else "") + ")")
    elif enrolled is False:
        print("Medicare PECOS: not found in the public FFS enrollment file")
    else:
        print(f"Medicare PECOS: error ({pecos.get('error')})")
    print("Network status:")
    for row in (data.get("network_status") or {}).get("results") or []:
        if row.get("in_network"):
            networks = ", ".join(n.get("name") or n.get("id") for n in (row.get("networks") or [])[:8]) or "in directory"
            print(f"  - {row.get('payer_name')}: in-network ({networks})")
        elif row.get("error") and not row.get("found"):
            print(f"  - {row.get('payer_name')}: error ({row.get('error')})")
        else:
            print(f"  - {row.get('payer_name')}: not found")
    for skipped in (data.get("network_status") or {}).get("skipped") or []:
        print(f"  - {skipped.get('name') or skipped.get('payer')}: skipped ({skipped.get('reason')})")
    if not stedi.get("configured"):
        print("Stedi: not configured (set STEDI_API_KEY to enable EDI enrollment lookups)")
    elif stedi.get("error"):
        print(f"Stedi: {stedi.get('error')}")
    elif not stedi.get("enrollments"):
        print("Stedi: no enrollments found")
    else:
        print(f"Stedi: {summary.get('stedi')}")
        for enrollment in stedi.get("enrollments") or []:
            tx = ", ".join(t.get("label") or t.get("code") for t in (enrollment.get("transactions") or []))
            payer_name = (enrollment.get("payer") or {}).get("name") or (enrollment.get("payer") or {}).get("stedi_payer_id")
            print(f"  - {payer_name}: {enrollment.get('status')}" + (f" ({tx})" if tx else ""))
            for task in enrollment.get("open_tasks") or []:
                print(f"      open task ({task.get('responsible_party')}): {task.get('instructions') or task.get('id')}")


async def _lookup(npi: str, payer: str | None, tin: str | None) -> dict[str, Any]:
    async with new_async_client() as client:
        return await lookup_provider_status(client, npi, payer=payer, tin=tin)


async def _batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async with new_async_client() as client:
        return await lookup_batch(client, items)


def _normalize_argv(argv: list[str]) -> list[str]:
    """Allow `cli.py <npi>` in addition to explicit subcommands."""
    commands = {"lookup", "batch", "payers", "health"}
    tokens = [a for a in argv if not a.startswith("-")]
    if not tokens or tokens[0] in commands:
        return argv
    out: list[str] = []
    inserted = False
    for arg in argv:
        if not inserted and not arg.startswith("-"):
            out.append("lookup")
            inserted = True
        out.append(arg)
    return out


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    argv = _normalize_argv(raw)
    parser = argparse.ArgumentParser(
        description="Look up a provider's NPPES, Medicare PECOS, Plan-Net, and Stedi EDI status."
    )
    parser.add_argument("--json", action="store_true", help="Always print raw JSON")
    sub = parser.add_subparsers(dest="command")

    lookup_parser = sub.add_parser("lookup", help="Look up one NPI (default command)")
    lookup_parser.add_argument("npi")
    lookup_parser.add_argument("--payer", help="Optional payer name or ID")
    lookup_parser.add_argument("--tin", help="Optional tax ID for Stedi filters")
    lookup_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Always print raw JSON")

    batch_parser = sub.add_parser("batch", help="Look up a JSON array of {npi, payer?, tin?} objects")
    batch_parser.add_argument("file", help="Path to JSON file, or - for stdin")

    sub.add_parser("payers", help="List supported live FHIR payers and CMS/Stedi sources")
    sub.add_parser("health", help="Show local health information")

    args = parser.parse_args(argv)
    command = args.command

    if command in {None, "lookup"}:
        npi = getattr(args, "npi", None)
        if not npi:
            parser.print_help()
            return 2
        try:
            npi = validate_npi(npi)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        result = asyncio.run(_lookup(npi, getattr(args, "payer", None), getattr(args, "tin", None)))
        _print(result, args.json)
        return 0

    if command == "batch":
        if args.file == "-":
            raw = sys.stdin.read()
        else:
            with open(args.file, encoding="utf-8") as handle:
                raw = handle.read()
        payload = json.loads(raw)
        if isinstance(payload, dict) and "items" in payload:
            payload = payload["items"]
        if not isinstance(payload, list) or not payload:
            print("error: batch file must be a JSON array of {npi, payer?, tin?} objects", file=sys.stderr)
            return 2
        if len(payload) > 50:
            print("error: batch is limited to 50 items", file=sys.stderr)
            return 2
        items = []
        for row in payload:
            try:
                items.append(
                    {
                        "npi": validate_npi(str(row.get("npi"))),
                        "payer": row.get("payer"),
                        "tin": row.get("tin"),
                    }
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        results = asyncio.run(_batch(items))
        json.dump({"count": len(results), "results": results}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if command == "payers":
        json.dump(payers_catalog(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if command == "health":
        from clients.stedi import is_configured

        json.dump(
            {"status": "ok", "service": "provider-status-api", "stedi_configured": is_configured()},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(None))
