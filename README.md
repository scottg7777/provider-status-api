# Provider Status API

A FastAPI service and CLI that answers: *what is this provider's status with a particular payer?* It combines four sources:

1. **CMS NPPES NPI Registry** — name, enumeration type (NPI-1 individual vs NPI-2 organization), active status, primary taxonomy, practice addresses.
2. **CMS Medicare PECOS public enrollment** — whether the NPI appears in the current Medicare Fee-For-Service Public Provider Enrollment dataset (actively approved to bill Medicare FFS, or completed an 855O, at the PECOS extract).
3. **Public HL7 Da Vinci PDEX Plan-Net FHIR directories** — in-network presence, networks/plans, active status, and practice locations for payers whose **live, unauthenticated** endpoints were verified.
4. **Stedi EDI Transaction Enrollment API** (optional) — per-transaction-type enrollment status (`LIVE`, `PROVISIONING`, `PROVIDER_ACTION_REQUIRED`, `STEDI_ACTION_REQUIRED`, `REJECTED`, etc.) and open manual tasks, when `STEDI_API_KEY` is set.

Stedi is not required. If the key is missing, the Stedi section returns `"configured": false` and all public CMS / FHIR results are still returned.

## Setup

```bash
cd /workspace/provider-status-api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

To enable Stedi EDI enrollment lookups, export a key from the Stedi dashboard (Developer → API keys). The key is read **only** from the environment and is never written to logs or response bodies:

```bash
export STEDI_API_KEY="your-stedi-api-key"
```

## Web UI

The same process also serves a browser interface at `GET /` (`/workspace/provider-status-api/static/`). Open http://127.0.0.1:8080/ after starting uvicorn. Existing JSON endpoints and `/docs` are unchanged.

## Run the API

```bash
source .venv/bin/activate
cd /workspace/provider-status-api
uvicorn app:app --host 127.0.0.1 --port 8080
```

### Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Liveness plus whether Stedi is configured |
| `GET` | `/payers` | Live FHIR payers, CMS sources, Stedi URLs, and the `unsupported_or_auth_required` list |
| `GET` | `/provider-status?npi=&payer=&tin=` | Combined lookup. `payer` and `tin` are optional. |
| `POST` | `/provider-status/batch` | Up to 50 `{npi, payer?, tin?}` items, run with bounded concurrency |

Example:

```bash
curl -sS "http://127.0.0.1:8080/health"
curl -sS "http://127.0.0.1:8080/payers"
curl -sS "http://127.0.0.1:8080/provider-status?npi=1003000126"
curl -sS "http://127.0.0.1:8080/provider-status?npi=1003000126&payer=cigna"
```

`payer` accepts names or aliases such as `cigna`, `humana`, `uhc`, `unitedhealthcare`, `centene`, `molina`, `bcbsri`, `medicare` / `pecos`, or a major payer that is documented as auth-required (for example `aetna`). For `medicare`, only NPPES + PECOS + Stedi run. For `aetna`, public CMS results still return and the FHIR section explains that OAuth / registration is required.

Batch:

```bash
curl -sS -X POST http://127.0.0.1:8080/provider-status/batch \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"npi":"1003000126","payer":"cigna"},{"npi":"1710975354","payer":"humana"}]}'
```

## Run the CLI

The CLI calls the same in-process clients (no server required):

```bash
source .venv/bin/activate
cd /workspace/provider-status-api

python cli.py 1003000126
python cli.py 1003000126 --payer cigna --json
python cli.py lookup 1710975354 --payer humana
python cli.py payers
python cli.py health
python cli.py batch examples.json
```

## Sources and live-test results

Live checks were run on 2026-11-19 (America/Los_Angeles) / 2026-11-20 UTC.

### CMS NPPES

- `GET https://npiregistry.cms.hhs.gov/api/?version=2.1&number=<npi>`
- Sample: NPI `1003000126` → Ardalan Enkeshafi, NPI-1, status A, primary taxonomy Hospitalist (`208M00000X`), practice location in Bethesda, MD.

### CMS Medicare PECOS

- Current series (no auth): `GET https://data.cms.gov/data-api/v1/dataset/2457ea29-fc82-48b0-86ec-3b0755de7515/data?filter[NPI]=<npi>`
- Dataset: **Medicare Fee-For-Service Public Provider Enrollment**. `dataset-resources` reported the Q4 2026 extract at test time.
- Sample: NPI `1003000126` returned four PECOS enrollments (Internal Medicine in MD, VA, and PA; Hospitalist in DC), PAC ID `7517003643`. Presence in this file is treated as Medicare-enrolled.

### Live public Plan-Net FHIR payers (enabled)

These endpoints returned HTTP 200 **without authentication**, and a real NPI lookup succeeded:

| Payer | Base URL | Search used | Live sample |
| --- | --- | --- | --- |
| Cigna | `https://fhir.cigna.com/ProviderDirectory/v1` | Name (family/given) then client-side NPI match. `Practitioner.identifier` is **not** a supported search parameter. | NPI `1003000126` Ardalan Enkeshafi and NPI `1710975354` Nelayda Fonte found. Networks come from Plan-Net `network-reference` extensions. |
| UnitedHealthcare | `https://flex.optum.com/fhirpublic/R4` | `Practitioner?identifier=` (system\|NPI or bare NPI) | NPI `1437170412` Peter Lanza found, with included network/plan organizations such as National Options PPO 10. |
| Humana | `https://fhir.humana.com/api` | `Practitioner?identifier=` | NPI `1679052898` Juan Sanchez and NPI `1710975354` Nelayda Fonte found. |
| Centene | `https://iopc-pd.api.centene.com/iopc/pd/fhir/providerdirectory` | Name then NPI match (`identifier` is not searchable). Roles require `practitioner=Practitioner/{id}`. | NPI `1003000126` and `1710975354` found. |
| Molina Healthcare | `https://api.interop.molinahealthcare.com/providerdirectory` | Bare `Practitioner?identifier={npi}` only (`system\|npi` returns no matches). `_include` on roles returns HTTP 500, so network names are read from extensions or a follow-up Organization read. | NPI `1427458553` Mallory E Mowdy found in network **MHMS CHIP**. |
| BCBS Rhode Island | `https://transmitapi.goperspecta.com/v2/bcbsri` | Name then NPI match. `Practitioner.identifier` is ignored (returns an unfiltered list). | `family=SEIDLER` matched NPI `1013231570` Michael L Seidler. |

### Payers not queried live (OAuth, registration, or broken public host)

Documented in `GET /payers` → `unsupported_or_auth_required`:

- **Aetna (CVS Health)** — Provider Directory APIs require OAuth 2.0 client credentials and app registration at the Aetna developer portal.
- **Anthem / Elevance Health / Wellpoint** — `totalview.healthos.elevancehealth.com` Provider Directory metadata returns HTTP 401 without registered credentials. `fhir.anthem.com` failed TLS.
- **Kaiser Permanente** — Epic FHIR metadata is public, but `Practitioner` searches return HTTP 401. This is not a public Plan-Net directory.
- **Florida Blue, CareFirst, Highmark, BCBS South Carolina** — published or guessed hosts failed TLS or had no verified no-auth URL.
- **Independence Blue Cross** — `https://eapics.ibx.com/provider/v1/fhir/metadata` returned HTTP 400.
- **UHC `fhirpublic2025`** — `/fhirpublic2025/R4/metadata` returned HTTP 502; the working UHC directory is `/fhirpublic/R4`.

Stedi cannot automate CAQH credentialing, license primary-source verification, or payer contracting. It *can* report EDI transaction enrollment (837/835/270/276) once `STEDI_API_KEY` is set.

### Stedi endpoints used

- Enrollments: `GET https://enrollments.us.stedi.com/2024-09-01/enrollments?providerNpis=&providerTaxIds=&payerIds=`
- Payer search: `GET https://payers.us.stedi.com/2024-04-01/payers/search?query=`
- Auth: `Authorization: <STEDI_API_KEY>` (Stedi still accepts the legacy `Authorization: Key <api_key>` prefix).

## Tests

```bash
source .venv/bin/activate
cd /workspace/provider-status-api
pytest -q
```

This runs mocked unit tests for Stedi plus live smoke tests for NPPES, PECOS, and every enabled FHIR payer. Live tests need outbound HTTPS.

## Project layout

```
app.py              FastAPI routes
cli.py              Command-line entry point
service.py          Combined lookup orchestration
models.py           Request models
test_api.py         Unit + live smoke tests
clients/nppes.py    CMS NPPES
clients/pecos.py    CMS PECOS public enrollment
clients/fhir.py     Plan-Net FHIR
clients/stedi.py    Stedi enrollments + payer search
clients/registry.py Live and unsupported payer lists
```
