"""Pydantic request and response models for the provider status API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from clients.npi_util import validate_npi


class ProviderStatusQuery(BaseModel):
    npi: str
    payer: str | None = None
    tin: str | None = None

    @field_validator("npi")
    @classmethod
    def _npi(cls, value: str) -> str:
        return validate_npi(value)

    @field_validator("payer")
    @classmethod
    def _payer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("tin")
    @classmethod
    def _tin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class BatchRequest(BaseModel):
    items: list[ProviderStatusQuery] = Field(..., min_length=1, max_length=50)


class HealthResponse(BaseModel):
    status: str
    service: str
    stedi_configured: bool


class PayersResponse(BaseModel):
    stedi: dict[str, Any]
    cms: dict[str, Any]
    fhir_payers: list[dict[str, Any]]
    unsupported_or_auth_required: list[dict[str, Any]]
