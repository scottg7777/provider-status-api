"""NPI format and CMS Luhn checksum validation."""

from __future__ import annotations

import re

NPI_RE = re.compile(r"^\d{10}$")


def npi_checksum_ok(npi: str) -> bool:
    """Return True if the 10-digit NPI passes the CMS Luhn check (prefix 80840)."""
    if not NPI_RE.fullmatch(npi or ""):
        return False
    digits = "80840" + npi
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def validate_npi(npi: str) -> str:
    """Normalize and validate a 10-digit NPI, including the CMS checksum."""
    cleaned = (npi or "").strip()
    if not NPI_RE.fullmatch(cleaned):
        raise ValueError("NPI must be a 10-digit number")
    if not npi_checksum_ok(cleaned):
        raise ValueError("NPI failed the CMS Luhn checksum")
    return cleaned
