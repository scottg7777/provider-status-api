"""Live-tested public FHIR payer registry and payers that require registration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FhirPayerConfig:
    """A public Plan-Net (or equivalent) FHIR directory verified live without auth."""

    id: str
    name: str
    base_url: str
    search_strategy: str
    aliases: tuple[str, ...] = ()
    use_identifier_system: bool = True
    includes_supported: bool = True
    # Some servers (Centene) only match PractitionerRole.practitioner as "Practitioner/{id}".
    practitioner_role_ref_prefix: bool = False
    notes: str = ""
    live_sample: str = ""


@dataclass(frozen=True)
class UnsupportedPayer:
    id: str
    name: str
    reason: str
    aliases: tuple[str, ...] = ()
    references: tuple[str, ...] = ()


# Verified 2026-11-19/20 with live unauthenticated GETs. Do not add a payer
# here unless Practitioner (or name) search returns a clean 200 without auth.
LIVE_FHIR_PAYERS: tuple[FhirPayerConfig, ...] = (
    FhirPayerConfig(
        id="cigna",
        name="Cigna",
        base_url="https://fhir.cigna.com/ProviderDirectory/v1",
        search_strategy="name_then_npi",
        aliases=("cigna", "evernorth"),
        includes_supported=True,
        notes=(
            "Practitioner.identifier is not a supported search parameter. "
            "Lookup uses NPPES family/given (or organization name), then "
            "client-side NPI matching. Networks are Plan-Net network-reference extensions."
        ),
        live_sample="NPI 1003000126 Ardalan Enkeshafi and NPI 1710975354 Nelayda Fonte found.",
    ),
    FhirPayerConfig(
        id="unitedhealthcare",
        name="UnitedHealthcare",
        base_url="https://flex.optum.com/fhirpublic/R4",
        search_strategy="identifier",
        aliases=("unitedhealthcare", "united", "uhc", "optum"),
        use_identifier_system=True,
        includes_supported=True,
        notes="Public unauthenticated directory at flex.optum.com/fhirpublic/R4.",
        live_sample="NPI 1437170412 Peter Lanza found; NPI 1003000126 not in this directory.",
    ),
    FhirPayerConfig(
        id="humana",
        name="Humana",
        base_url="https://fhir.humana.com/api",
        search_strategy="identifier",
        aliases=("humana",),
        use_identifier_system=True,
        includes_supported=True,
        notes="Public Provider Directory FHIR API. Identifier search accepts system|npi or bare NPI.",
        live_sample="NPI 1679052898 Juan Sanchez and NPI 1710975354 Nelayda Fonte found.",
    ),
    FhirPayerConfig(
        id="centene",
        name="Centene",
        base_url="https://iopc-pd.api.centene.com/iopc/pd/fhir/providerdirectory",
        search_strategy="name_then_npi",
        aliases=("centene", "ambetter", "fidelis", "wellcare"),
        includes_supported=True,
        practitioner_role_ref_prefix=True,
        notes=(
            "Practitioner.identifier is not searchable; open search requires a name. "
            "PractitionerRole.practitioner must be Practitioner/{id}."
        ),
        live_sample="NPI 1003000126 Ardalan Enkeshafi and NPI 1710975354 Nelayda Fonte found.",
    ),
    FhirPayerConfig(
        id="molina",
        name="Molina Healthcare",
        base_url="https://api.interop.molinahealthcare.com/providerdirectory",
        search_strategy="identifier_bare",
        aliases=("molina",),
        use_identifier_system=False,
        includes_supported=False,
        notes=(
            "Bare NPI identifier search works; system|npi returns no matches. "
            "_include on PractitionerRole returns HTTP 500, so network names are "
            "taken from network-reference displays or a follow-up Organization read."
        ),
        live_sample="NPI 1427458553 Mallory E Mowdy found, including network MHMS CHIP.",
    ),
    FhirPayerConfig(
        id="bcbsri",
        name="Blue Cross & Blue Shield of Rhode Island",
        base_url="https://transmitapi.goperspecta.com/v2/bcbsri",
        search_strategy="name_then_npi",
        aliases=("bcbsri", "blue cross rhode island", "blue shield rhode island"),
        includes_supported=True,
        notes=(
            "Public Plan-Net directory. Practitioner.identifier is ignored "
            "(returns an unfiltered list). Lookup uses family/given or name, "
            "then client-side NPI matching."
        ),
        live_sample="family=SEIDLER matched NPI 1013231570 Michael L Seidler; Enkeshafi not in directory.",
    ),
)


UNSUPPORTED_OR_AUTH_REQUIRED: tuple[UnsupportedPayer, ...] = (
    UnsupportedPayer(
        id="aetna",
        name="Aetna (CVS Health)",
        aliases=("aetna", "cvs", "cvs health"),
        reason=(
            "Provider Directory APIs require OAuth 2.0 client credentials and "
            "app registration on the Aetna developer portal."
        ),
        references=(
            "https://developerportal.aetna.com/gettingstarted",
        ),
    ),
    UnsupportedPayer(
        id="anthem",
        name="Anthem / Elevance Health / Wellpoint",
        aliases=("anthem", "elevance", "wellpoint", "anthem blue cross", "anthem blue cross blue shield"),
        reason=(
            "Elevance Provider Directory production endpoints return HTTP 401 "
            "without registered OAuth client credentials. The older "
            "fhir.anthem.com host failed TLS during live tests."
        ),
        references=(
            "https://totalview.healthos.elevancehealth.com/resources/registered/AnthemBlueCross/api/v1/fhir/metadata",
        ),
    ),
    UnsupportedPayer(
        id="kaiser",
        name="Kaiser Permanente",
        aliases=("kaiser", "kaiser permanente", "kp"),
        reason=(
            "The public Epic FHIR metadata endpoint responds without auth, but "
            "Practitioner searches return HTTP 401. This is not a public Plan-Net directory."
        ),
        references=(
            "https://fhir.kp.org/service/ptnt_care/EpicEdiFhirRoutingSvc/v2014/esb-envlbl/212/api/FHIR/R4/metadata",
        ),
    ),
    UnsupportedPayer(
        id="florida_blue",
        name="Florida Blue (BCBS Florida)",
        aliases=("florida blue", "bcbsfl", "bcbs florida", "blue cross florida"),
        reason=(
            "Documented FHIR host fhir.bcbsfl.com failed TLS handshake in live tests. "
            "The developer portal lists a Provider Directory API but no working no-auth URL was verified."
        ),
        references=("https://developer.bcbsfl.com/interop/interop-developer-portal/product/provider-directory",),
    ),
    UnsupportedPayer(
        id="carefirst",
        name="CareFirst",
        aliases=("carefirst",),
        reason="No working public no-auth Plan-Net base URL was verified; fhir.carefirst.com failed TLS.",
        references=("https://developer.carefirst.com/product/fhir-provider-directory",),
    ),
    UnsupportedPayer(
        id="highmark",
        name="Highmark",
        aliases=("highmark",),
        reason="No working public no-auth Plan-Net base URL was verified; fhir.highmark.com failed TLS.",
    ),
    UnsupportedPayer(
        id="bcbssc",
        name="BlueCross BlueShield of South Carolina",
        aliases=("bcbssc", "bcbs south carolina", "blue cross south carolina"),
        reason="https://fhir.bcbssc.com/r4/providerlisting/metadata failed TLS handshake in live tests.",
    ),
    UnsupportedPayer(
        id="ibx",
        name="Independence Blue Cross",
        aliases=("ibx", "independence blue cross", "independence blue"),
        reason="https://eapics.ibx.com/provider/v1/fhir/metadata returned HTTP 400 and was not usable without further registration.",
    ),
    UnsupportedPayer(
        id="uhc_fhirpublic2025",
        name="UnitedHealthcare (fhirpublic2025)",
        aliases=(),
        reason="https://flex.optum.com/fhirpublic2025/R4/metadata returned HTTP 502. The live UHC directory used instead is /fhirpublic/R4.",
    ),
)


# CMS / Stedi constants used in /payers and clients.
NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
NPPES_VERSION = "2.1"

# CMS Data API series ID for the current Medicare FFS Public Provider Enrollment
# (PECOS) dataset. Live-tested 2026-11-19; dataset-resources reported Q4 2026.
PECOS_DATASET_ID = "2457ea29-fc82-48b0-86ec-3b0755de7515"
PECOS_DATASET_NAME = "Medicare Fee-For-Service Public Provider Enrollment"
PECOS_DATA_URL = f"https://data.cms.gov/data-api/v1/dataset/{PECOS_DATASET_ID}/data"

STEDI_ENROLLMENTS_BASE = "https://enrollments.us.stedi.com/2024-09-01"
STEDI_PAYERS_BASE = "https://payers.us.stedi.com/2024-04-01"


def _norm(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def all_alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for payer in LIVE_FHIR_PAYERS:
        for alias in (payer.id, payer.name) + payer.aliases:
            mapping[_norm(alias)] = payer.id
    for payer in UNSUPPORTED_OR_AUTH_REQUIRED:
        for alias in (payer.id, payer.name) + payer.aliases:
            mapping.setdefault(_norm(alias), f"unsupported:{payer.id}")
    for alias in ("medicare", "pecos", "cms", "medicare ffs", "medicare fee for service"):
        mapping[_norm(alias)] = "medicare"
    return mapping


_ALIAS_MAP = all_alias_map()


def normalize_payer(payer: str | None) -> str | None:
    if payer is None:
        return None
    text = _norm(payer)
    if not text:
        return None
    if text in _ALIAS_MAP:
        return _ALIAS_MAP[text]
    # Partial contains match for phrases like "aetna better health"
    for alias, mapped in _ALIAS_MAP.items():
        if alias and alias in text:
            return mapped
    return text


def live_payer_by_id(payer_id: str) -> FhirPayerConfig | None:
    for payer in LIVE_FHIR_PAYERS:
        if payer.id == payer_id:
            return payer
    return None


def unsupported_by_id(payer_id: str) -> UnsupportedPayer | None:
    for payer in UNSUPPORTED_OR_AUTH_REQUIRED:
        if payer.id == payer_id:
            return payer
    return None
