# Noble invoice decision engine — Azure Function

HTTP-triggered Azure Function (Python v2) that is the **decision engine** for the
invoice pipeline. A Power Automate cloud flow fetches the SharePoint file and
calls this function; the function decides what should happen to the invoice,
records it on the ledger, and returns the decision. It is the code realisation of
the v8 design.

## What it does (and does not do)

Per request it runs gate **A1** (item-id concurrency guard), writes the ledger row
`Received`, calls **Content Understanding** with the document **bytes** (binary
transport — no Blob, no SAS), applies the routing gates via `gates.py` — **B2**
(router/effective category `other` → reject), **B4** (a critical field for the
resolved bill-type bucket is missing, empty, low-confidence, or fails its format
rule — e.g. `po_or_job_number` must be exactly 8 digits; municipal bills
additionally require `account_number` and `invoice_number` — → review), and a
no-child-extraction review — using the bill-type policy in `field_policy.py`,
writes `Extracted` + the routing decision, and returns the decision.

It does **not**: write to Dataverse, write the review-queue item, or run a B1
classifier-confidence gate (B1 does not exist at GA). **B3** (handwriting) and the
**GST-math** reconciliation gate were retired — handwriting is surfaced as advisory
only, and GST is enforced for commercial bills via critical-field confidence. **There is no
duplicate-invoice verification anywhere in the pipeline (requirement): no
Dataverse duplicate query and no alternate key.** The Power Automate flow remains
the single owner of the Dataverse write and writes the terminal `Written` +
`DynamicsRecordId` to the ledger only after Dataverse returns 201.

## Files

| File | Role |
|------|------|
| `function_app.py` | HTTP entry point; orchestrates A1 → Received → CU → gates → Extracted → return |
| `gates.py` | CU-output parsing helpers, child-content selection, routing-gate evaluator (B2 / B4 / no-child); imports `field_policy` |
| `field_policy.py` | Single source of truth for field rules: threshold, per-bucket critical fields, bill-type bucket resolution, date/amount write-values |
| `cu_client.py` | CU binary/url analyze wrapper (same GA SDK calls as `step24_test.py`) |
| `ledger.py` | `InvoiceExtractProcessLog` client (lifted from `phase3_ledger_test.py`) |
| `requirements.txt`, `host.json` | Function app config |
| `local.settings.json.template` | Copy to `local.settings.json` for local runs (do not commit secrets) |

The PDF-posting client `verify_fn.py` lives in `../scripts/`, not here (it is a
harness, not part of the deployable unit; it targets the local `func start` host
by default and the deployed app with `--base-url`/`--key`). The offline
gate/policy suite is in `../tests/` — run it with `python -m pytest` from the
repo root.

## Request / response

`POST /api/process-invoice`

```json
{
  "sourceId": "<SharePoint item UniqueId GUID>",   // required — A1 key / row identity
  "contentBase64": "<base64 of the PDF bytes>",     // required for binary transport
  "fileName": "invoice1.pdf",                       // optional — content-type hint, provenance,
                                                    //   municipal invoice-number fallback (ext stripped)
  "sourceFileUrl": "https://.../invoice1.pdf",      // optional — stored on the ledger
  "url": "https://...blob...?sas",                  // optional — ad-hoc test only, instead of contentBase64
  "fieldThreshold": 0.75                            // optional — overrides field_policy.THRESHOLD (default 0.73)
}
```

Response (HTTP 200 on a normal decision):

```json
{
  "sourceId": "...", "partitionKey": "0f", "rowKey": "0fb9...d77",
  "alreadyProcessed": false, "skippedCU": false, "status": "Extracted",
  "routingDecision": "HAPPY_PATH_CANDIDATE",
  "effectiveDocumentType": "general_invoice",
  "category": "general_invoice", "routerCategory": "general_invoice",
  "routerCategoryPath": "$.contents[0].segments[0].category",
  "analyzerUsed": "generalinvoice", "childSelection": "matched analyzerId == generalinvoice",
  "billType": "commercial", "subBillType": "repair",
  "policyBucket": "commercial", "policyVersion": "commercial-narrative-v10",
  "isHandwritten": "no", "isHandwrittenConfidence": 0.97,
  "reviewReasons": [], "advisoryFlags": [],
  "fields": { "vendor_name": {"value": "...", "confidence": 0.93}, "...": {} },
  "writeValues": { "vendor_name": "...", "invoice_date": "2026-05-01",
                   "payment_due_date": "2026-05-31", "amount_excluding_gst": 100.0,
                   "account_number": "Account No: 123456789012", "sub_bill_type": "repair",
                   "billing_period_start_date": "2026-01-01",
                   "billing_period_end_date": "2026-03-31", "number_of_days": 83,
                   "diagnosis_solution": "Traced the leak to a hole in the membrane and made a temporary repair.",
                   "diagnosis_solution_zh_hant": "調查漏水，確認源頭為防水膜破洞，已完成臨時修補。",
                   "recommendation": "Carry out a permanent repair with an EPDM kit.",
                   "recommendation_zh_hant": "建議使用 EPDM 套件進行永久修復。",
                   "warranty": "Repairs are not guaranteed; further service calls are chargeable.",
                   "...": null },
  "defaultedFields": [], "anomalyFlag": ""
}
```

The five narrative fields — `diagnosis_solution`, `recommendation`, `warranty` and the
Traditional Chinese twins `*_zh_hant` — summarise what the vendor found, did, recommends
next, and warrants. They are **always strings, never null**, and are **blank on the
municipal bucket**: a water or hydro bill diagnoses nothing, so `build_write_values`
forces all five to `""` there rather than trusting the prompts to decline. Their length
limits (800 / 800 / 400 characters) live in the analyzer prompts only — nothing truncates
them in code, so size the Dataverse columns with headroom (see the Phase-4 note below).

`routingDecision` is one of: `HAPPY_PATH_CANDIDATE`, `REVIEW_B4_CRITICAL_FIELD`,
`REJECT_B2_OTHER_CATEGORY`, `REVIEW_NO_CHILD_EXTRACTION`. (`REVIEW_B3_HANDWRITTEN_OR_UNKNOWN`
and `REVIEW_GST_MATH` were retired with the B3/GST gates.)

**The B2 rescue.** A router category of `other` binds no child analyzer, so CU chains
nothing and the reject carries no fields at all — a reviewer would re-key every value by
hand. When that happens the Function makes a second analyze call straight to the
general-invoice analyzer and re-runs the gates on the result, and the document is then
judged **exactly like any other**: B4 decides, so a clean extraction reaches
`HAPPY_PATH_CANDIDATE` and a weak one lands in `REVIEW_B4_CRITICAL_FIELD`. (An earlier
version forced a review because the router had disagreed; that was measured to protect
nothing, since B4 already catches the shapes the router rejects correctly.)

A rescued decision is therefore indistinguishable from a normal one by `routingDecision`
alone. Provenance lives in three places: `routerCategory` stays `other`, `advisoryFlags`
records how many values were recovered, and the ledger stamps **`RouterCategory`** — query
`RouterCategory eq 'other'` to find every rescued invoice.

The rescue is best-effort: any failure, timeout, or re-analysis that recovers no fields
leaves the original `REJECT_B2_OTHER_CATEGORY` untouched. It is given only the analyze
budget left over from the first call, so two calls never exceed the single-call cap.

On a `REVIEW_B4_CRITICAL_FIELD` decision, `reviewReasons` holds a **single
reviewer-facing summary** naming every failing critical field — e.g.
`vendor_name and po_or_job_number need attention`. The per-field diagnostics
(confidence values, which twin failed, format hints) are in `advisoryFlags`
with a `B4 ` prefix.

`subBillType` (also `writeValues.sub_bill_type`) is the resolved sub-classification
of `billType` — informational only, it never gates routing. A commercial bill
derives it from the resolved `po_or_job_number` (Noble's numbering scheme): a
format-valid PO starting `33` → `service`, starting `11` → `repair` (only the
first two digits are consulted; an OCR-rescued PO counts); a missing or
format-violating PO → `other`. The classified
label is ignored on commercial bills. A municipal bill resolves the classified
`sub_bill_type` label — trusted when it clears its own confidence bar (0.80,
stricter than the critical-field threshold) OR when the `sub_bill_type_generate`
reasoning twin returns the same label (CU's estimated confidence on classify
fields is noisy on identical documents; two independent reads agreeing are not) —
accepting `gas`, `electric`, `water` (includes sewer/stormwater and combined city
utility bills), or `business_license` (city-issued business licence/permit
renewals). Everything else — unconfirmed below-bar labels, unknown or
cross-bucket labels, property tax — resolves to `other`.

`vendor_name` twin resolution: the two spellings name the same vendor when they are
equal after normalisation (casing, punctuation and a trailing legal suffix are
ignored), when one contains the other (`FortisBC Energy Inc.` / `FortisBC`), or when
one's words are a subset of the other's — that last form catches a shortened personal
name (`Simon Kan` / `Simon Sik Fai Kan`) whose dropped words sit in the middle. On
agreement the written spelling is the generate twin's whenever both twins carry the
same name (so casing stays clean and `Ltd.`/`Inc.` stays dropped); when the spellings
genuinely differ, whichever twin the model was **more confident** in is written, with
a tie keeping the normalised generate name. Runs of whitespace in the written value
are collapsed — OCR breaks a long company name across a line on some runs and not
others, and that line break is an artifact, not part of the name. (`service_address`
keeps its legitimate multi-line form.)

Municipal invoice-number fallback: when a municipal bill's invoice-number twins
both come back empty, the field defaults to `fileName` without its extension.
The response marks it three ways — `defaultedFields` gains `invoice_number`,
`resolutions.invoice_number.source` is `"filename"`, and an advisory flag
records the substituted value. With no usable `fileName` the field fails to
review exactly as before; a present-but-low-confidence value is never
overwritten.

`invoice_date` is an extract + generate twin resolved like `vendor_name` (agreement
boost included: two sub-threshold twins naming the same calendar day pass, with
`resolutions.invoice_date.source` = `"agreement"`). Only when the twins resolve to
nothing usable — absent, unparseable, or below the bar and disagreeing — does the
write value fall back to **today (PST)**, recorded in `defaultedFields`. Unlike the
other twins, a confident generate value does not rescue an *absent* extract here
(`field_policy.NO_GENERATE_RESCUE`): with no extract span behind it the reasoning
twin has been seen answering with a page-footer print timestamp. It is accepted only
when **corroborated** — the same calendar day printed in the OCR text in an
unambiguous month-name or ISO form (`Jun 17, 2026`, `2026-06-17`), which yields
`resolutions.invoice_date.source` = `"corroborated"` and an advisory flag. Slashed
forms (`1/13/26`) never corroborate: they are ambiguous, and the print timestamp is
always printed that way. A resolved
date **after today** (America/Vancouver) routes the run to
`REVIEW_B4_CRITICAL_FIELD` — a future issue date is either a misread or a document
that shouldn't be paid yet — with the extracted date written unchanged so the
reviewer sees what the document said. `invoice_date` is otherwise never critical: an
unresolved one defaults quietly and does not trigger review.

Billing-period fields (municipal utility bills): `billing_period_start_date`,
`billing_period_end_date`, and `number_of_days` feed the tenant utility-sharing
calculation. Each is an extract + generate twin resolved like `vendor_name`
(including the agreement boost: two sub-threshold twins naming the same
date/day count pass with source `"agreement"`). They are **informational only**
— never critical, never gate routing — and the dates are normalised to
`YYYY-MM-DD` but **never defaulted**: a bill that doesn't state a value writes
`""` (a substituted date would corrupt the cost sharing). When a bill prints no
full start date (a month-only period like `Mar/Apr 2026`, or none at all), the
generate twin returns the meter Reading Date as the period end and the start is
derived in code as `end − (number_of_days − 1)` — period inclusive of both
endpoints — with `resolutions.billing_period_start_date.source` = `"derived"`,
confidence the weaker of the two inputs, and an advisory flag recording the
derivation. A printed start value, even below the confidence bar, is never
overwritten by the derivation.

Sectioned-bill GST (municipal bills only): a utility bill that splits its charges
into sections prints a GST line under each and need not print a bill-level recap,
and on such a bill CU has been observed returning one section's line from *both*
twins — so they agree, the pair passes on corroboration, and no twin-resolution
rule can catch it. The GST amounts printed in the OCR text are read instead: when
two or more appear, the answer is the printed recap (the one amount equal to the
sum of the others) or, when none is printed, their sum. The candidate is written
only if it satisfies the bill's own arithmetic — GST ≈ 5% of `total − gst − pst` —
and the value CU resolved does not, with `resolutions.gst_amount.source` =
`"sectioned_sum"`, confidence `1.0`, and an advisory flag. Text proposes the
candidate and arithmetic confirms it; neither is trusted alone, so a bill carrying
an untaxed charge (a security deposit, a levy) fails the check and is left alone.
`amount_excluding_gst` is recomputed from the corrected value.

`vendor_name` has one further rescue, for letterheads where the vendor's name is
printed **only as a stylized logo**. The extract prompt is told to prefer clearly
printed text over a graphic wordmark, so on such a page it takes whatever plain
text sits nearby — on one corpus invoice, the dispatch service in the top-right
contact block — and the twins then disagree with both below the bar. The vendor's
own web or e-mail domain is printed on the same letterhead and *is* machine
readable, so when the twins name genuinely different vendors and exactly one of
them matches a printed domain label, that one is written, with
`resolutions.vendor_name.source` = `"domain_corroborated"` and an advisory flag.
Deliberately inert otherwise: twins that name the same vendor in two spellings
("District of West Vancouver" vs "West Vancouver") are left to the ordinary twin
resolution, since a city's domain always matches the shorter form.

When gate A1 short-circuits (another invocation is processing the same item), the
response has `alreadyProcessed: true`, `skippedCU: true`, and `routingDecision`
`PROCESSING_IN_PROGRESS`. The Power Automate flow treats `alreadyProcessed: true` as a
no-op — no Dataverse write, no review item.

Status codes: `200` decision returned (including review/reject); `400` bad request;
`502` Content Understanding failed (claim released — row set to `Failed`, see A1
below — so the caller's automatic retry re-processes immediately); `500`
ledger/config failure.

## Gate A1 semantics — atomic concurrency claim (re-uploads re-process)

A1 no longer de-duplicates re-uploads: the same file uploaded again (same
SharePoint item id) is **re-processed in full**, because a corrected version of
an invoice may arrive as the same file. Each re-run resets the ledger row to
`Received` and overwrites it with the new decision; on the happy path the flow
writes **another** Dataverse row (there is no duplicate-invoice verification —
requirement).

What A1 still guarantees is that two **concurrent** invocations for the same item
(a re-fired SharePoint trigger) cannot both process it:

- **No row yet** → atomically claim it (insert `Received`). The insert is atomic
  in Table Storage, so of two perfectly concurrent triggers exactly one claims
  and proceeds; the other sees the row and short-circuits.
- **Row decided (incl. `Failed`), or stale at `Received`** (older than
  `A1_LEASE_SECONDS`, default 300 — a crashed prior run) → atomically
  **re-claim** it (etag-conditioned reset to `Received`) and re-process; a
  concurrent invocation that loses the re-claim short-circuits.
- **Row at `Received` within the lease** → skip with `PROCESSING_IN_PROGRESS`;
  another invocation owns the item.
- **CU failure releases the claim**: the `502` handler resets the row to
  `Failed` (with `FailedStage`/`LastError`), conditioned on the etag of **this
  invocation's own claim**, so the caller's automatic retry re-claims and
  re-processes within seconds instead of hitting `PROCESSING_IN_PROGRESS` for
  the rest of the lease (which silently no-ops the retry and strands the
  invoice — the SharePoint trigger fires only once). If the conditional write
  loses (a newer invocation re-claimed first), nothing is stamped. Only a hard
  crash (no stamp at all) still waits out the lease — the flow's retry policy
  must put the **first** retry after such a failure beyond the lease (interval
  > lease − 120 s; retries stop at the first success, and the 200
  `PROCESSING_IN_PROGRESS` no-op counts as a success — see
  `docs/power-automate-design.html`).

The `reprocess` request flag was removed along with the dedup — re-processing is
now the default. Requests that still send it are accepted; the field is ignored.

## Authentication

**Content Understanding** — key auth (`AZURE_CU_KEY`) is the validated default; set
it as a Key Vault reference in the app settings, e.g.
`@Microsoft.KeyVault(SecretUri=https://kv-invoice-process-dev.vault.azure.net/secrets/cu-key/)`.
If `AZURE_CU_KEY` is empty the client falls back to `DefaultAzureCredential`
(managed identity); the identity then needs **Cognitive Services User** on
`invoice-processing-dev-resource`.

**Table Storage** — `DefaultAzureCredential` against `AZURE_STORAGE_ACCOUNT`; the
identity needs **Storage Table Data Contributor** on `stinvoicedevwestus`. For a
quick local run you can set `AZURE_TABLES_CONNECTION_STRING` instead.

**Managed identity** — the app uses the user-assigned identity
`id-invoice-processing-dev` (both roles already granted). Set `AZURE_CLIENT_ID` to
that identity's client id so `DefaultAzureCredential` picks the right one when
several identities are present.

## Run locally

```cmd
cd functionapp
copy local.settings.json.template local.settings.json
:: put the CU key in AZURE_CU_KEY (or leave blank to use your az login identity),
:: and set AZURE_STORAGE_ACCOUNT (your az login identity needs the table role)
python -m pip install -r requirements.txt
func start
```

Then, in another shell (the client lives in `../scripts/`; no `--base-url` needed
— it defaults to the `func start` host):

```cmd
cd scripts
python verify_fn.py --file "..\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77
:: run the same line again -> full re-process (A1 no longer skips decided items)
```

`gates.py` and `field_policy.py` have no Azure dependency; the suite in `../tests/`
exercises them offline (`python -m pytest` from the repo root).

## Deploy

```cmd
az account set --subscription "dev-Document Intelligence"
func azure functionapp publish <function-app-name>
```

Set the same `AZURE_*` values as application settings on the Function App, assign
the user-assigned identity, and confirm the two role assignments above. A
Consumption or Flex Consumption plan is fine; the choice does not affect this code.

## Power Automate integration (next phase)

The orchestrator is a Power Automate cloud flow (not Logic Apps). Call this
function through a **custom connector**, not the HTTP action — the HTTP action
can't use managed identity, and a custom connector keeps the function key in the
connection (encrypted at rest, never in run history) rather than in the flow.

1. SharePoint trigger → **Get file content** and read the item **UniqueId**.
2. Call the custom connector's `process-invoice` action with `sourceId` = item
   UniqueId, `contentBase64` = `base64()` of the file content, `fileName`,
   `sourceFileUrl`. The function key lives in the connection, so it isn't in the
   request body.
3. Branch (Condition/Switch) on `routingDecision`:
   - `HAPPY_PATH_CANDIDATE` → **Add a new row** to the Dataverse invoice table →
     on 201, update the ledger row (`Status=Written`, `DynamicsRecordId`).
     `writeValues` now includes `account_number` (required on municipal bills,
     optional on commercial; since `commercial-narrative-v10` the written value
     carries its own label — `"Account No: 123456"` on both bill types, `""`
     when the bill prints none), `sub_bill_type` (gas / electric / water /
     business_license / service / repair / other), and the billing-period trio
     `billing_period_start_date` / `billing_period_end_date` (`YYYY-MM-DD` or
     `""`) and `number_of_days` (integer or `""`) for the tenant
     utility-sharing calculation — map each to the matching Dataverse column.
     Since `commercial-narrative-v9` it also includes the five narrative fields
     `diagnosis_solution` / `recommendation` / `warranty` and the Traditional
     Chinese `diagnosis_solution_zh_hant` / `recommendation_zh_hant` (always a
     string, `""` on municipal bills) — map each to a **Multiple Lines of Text**
     column sized **1000 / 1000 / 500** so an occasional over-length summary
     cannot fail the row write.
   - any `REVIEW_*` / `REJECT_*` → write the SharePoint review-queue item (the
     approval flow later re-enters the same write action, which adds the row).
   - `alreadyProcessed: true` → do nothing.

Auth boundaries:
- **Flow → function:** custom connector. Prototype: API-key auth with the function
  key in the connection. Production: Entra federated-credential managed identity
  (secretless) — set the function to Easy Auth and `auth_level=ANONYMOUS`.
- **Flow → Dataverse / SharePoint:** connection references (a user or
  service-principal connection), not managed identity.
- **Function → CU / Table Storage:** the function's own user-assigned managed
  identity — unchanged by the orchestrator switch.

There is no duplicate query before the write: per requirement, duplicate invoices
are not verified, so the happy path writes directly.

> Synchronous-call note: this function responds synchronously after the CU call.
> Keep CU latency under the connector's request timeout; if it approaches the
> limit, switch the function to the async 202 + `Location` polling pattern.

> Corporate network note: `curl` to the endpoint needs `--ssl-no-revoke --insecure`.
> `verify_fn.py` uses Python's urllib and is unaffected for local http; against
> the deployed endpoint pass `--insecure`.

> Naming note: the ledger column is `DynamicsRecordId` (matching the validated
> Phase 3 table), which is the design's `DataverseRowId` placeholder.
