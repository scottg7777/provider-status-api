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


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class EligibilityReadinessRequest(BaseModel):
    """One real-time 270 under the provider's NPI. Member fields are never stored or logged."""

    npi: str = Field(..., description="10-digit provider NPI the inquiry is sent under")
    payer: str = Field(..., min_length=1, max_length=120, description="Stedi payer ID, primary payer ID, alias, or name")
    provider_organization_name: str | None = Field(None, max_length=120)
    provider_first_name: str | None = Field(None, max_length=60)
    provider_last_name: str | None = Field(None, max_length=60)
    member_id: str = Field(..., min_length=1, max_length=80)
    first_name: str = Field(..., min_length=1, max_length=60)
    last_name: str = Field(..., min_length=1, max_length=60)
    date_of_birth: str = Field(..., description="YYYY-MM-DD")
    date_of_service: str | None = Field(None, description="YYYY-MM-DD; omit for today")
    service_type_code: str = Field("30", max_length=3, description="X12 service type code (default 30)")
    dependent_first_name: str | None = Field(None, max_length=60)
    dependent_last_name: str | None = Field(None, max_length=60)
    dependent_date_of_birth: str | None = None

    model_config = {"json_schema_extra": {"examples": [{
        "npi": "1999999984", "payer": "87726", "provider_organization_name": "Provider Name",
        "member_id": "UHC123456", "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1971-01-01",
    }]}}

    @field_validator("npi")
    @classmethod
    def _npi(cls, value: str) -> str:
        return validate_npi(value)

    @field_validator(
        "payer", "provider_organization_name", "provider_first_name", "provider_last_name",
        "member_id", "first_name", "last_name", "dependent_first_name", "dependent_last_name",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _clean(value) if isinstance(value, str) else value

    @field_validator("date_of_birth", "date_of_service", "dependent_date_of_birth")
    @classmethod
    def _dates(cls, value: str | None, info) -> str | None:
        from eligibility import normalize_date

        return normalize_date(value, field=info.field_name)

    @field_validator("service_type_code")
    @classmethod
    def _stc(cls, value: str) -> str:
        cleaned = (value or "30").strip().upper() or "30"
        if not cleaned.isalnum():
            raise ValueError("service_type_code must be an X12 service type code such as 30")
        return cleaned
