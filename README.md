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
| `GET` | `/search/name?q=&state=&city=&kind=auto\|organization\|individual&limit=25` | Friendly name → ranked probable NPI matches from the NPPES NPI Registry |
| `GET` | `/search/affiliated?npi=` | Individuals likely affiliated with an organization NPI, tiered Strong / Possible (same suite or phone, authorized official, same building + specialty, second-site phone matches) |
| `GET` | `/readiness-report?npi=` | Readiness-to-bill report: overall verdict + per-payer table from every public source and Stedi |
| `POST` | `/eligibility-readiness` | Eligibility Readiness Check: one Stedi real-time 270/271 under the NPI, classified as RECOGNIZED / PROVIDER_NOT_RECOGNIZED / PATIENT_ISSUE / PAYER_UNAVAILABLE / INCONCLUSIVE |
| `GET` | `/payers/search?q=` | Stedi Payer Network search (payer typeahead) with eligibility / claims / ERA support |
| `GET` | `/eligibility-readiness/prefill?npi=` | Provider name from NPPES (org vs. first/last) to prefill the check |
| `GET` | `/eligibility-readiness/mock-requests` | Stedi-documented PHI-free mock requests (Stedi **test** API key only) |

Example:

```bash
curl -sS "http://127.0.0.1:8080/health"
curl -sS "http://127.0.0.1:8080/payers"
curl -sS "http://127.0.0.1:8080/provider-status?npi=1003000126"
curl -sS "http://127.0.0.1:8080/provider-status?npi=1003000126&payer=cigna"
```

`payer` accepts names or aliases such as `cigna`, `humana`, `uhc`, `unitedhealthcare`, `centene`, `molina`, `bcbsri`, `medicare` / `pecos`, or a major payer that is documented as auth-required (for example `aetna`). For `medicare`, only NPPES + PECOS + Stedi run. For `aetna`, public CMS results still return and the FHIR section explains that OAuth / registration is required.

## Find Provider → Readiness Report (default tab)

1. **Find provider.** Type a practice or person ("Aeterna Aesthetics", "Dr. Christine Snoke", "Snoke, Christine"), optionally a state and city, and pick Auto / Organization / Individual. `GET /search/name` normalizes the text (punctuation, titles and credentials like Dr/MD/NP/FNP-C, entity suffixes like LLC/PLLC/PC/Inc), builds NPPES query variants (exact org name, `name*` wildcard with the NPPES 2-character minimum, first word*, first+last, both name orders, `first*`/`last*`), runs them concurrently, merges by NPI, and ranks by name similarity (organization name plus NPPES other/DBA names), state/city match, active status, and type. Each result has NPI, type, name, credential, primary taxonomy, practice address and phone, enumeration/last-updated dates, authorized official (orgs), and a match score with reasons.
2. **Select the NPI.** For organizations, **Show affiliated providers** searches every org address (practice, mailing and NPPES secondary locations) by 9-digit ZIP, same-specialty individuals in the org's cities, and the authorized official. **Strong** = same suite, same phone, or authorized official. **Possible** = same building and same specialty, or an address shared with a phone-matched provider. A second hop searches other sites where a strong match uses the org's phone (e.g. a second clinic not on the org NPI). Unrelated tenants of a shared building are counted, not listed. Selecting an individual from this list passes `group_npi`, so the report checks the individual reassigns Medicare benefits to that group.
3. **Readiness report.** `GET /readiness-report` runs in parallel: NPPES, Medicare PECOS, all live Plan-Net FHIR directories, Stedi enrollments, Stedi payer transaction support, OIG LEIE, CMS opt-out, Order & Referring, Medicare reassignment (individual → groups, or group PAC ID → members), and revalidation due dates. It returns an overall verdict and a per-payer table (Medicare, each FHIR payer, each Stedi-enrolled payer) with **Ready / Likely ready / Not ready / Unknown**, reasons, and sources. Each row has an **Eligibility check** button that opens the Eligibility Readiness tab prefilled with the NPI and payer.

Verdict rules (summary):
- Blockers → overall Not ready: NPI not in NPPES or deactivated; NPI on OIG LEIE.
- Medicare: active opt-out → Not ready; in PECOS → Ready (reassignment groups listed); a revalidation due date that has passed on the CMS list → Likely ready plus a warning; due within 120 days → warning; not in PECOS → Not ready (the public extract lags).
- `GET /readiness-report?npi=&group_npi=` (optional): warns when the individual has no Medicare reassignment to that group, naming the groups they reassign to instead.
- Order & Referring warnings are skipped for provider types that can't order/refer (SLP, PT, OT, counselors, social workers). State-only payer directories (BCBSRI) are hidden for providers in other states and noted under skipped sources.
- Other payers: Stedi enrollment LIVE + listed in the payer's directory → Ready; one of the two → Likely ready; enrollment pending or rejected → Not ready; nothing found → Unknown (absence from a directory isn't proof).

Public sources used (all free, no key): NPPES API; data.cms.gov Medicare FFS Public Provider Enrollment, Opt Out Affidavits (`9887a515-…`), Order and Referring (`c99b5865-…`), Revalidation Reassignment List (`20f51cff-…`), Revalidation Due Date List (`3746498e-…`); HHS-OIG LEIE `UPDATED.csv` (downloaded on first use, cached in memory, refreshed every 24 h; DOBs never stored or returned). Skipped: SAM.gov (needs an API key), state Medicaid lists (no common free API), OAuth-only payer directories.

Deep links: `/?tab=find&q=Aeterna%20Aesthetics&state=OH` runs the search; `/?npi=1699443499` opens the readiness report. (Single Lookup moved to `/?tab=lookup&npi=…`.) Browser history for reports stores only NPI, provider name, verdict, and time.

## Eligibility Readiness Check

Goal: tell whether a payer recognizes a provider NPI (a proxy for "ready to submit claims") before any claim exists. The app sends one Stedi real-time eligibility check (270/271) under the provider's NPI for one real member on that payer's plan and classifies the 271:

| Classification | Meaning |
| --- | --- |
| `RECOGNIZED` | Active coverage returned, no provider-related AAA errors |
| `PROVIDER_NOT_RECOGNIZED` | Provider-related AAA: 41, 43, 44, 45, 46, 47, 49, 50, 51, 52, 53, 97, or 35 (Out of Network) |
| `PATIENT_ISSUE` | Member/patient AAA (58, 60, 61, 64–78, e.g. 72, 73, 75, 76) — inconclusive for the provider |
| `PAYER_UNAVAILABLE` | AAA 42 (retryable follow-up), 79, 80; HTTP 429/5xx; timeouts. Stedi documents 79 as a payer connectivity/routing problem, so it is not treated as a provider rejection |
| `INCONCLUSIVE` | Anything else (inactive coverage, 42 with "Please Correct and Resubmit", Stedi 400 validation errors, payer doesn't support real-time eligibility) |
| `PAYER_AMBIGUOUS` | The payer text matched several Stedi payers; the response lists `payer_matches` to pick from. No check is sent |

The response also includes the AAA codes with descriptions/level/follow-up, coverage active yes/no, plan status, payer name, in/out-of-network benefit-tier indicators (benefit-tier info, **not** proof of participation), and the Stedi transaction-enrollment status for that NPI + payer. A `RECOGNIZED` result is a strong signal, not proof of contracting or credentialing.

```bash
curl -sS -X POST http://127.0.0.1:8080/eligibility-readiness \
  -H 'Content-Type: application/json' \
  -d '{"npi":"1999999984","payer":"87726","provider_organization_name":"Provider Name",
       "member_id":"UHC123456","first_name":"Jane","last_name":"Doe","date_of_birth":"1971-01-01"}'
```

Inputs: `npi`, `payer` (Stedi payer ID, primary payer ID, alias, or name), optional `provider_organization_name` or `provider_first_name`/`provider_last_name` (NPPES is used when omitted), `member_id`, `first_name`, `last_name`, `date_of_birth`, optional `date_of_service`, `service_type_code` (default `30`), and optional `dependent_*` fields.

PHI handling: request/response bodies and member data are never logged or stored on the server; the response omits member name, DOB, and raw X12, and masks the member ID; validation errors don't echo submitted values; the browser history stores only NPI, payer, classification, and time.

The UI is the **Eligibility Readiness** tab. Deep link: `/?tab=eligibility&npi=1699443499&payer=cigna`. The values above are a Stedi mock request; mock requests only return mock data with a Stedi test API key (with a production key they'd go to the real payer).

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
- Real-time eligibility (270/271 JSON, legacy shape): `POST https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3`. Stedi now also offers a newer `POST https://healthcare.us.stedi.com/2026-06-01/eligibility-check` with a different request/response shape; the legacy v3 endpoint is still supported and is what this app uses.
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
test_eligibility.py Eligibility readiness unit tests (mocked Stedi, PHI-not-logged checks)
eligibility.py      Eligibility readiness request building + 271 classification
name_search.py      Friendly NPPES name search, ranking, affiliated providers
readiness.py        Readiness-to-bill report and verdict logic
clients/cms_extra.py  CMS opt-out, Order & Referring, reassignment, revalidation
clients/leie.py     HHS-OIG LEIE exclusion list (cached CSV)
test_find_report.py Name search / sources / verdict tests (+ live NPPES checks)
clients/nppes.py    CMS NPPES
clients/pecos.py    CMS PECOS public enrollment
clients/fhir.py     Plan-Net FHIR
clients/stedi.py    Stedi enrollments, payer search, real-time eligibility
clients/registry.py Live and unsupported payer lists
```
