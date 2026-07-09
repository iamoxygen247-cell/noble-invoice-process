# Noble invoice decision engine — Azure Function

HTTP-triggered Azure Function (Python v2) that is the **decision engine** for the
invoice pipeline. A Power Automate cloud flow fetches the SharePoint file and
calls this function; the function decides what should happen to the invoice,
records it on the ledger, and returns the decision. It is the code realisation of
the v8 design.

## What it does (and does not do)

Per request it runs gate **A1** (item-id idempotency), writes the ledger row
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
  "reprocess": false,                               // optional — bypass gate A1
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
  "policyBucket": "commercial", "policyVersion": "sub-bill-type-v2",
  "isHandwritten": "no", "isHandwrittenConfidence": 0.97,
  "reviewReasons": [], "advisoryFlags": [],
  "fields": { "vendor_name": {"value": "...", "confidence": 0.93}, "...": {} },
  "writeValues": { "vendor_name": "...", "invoice_date": "2026-05-01",
                   "payment_due_date": "2026-05-31", "amount_excluding_gst": 100.0,
                   "account_number": "123456789012", "sub_bill_type": "repair",
                   "...": null },
  "defaultedFields": [], "anomalyFlag": ""
}
```

`routingDecision` is one of: `HAPPY_PATH_CANDIDATE`, `REVIEW_B4_CRITICAL_FIELD`,
`REJECT_B2_OTHER_CATEGORY`, `REVIEW_NO_CHILD_EXTRACTION`. (`REVIEW_B3_HANDWRITTEN_OR_UNKNOWN`
and `REVIEW_GST_MATH` were retired with the B3/GST gates.)

On a `REVIEW_B4_CRITICAL_FIELD` decision, `reviewReasons` holds a **single
reviewer-facing summary** naming every failing critical field — e.g.
`vendor_name and po_or_job_number need attention`. The per-field diagnostics
(confidence values, which twin failed, format hints) are in `advisoryFlags`
with a `B4 ` prefix.

`subBillType` (also `writeValues.sub_bill_type`) is the resolved sub-classification
of `billType` — informational only, it never gates routing. A commercial bill
derives it from the resolved `po_or_job_number` (Noble's numbering scheme): a
format-valid PO starting `330` → `service`, starting `110` → `repair` (an
OCR-rescued PO counts); a missing or format-violating PO → `other`. The classified
label is ignored on commercial bills. A municipal bill resolves the classified
`sub_bill_type` label — trusted when it clears its own confidence bar (0.80,
stricter than the critical-field threshold) OR when the `sub_bill_type_generate`
reasoning twin returns the same label (CU's estimated confidence on classify
fields is noisy on identical documents; two independent reads agreeing are not) —
accepting `gas`, `electric`, `water` (includes sewer/stormwater and combined city
utility bills), or `business_license` (city-issued business licence/permit
renewals). Everything else — unconfirmed below-bar labels, unknown or
cross-bucket labels, property tax — resolves to `other`.

Municipal invoice-number fallback: when a municipal bill's invoice-number twins
both come back empty, the field defaults to `fileName` without its extension.
The response marks it three ways — `defaultedFields` gains `invoice_number`,
`resolutions.invoice_number.source` is `"filename"`, and an advisory flag
records the substituted value. With no usable `fileName` the field fails to
review exactly as before; a present-but-low-confidence value is never
overwritten.

When gate A1 short-circuits, the response has `alreadyProcessed: true`,
`skippedCU: true`, and `routingDecision` is the stored decision (or
`PROCESSING_IN_PROGRESS`). The Power Automate flow treats `alreadyProcessed: true` as a
no-op — no Dataverse write, no review item.

Status codes: `200` decision returned (including review/reject); `400` bad request;
`502` Content Understanding failed (row left at `Received` — see A1 below); `500`
ledger/config failure.

## Gate A1 semantics — atomic idempotency claim

A1 prevents the **same SharePoint upload** being processed twice; it is not
duplicate-invoice verification. It uses an **atomic insert-claim** on the ledger:

- **Already decided** (`RoutingDecision` set, or `Status` past `Received`) → return
  the stored decision, skip Content Understanding. The invoice is not reprocessed.
- **No row yet** → atomically claim it (insert `Received`). The insert is atomic in
  Table Storage, so of two perfectly concurrent triggers exactly one claims and
  proceeds; the other sees the row and short-circuits.
- **Claim lost / row in-flight within the lease** → skip with
  `PROCESSING_IN_PROGRESS`; another invocation owns the item.
- **Row stale at `Received`** (older than `A1_LEASE_SECONDS`, default 600) → a
  crashed prior run; **resume** it. A transient CU failure thus recovers on the
  next trigger rather than being skipped forever.

Send `"reprocess": true` to force a full re-run past A1.

The atomic claim replaces the role the Dataverse alternate key used to play at the
ingestion level, so a re-fired trigger cannot create two Dynamics rows for one
upload. It does **not** detect the same invoice arriving by a different route: a
re-upload as a *new* SharePoint file (new item id) or a manual Dynamics entry is
not seen, and will create another Dynamics row. That is the accepted consequence
of removing duplicate-invoice verification.

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
:: run the same line again -> alreadyProcessed:true, skippedCU:true (gate A1)
python verify_fn.py --file "..\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77 --reprocess
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
     optional on commercial) and `sub_bill_type` (gas / electric / water /
     business_license / service / repair / other) — map each to the matching
     Dataverse column.
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
