"""HHS-OIG LEIE (List of Excluded Individuals/Entities) from the official monthly CSV.

The CSV (~11 MB) is downloaded on first use and cached in memory, then refreshed
after LEIE_TTL_SECONDS. DOB values in the file are never stored or returned.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from typing import Any

import httpx

from .registry import OIG_LEIE_CSV_URL

LEIE_TTL_SECONDS = 24 * 3600
SEARCH_URL = "https://exclusions.oig.hhs.gov/"


def _norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9 ]", "", str(value or "").upper()).strip()


def _norm_org(value: Any) -> str:
    v = _norm(value)
    v = re.sub(r"\b(LLC|PLLC|PC|INC|CORP|CORPORATION|CO|LTD|LLP|PA|PSC)\b", "", v)
    return re.sub(r"\s+", " ", v).strip()


class LeieIndex:
    def __init__(self) -> None:
        self.by_npi: dict[str, list[dict[str, Any]]] = {}
        self.by_person: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.by_org: dict[str, list[dict[str, Any]]] = {}
        self.loaded_at: float | None = None
        self.last_modified: str | None = None
        self.row_count = 0
        self.error: str | None = None
        self._lock = asyncio.Lock()

    def load_csv_text(self, text: str, last_modified: str | None = None) -> None:
        by_npi: dict[str, list[dict[str, Any]]] = {}
        by_person: dict[tuple[str, str], list[dict[str, Any]]] = {}
        by_org: dict[str, list[dict[str, Any]]] = {}
        count = 0
        for row in csv.DictReader(io.StringIO(text)):
            count += 1
            rec = {
                "last_name": row.get("LASTNAME") or None,
                "first_name": row.get("FIRSTNAME") or None,
                "business_name": row.get("BUSNAME") or None,
                "general": row.get("GENERAL") or None,
                "specialty": row.get("SPECIALTY") or None,
                "npi": (row.get("NPI") or "").strip() if (row.get("NPI") or "0").strip() not in {"", "0"} else None,
                "city": row.get("CITY") or None,
                "state": row.get("STATE") or None,
                "exclusion_type": row.get("EXCLTYPE") or None,
                "exclusion_date": row.get("EXCLDATE") or None,
                "reinstatement_date": row.get("REINDATE") if (row.get("REINDATE") or "0") != "0" else None,
                "waiver_date": row.get("WAIVERDATE") if (row.get("WAIVERDATE") or "0") != "0" else None,
            }
            if rec["npi"]:
                by_npi.setdefault(rec["npi"], []).append(rec)
            if rec["last_name"] and rec["first_name"]:
                by_person.setdefault((_norm(rec["last_name"]), _norm(rec["first_name"])), []).append(rec)
            if rec["business_name"]:
                by_org.setdefault(_norm_org(rec["business_name"]), []).append(rec)
        self.by_npi, self.by_person, self.by_org = by_npi, by_person, by_org
        self.row_count = count
        self.loaded_at = time.time()
        self.last_modified = last_modified
        self.error = None

    async def ensure_loaded(self, client: httpx.AsyncClient) -> None:
        fresh = self.loaded_at and (time.time() - self.loaded_at) < LEIE_TTL_SECONDS
        if fresh:
            return
        async with self._lock:
            if self.loaded_at and (time.time() - self.loaded_at) < LEIE_TTL_SECONDS:
                return
            try:
                response = await client.get(OIG_LEIE_CSV_URL, timeout=httpx.Timeout(90.0, connect=10.0))
                response.raise_for_status()
                text = response.content.decode("utf-8-sig", errors="replace")
                await asyncio.to_thread(self.load_csv_text, text, response.headers.get("last-modified"))
            except httpx.HTTPError as exc:
                self.error = f"OIG LEIE download failed: {exc.__class__.__name__}"

    def lookup(
        self,
        npi: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        organization_name: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        npi_hits = self.by_npi.get(npi, [])
        name_hits: list[dict[str, Any]] = []
        if last_name and first_name:
            name_hits = self.by_person.get((_norm(last_name), _norm(first_name)), [])
        elif organization_name:
            name_hits = self.by_org.get(_norm_org(organization_name), [])
        name_hits = [h for h in name_hits if h not in npi_hits]
        if state:
            same_state = [h for h in name_hits if (h.get("state") or "").upper() == state.upper()]
        else:
            same_state = name_hits
        active_npi = [h for h in npi_hits if not h.get("reinstatement_date")]
        return {
            "dataset": "HHS-OIG LEIE (List of Excluded Individuals/Entities)",
            "source": OIG_LEIE_CSV_URL,
            "verify_url": SEARCH_URL,
            "loaded": self.loaded_at is not None,
            "file_last_modified": self.last_modified,
            "row_count": self.row_count,
            "excluded": bool(active_npi) if self.loaded_at else None,
            "npi_matches": npi_hits,
            "possible_name_matches": same_state[:10],
            "error": self.error if not self.loaded_at else None,
        }


LEIE = LeieIndex()
