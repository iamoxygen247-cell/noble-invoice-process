# Troubleshooting

Confirmed, verified environment/debugging lessons for this project. Keep entries
reusable and secret-free; machine-specific paths and account values belong in
`CLAUDE.local.md` (gitignored).

---

## Azure CLI (`az`) fails with SSL / connection-reset errors behind TLS inspection

**Symptoms (this dev machine, June 2026):**

- `az account show` works (it only reads the local token cache), but any `az`
  command that hits the network fails.
- Without a CA bundle: `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`.
- After adding the inspection root via `REQUESTS_CA_BUNDLE`: OpenSSL 3.x then
  strict-rejects it with `Basic Constraints of CA cert not marked critical`.
- Intermittently (even once TLS verifies): `ConnectionResetError 10054` during the
  TLS handshake.
- `func azure functionapp publish` and other .NET tools work fine.

**Cause:** a transparent TLS-inspecting security agent re-signs HTTPS traffic.
`func`/.NET trust the agent's root automatically via the Windows certificate store
(SChannel). `az` ships its own Python + OpenSSL 3.x with the `certifi` bundle, which
(a) doesn't include the agent root and (b) strict-rejects it once added. There is no
proxy configured (`netsh winhttp show proxy` = direct), so this is in-line interception,
not a proxy you can point `az` at.

**Why env-var/cert workarounds don't stick:** the MSI `az.cmd` launches
`python.exe -IBm azure.cli` (isolated mode) and the bundled Python has a `._pth` file,
so `PYTHONPATH`, `sitecustomize.py`, and user-site hooks are all ignored. Editing the
install directly needs admin (it lives under `C:\Program Files\Microsoft SDKs\Azure\CLI2`).

**Fix (verified, no admin):** make `az`'s Python verify via the OS store using
[`truststore`](https://pypi.org/project/truststore/), exactly like `func`/.NET.

1. Stage a bootstrap dir (e.g. `%LOCALAPPDATA%\az-truststore`) containing the pure-Python
   `truststore` package plus an `azrun.py`:

   ```python
   import os, sys
   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
   import truststore
   truststore.inject_into_ssl()
   import runpy
   sys.argv = ["az"] + sys.argv[1:]
   runpy.run_module("azure.cli", run_name="__main__")
   ```

   To bootstrap-install `truststore` past the same TLS block, use
   `pip install truststore --trusted-host pypi.org --trusted-host files.pythonhosted.org`
   (scoped to that one download), or copy the package folder from a venv.

2. Add an `az` function to the PowerShell profile so plain `az ...` runs the bootstrap,
   bypassing `-I`:

   ```powershell
   function az {
       & 'C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe' -B "$env:LOCALAPPDATA\az-truststore\azrun.py" @args
   }
   ```

**Caveat:** the inspection agent still intermittently resets TLS handshakes
(`WinError 10054`) regardless of the cert fix. Re-run the command, or wrap calls in a
short retry loop. Heavier responses (`functionapp show`, full `list`) hit it more often.

**Alternatives that sidestep the local agent entirely:** Azure Cloud Shell or the Azure
Portal (browser is allowlisted); or ask IT to allowlist `az`'s `python.exe` / supply the
corporate root in a form OpenSSL accepts.

---

## `func azure functionapp publish` fails: "Unable to connect to Azure"

**Symptoms (this dev machine):** `func azure functionapp publish <app> --build remote`
exits immediately with `Unable to connect to Azure. Make sure you have the az CLI or
Az.Accounts PowerShell module installed and logged in` â€” even though `az account show`
(via the truststore wrapper) shows a valid login.

**Cause:** `func` shells out to the raw `az.cmd` on PATH (not the PowerShell profile
wrapper) to fetch an ARM token. When the cached access token has expired, `az.cmd`
must refresh over the network and dies on the TLS-inspection SSL failure above, so
`func` sees no credential.

**Fix (verified 2026-07-07):** pre-warm the az token cache through the truststore
bootstrap, then publish â€” `az.cmd` serves `func` the cached token without a network call:

```powershell
& 'C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe' -B "$env:LOCALAPPDATA\az-truststore\azrun.py" account get-access-token --output none
cd functionapp
func azure functionapp publish func-invoiceprocess-westus --build remote
```

The publish itself (Kudu remote build, host health check) is .NET and works fine.
The cached ARM token lasts roughly an hour; re-run the first command if publish
fails again after a long gap.

---

## `func start` local run: CU call fails with `CERTIFICATE_VERIFY_FAILED`, then `401`

**Symptoms (this dev machine):** running the decision Function locally (`func start` +
`scripts/verify_fn.py`, which defaults to the localhost host) returns HTTP 502 with
`Content Understanding analyze failed: [SSL: CERTIFICATE_VERIFY_FAILED] ... unable to get
local issuer certificate`. The host, the Azurite-backed ledger, and gate A1 all work â€” only
the outbound HTTPS call to Content Understanding fails.

**Cause:** the same transparent TLS-inspecting agent as the `az` entry above, but here it hits
the **Python worker** the func host spawns. The `.NET` func host trusts the agent root via
SChannel, but the Python worker uses `certifi`, which doesn't include it. This is not
`az`-specific â€” any venv-Python outbound HTTPS (the Functions worker, or the standalone
harness scripts) is affected.

**Fix:** make the worker verify via the Windows cert store with
[`truststore`](https://pypi.org/project/truststore/), exactly like the `az` fix. For a local
run, put a `sitecustomize.py` on `PYTHONPATH` (auto-loaded at interpreter startup) before
`func start`:

```python
# sitecustomize.py â€” on a dir added to PYTHONPATH
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
```

`import truststore` must resolve (install it into the venv, or add the staged `truststore`
package dir to `sys.path`/`PYTHONPATH`; the staged location is in `CLAUDE.local.md`). For the
standalone scripts, calling `truststore.inject_into_ssl()` once at startup has the same effect.

**Then a `401` (invalid subscription key):** the CU key hardcoded in `scripts/step21_test.py`
is stale/rotated. Get the current key with
`az cognitiveservices account keys list --name <cu-account> --resource-group <rg>` (run `az`
through the truststore bootstrap above) and set it as `AZURE_CU_KEY` in the gitignored
`functionapp/local.settings.json`. Local storage: run **Azurite** and set both
`AzureWebJobsStorage` and `AZURE_TABLES_CONNECTION_STRING` to `UseDevelopmentStorage=true` so
the ledger stays local (CU has no emulator, so analyze calls still hit the real dev resource).

---

## `scripts/create_analyzer.py`: `DefaultAzureCredential failed` / `CERTIFICATE_VERIFY_FAILED`

**Symptoms (this dev machine):** a bare
`.\.venv\Scripts\python.exe scripts\create_analyzer.py` fails twice over: every
`DefaultAzureCredential` chain entry is unavailable (the `AzureCliCredential` leg dies on the
TLS-inspection `CERTIFICATE_VERIFY_FAILED` above), and even with a key the direct CU HTTPS call
hits the same SSL failure in the venv Python.

**Fix (verified 2026-07-02):** run it with key auth + the truststore bootstrap in one go â€”
load `AZURE_CU_KEY` from the gitignored `functionapp/local.settings.json` into the environment,
and inject truststore before the script runs:

```powershell
$s = Get-Content functionapp\local.settings.json -Raw | ConvertFrom-Json
$env:AZURE_CU_KEY = $s.Values.AZURE_CU_KEY
.\.venv\Scripts\python.exe -c "import truststore; truststore.inject_into_ssl(); import runpy, sys; sys.argv = ['create_analyzer.py']; runpy.run_path('scripts/create_analyzer.py', run_name='__main__')"
```

Expected output ends with `status=ContentAnalyzerStatus.READY`. Remember: editing
`analyzers/*.json` changes nothing in the service until this script is run.

---

## `func start` local run: worker dies with `ZoneInfoNotFoundError: 'No time zone found with key America/Vancouver'`

**Symptoms (verified 2026-07-09):** the func host starts but the Python worker fails to
initialize with `ModuleNotFoundError: No module named 'tzdata'` â†’
`ZoneInfoNotFoundError` from `field_policy.py`'s `ZoneInfo("America/Vancouver")`, and port
7071 never comes up.

**Cause:** Core Tools spawned its own bundled Python (3.14, under
`...\Azure Functions Core Tools\workers\python\...`) instead of the project venv. Putting
`.venv\Scripts` on `PATH` is **not** enough â€” the host only picks the project interpreter
when the venv is *activated*, i.e. the `VIRTUAL_ENV` environment variable is set.

**Fix:** set both before `func start` (this is what `Activate.ps1` does):

```powershell
$env:VIRTUAL_ENV = "<repo>\.venv"
$env:PATH = "<repo>\.venv\Scripts;" + $env:PATH
cd functionapp; func start
```

Combine with the `sitecustomize.py`-on-`PYTHONPATH` truststore shim above for the outbound
CU call, and run Azurite for the ledger.

---

## Python `json.loads` on `functionapp/local.settings.json` fails: `Unexpected UTF-8 BOM`

**Symptoms (verified 2026-07-13):** a Python script reading the gitignored
`functionapp/local.settings.json` with `read_text(encoding="utf-8")` dies with
`json.decoder.JSONDecodeError: Unexpected UTF-8 BOM (decode using utf-8-sig)`.

**Cause:** the file is saved with a UTF-8 BOM (normal for files created by Windows
tooling). PowerShell's `ConvertFrom-Json` strips it silently, so the PowerShell recipes
elsewhere in this doc are unaffected â€” only Python reads hit it.

**Fix:** read with `encoding="utf-8-sig"` (strips the BOM when present, harmless when
absent):

```python
settings = json.loads(path.read_text(encoding="utf-8-sig"))["Values"]
```

---

## CU misreads handwritten receipt-book amounts (drops the cents) and mislabels `is_handwritten`

**Symptoms (confirmed 2026-07-09, `samples/handwritten/260105_0007.pdf`):** a carbon-copy
receipt-book invoice with amounts written in **split dollars | cents columns** ("94 | 50"
with a printed vertical rule, no decimal point) extracted as integer `94`/`4` â€” the cents
sub-column was dropped by CU itself (both twins; no Python bug). The same document was
classified `is_handwritten = no` even after a targeted classify-prompt rewrite was
verified deployed (fetched the live analyzer definition to confirm before concluding).

**Fixes (verified end-to-end 2026-07-10):**

* **Amounts:** the four amount field descriptions in
  `analyzers/create-generalinvoice-analyzer.json` now explain the split dollars/cents
  sub-column convention with a concrete example ("94 and 50 in adjacent sub-columns means
  94.50"). After that, extract AND generate both read 94.50 / 4.50 stably (7/7 replicate
  analyze calls).
* **is_handwritten:** a classify-prompt rewrite alone was NOT enough â€” the label itself
  flips run-to-run on borderline documents (not just the confidence; the same bytes
  returned yes and no on consecutive analyze calls). Fixed with the generate-reasoning-twin
  pattern (`is_handwritten_generate`, like `po_or_job_number_generate`): concrete
  receipt-book genre cues, "OCR recognition errors in values are evidence of handwriting",
  and an explicit tie-break â€” *"when the evidence is mixed or you are unsure, answer yes"*.
  `gates.py resolve_is_handwritten()` surfaces **yes if either twin says yes** (advisory
  flag; a missed handwritten doc is the costly direction). Printed invoices still return
  a clean `no` (the tie-break does not fire on them).

**Reusable lessons:** (1) when a CU prompt fix "doesn't work", first GET the live analyzer
definition and compare â€” the JSON edit may simply not be provisioned; (2) CU classify
labels near the decision boundary are unstable run-to-run, so judge fixes on several
replicate analyze calls, never one; (3) iterate prompt candidates on a scratch analyzer id
(`create_analyzer.py --analyzer-id <scratch>` â€” ids cannot contain `-`) so the analyzer
the Function uses stays untouched until the wording is proven.

---

## Investigating a bad extraction (runbook, added 2026-07-10)

Every processed run persists its raw CU result and decision JSON as blobs in the
`invoice-diagnostics` container, with paths stamped on the ledger row
(`RawResultBlob`/`DecisionBlob`, plus `AnalyzerId`, `CuDurationMs`, and â€” on failed
runs â€” `FailedStage`/`LastError`). Full design/decision record:
`docs/invoice-diagnostics-design.html`; detailed step-by-step runbook (env setup, RBAC,
fault-domain table, symptom quick reference): `docs/invoice-diagnostics-runbook.html`.

1. **Look up the run** by the SharePoint item GUID â€” prints the ledger row and downloads
   both blobs to `out\diag\<rk>\`:

   ```powershell
   .\.venv\Scripts\python.exe scripts\diag.py --source-id <guid>
   ```

   A row stuck at `Received`: read `FailedStage`/`LastError` (CU timeout, auth, throttle).
   Runs that predate the sidecar have no blobs â€” only the ledger stamps.

2. **Read the raw confidences** in `<ts>-raw.json` â€” the per-field values/confidences CU
   actually returned for that run (a re-run is not evidence; labels flap, see the entry
   above).

3. **Split the fault domain** with an offline replay (runs `gates.evaluate` on the stored
   raw JSON, no CU call, no cost):

   ```powershell
   .\.venv\Scripts\python.exe scripts\diag.py --replay out\diag\<rk>\<ts>-raw.json
   ```

   Replay matches the stored `<ts>-decision.json` but the values are wrong â†’ the CU
   analyzer misread the document: fix the prompt on a scratch analyzer with replicates
   (previous entry). Replay differs from what you expect â†’ the bug is in
   `gates.py`/`field_policy.py`: fix the code and re-replay the same stored JSON as the
   regression check (`--field-threshold` to test threshold sensitivity).

---

## A word-count budget in a generate prompt does not cap characters

**Symptoms (confirmed 2026-07-14, `invoice_description`):** the requirement was a
character limit (< 44 chars incl. spaces/punctuation, the UI report truncates beyond
that), and the first prompt phrased it as "6 words or fewer and under 44 characters".
On `samples/commercial/trade1.pdf` the model returned
`'Intrusion security monitoring and GSM service'` â€” exactly 6 words, but 45 chars
(2/3 replicates over). The model satisfies the word budget and ignores the char count;
long words blow through.

**Fix (verified 2026-07-14, 18/18 replicates â‰¤ 41 chars):** make the character limit the
primary rule and tell the model *how to shorten* instead of asking it to count: "Hard
limit: under 44 characters â€” about 4 to 6 short words. Name only the primary service;
drop adjectives, brand or product names, and secondary services rather than exceed the
limit", plus a write-this-never-that counter-example built from the actual failure
(`'Security monitoring service'`, never
`'Intrusion security monitoring and GSM service'`). After that the same document
returned the short form 3/3.

**Reusable lesson:** LLMs can't count characters; a char limit in a prompt only works as
"aim well under, prefer fewer/shorter words, here is what to drop". Judge on 3+
replicates per document (same scratch-analyzer workflow as the entries above).

---

## `az rest` / `az functionapp` die with 10054 even through the truststore bootstrap

**Symptoms (verified 2026-07-14):** with the truststore `azrun.py` bootstrap in place,
`az resource show` / `az storage account list` work, but `az functionapp config
appsettings list` and `az rest` fail every time with
`('Connection aborted.', ConnectionResetError(10054, ...))`.

**Cause:** the TLS-inspecting agent intermittently resets handshakes to
`management.azure.com`. SDK-path az commands go through azure-core's pipeline, which
retries and absorbs the resets; `az rest` and the appservice module's
`send_raw_request` path use a bare `requests` call with no retry, so one reset kills the
command. The same applies to hand-rolled `urllib`/`requests` calls from the venv.

**Fix:** wrap raw ARM REST calls in a short retry loop (â‰¤6 attempts, linear backoff â€”
1â€“2 resets per success are typical), calling the endpoint directly from venv Python with
`truststore.inject_into_ssl()` and a token from
`az account get-access-token --resource https://management.azure.com` (SDK path, works).
For app settings the endpoint is
`POST .../sites/<app>/config/appsettings/list?api-version=2024-04-01`.

**Bonus gotcha (same session):** the Flex Consumption app's default hostname is the
*hashed* form `func-invoiceprocess-westus-<hash>.westus-01.azurewebsites.net`
(`properties.defaultHostName` on the site resource). The bare
`func-invoiceprocess-westus.azurewebsites.net` does **not** resolve â€” a DNS failure
there is not evidence of an outage.

---

## Changing Flex deployment storage: restart is NOT enough â€” stop/start is

**Symptoms (verified 2026-07-14):** after repointing
`functionAppConfig.deployment.storage` (and the `AzureWebJobsStorage__*` settings) to a
new storage account â€” with an ARM readback confirming the new values â€”
`func azure functionapp publish --build remote` still failed in
`[Kudu-ValidationStep]` with `InaccessibleStorageException â€¦ Name or service not known
(<OLD-account>.blob.core.windows.net)`. An ARM `POST â€¦/restart` did not help; the next
publish failed identically.

**Cause:** the Flex deployment (Kudu/Legion) environment is provisioned with the site's
storage config and does not re-read it on a plain restart.

**Fix:** full **stop â†’ start** (ARM `POST â€¦/stop`, wait ~20 s, `POST â€¦/start`), wait
~60 s, then publish. First publish after that validated against the new account and
succeeded end-to-end.

**Also seen:** a one-off `Can't find app with name "â€¦"` from `func publish` while the
site verifiably existed â€” the same TLS-inspector reset hitting func's site enumeration;
just re-run. And `scripts/test.py` against the deployed app needs the truststore
bootstrap like every other venv script (`CERTIFICATE_VERIFY_FAILED: Basic Constraints
of CA cert not marked critical`):

```powershell
# $env:FKEY holds the function key (never inline it on the command line)
.\.venv\Scripts\python.exe -c "import truststore; truststore.inject_into_ssl(); import os, runpy, sys; sys.argv = ['test.py', '--base-url', 'https://<hashed-host>', '--key', os.environ['FKEY'], '--file', 'samples\\commercial\\trade1.pdf']; runpy.run_path('scripts/test.py', run_name='__main__')"
```

---

## A 200 PROCESSING_IN_PROGRESS in Power Automate can be a silently dropped invoice

**Symptoms (verified 2026-07-16):** flow run shows the Process_invoice action
**succeeding** (HTTP 200) with body `alreadyProcessed:true`,
`routingDecision:"PROCESSING_IN_PROGRESS"` -- and the invoice never reaches Dataverse.
The `x-ms-apihub-cached-response: true` header in the run output is an APIHub artifact,
not the cause.

**Cause (from the live ledger row, not inferred):** CU analyze hit its cap -> function
502'd but (old design) left the A1 row at `Received` -> the connector had already timed
out (~120 s budget vs 120 s CU cap) -> Power Automate's default retry re-called ~2 s
later -> retry hit the 600 s A1 lease -> 200 no-op -> flow terminated Succeeded. The
SharePoint trigger fires once, so nothing ever resumed the row.

**Fix (shipped 2026-07-16):** CU failure now *releases* the claim (`Status=Failed`,
etag-conditioned on the failing invocation's own claim etag) so the automatic retry
re-processes immediately; CU cap default lowered to 100 s; the flow needs an explicit
retry policy (Fixed, 4 x PT4M) so the FIRST retry after a claim-holding failure lands beyond
the lease (interval > lease - 120 s; retries stop at the first success and the 200
PROCESSING_IN_PROGRESS no-op counts as a success). Full writeup:
`docs/incident-2026-07-16-silent-drop.html`.

**Diagnosis shortcut:** read the ledger row --
`az storage entity show --account-name stinvoicedevwestus --table-name InvoiceExtractProcessLog --partition-key <pk> --row-key <sourceId> --auth-mode login`.
`IngestedUtc`/`LastUpdatedUtc` deltas expose timeout chains to the second; a row at
`Failed` means retries exhausted (flow run is visibly Failed); a row stuck at
`Received` past the lease means a hard crash.

---

## `func azure functionapp publish` cannot authenticate on this machine; SChannel Kudu deploy works

**Symptoms (verified 2026-07-16):** `func ... publish --build remote` failed three ways in one
session: `Can't find app with name "..."`, `The SSL connection could not be established`, and
`Unable to connect to Azure. Make sure you have the az CLI ... installed and logged in`.

**Cause:** func shells out to *plain* `az` for tokens, and plain `az account get-access-token`
**deterministically** fails here (`CERTIFICATE_VERIFY_FAILED` -- no truststore; it redeems the
refresh token over the network every call, so a warm MSAL cache does not save it). Separately,
the TLS-inspection proxy has burst windows (minutes) where it RSTs **OpenSSL** handshakes
wholesale (kills wrapped az and `az functionapp deployment source config-zip` too) while
**SChannel** (PowerShell `Invoke-WebRequest`) still connects fine.

**Working deploy path (verified end-to-end):**
1. Zip the functionapp payload honoring `.funcignore` (py files + `host.json` + `requirements.txt`).
2. Token via the **wrapped** az (cached ~1 h): `az account get-access-token --resource https://management.azure.com`.
3. Over SChannel (`Invoke-WebRequest`, TLS 1.2): `POST https://<scm-host>/api/publish?RemoteBuild=true`
   with header `Authorization: Bearer <token>`, `Content-Type: application/zip`, body = the zip
   (`-InFile`). SCM host = the site's hashed default hostname with `.scm` inserted
   (first entry of `properties.enabledHostNames` on the ARM site resource). Returns 202 + a
   `Location: .../api/deployments/<id>`.
4. Poll that deployments URL (same Bearer) until `complete: true`; `status` 4 = success, 3 = failed.
5. Verify: ARM `GET .../sites/<app>/functions?api-version=2024-04-01` lists `process_invoice`.

**Do NOT** keep re-running `func publish` or config-zip during an RST burst -- probe first
(`Invoke-WebRequest https://management.azure.com/` returning 400 = transport OK) and prefer the
SChannel path above.

---

## A code default is not the live value — check the function app's settings

**Symptoms (confirmed 2026-07-17):** asked what the A1 staleness lease "is", a code
read of `function_app.py` gave 600 s — but the live dev value was 300 s, set via the
`A1_LEASE_SECONDS` app setting after the 2026-07-16 incident. The wrong number
invalidates the retry-policy math (`interval > lease − 120 s`), which is exactly the
inequality that prevents the silent-drop failure mode.

**Cause:** every tunable in this project (`A1_LEASE_SECONDS`, `AZURE_CU_TIMEOUT_SECONDS`,
`FIELD_CONFIDENCE_THRESHOLD`, …) is an env-var override; the constant in the code is
only the fallback. Docs that say "default N" describe the code, not the environment.

**Fix:** before quoting any tunable as fact, read the live settings:
`az functionapp config appsettings list -g <rg> -n <app>` (on this machine, via the
truststore bootstrap + 10054 retry — see the entries above). Follow-up shipped
2026-07-17: the code default was aligned to the validated 300 s so an environment
missing the app setting (e.g. freshly provisioned prod) inherits a lease that is safe
under the required Fixed 4×PT4M flow retry policy.

---

## CU generate date twin can collapse a shared-year range to the period END date

**Symptoms (verified 2026-07-17):** FortisBC final bill (260605_0006) prints
"Billing period: May 19 - May 31, 2026" but the connector wrote
`billing_period_start_date` = 2026-05-31 -- the period *end*. From the captured CU
output: `start_extract` = null @ 0.904 (per design: "May 19" carries no year of its
own, so no full-date span to ground) and `start_generate` = 2026-05-31 @ 0.458 --
a range collapse onto the end date, with both generate date twins returning the same
value (the known within-run twin correlation; "closed on Sunday May 31, 2026" and
the May 31 meter reading are strong distractors on a final bill).

**Cause (code layer -- the actual bug):** the start-derivation guard in `gates.py`
keyed on value *emptiness*, not resolution *success*. The failed, below-bar generate
value was non-empty, so the deterministic start = end - (days - 1) derivation --
which computes the correct 2026-05-19 from the resolved end 2026-05-31 and days 13 --
never ran. Billing-period fields are informational-only (never gate routing), so
`passed: false` produced no review and the wrong date shipped on the happy path.

**Fix (shipped 2026-07-17):** the guard now also derives when the start resolution
failed and its source is not "extract" (a printed extract value below the bar is
still never overwritten); the advisory names the replaced value. Repro locked into
`tests/test_field_policy_gates.py` (FortisBC shape, exact captured confidences).

**Lesson:** "present" is not "trusted" -- a below-threshold generate-only value must
never block a deterministic derivation or rescue. When adding any future derived
field, gate the derivation on resolution success/source, not just emptiness.

---

## CU field-description ranges act as confidence suppressors; shared-year date ranges need an explicit rule

**Symptoms (verified 2026-07-17, scratch analyzer, 3 replicates x 6 docs):** two
prompt-level causes of low billing-period twin confidence.

1. The `number_of_days_generate` description said the value is "typically between
   25 and 130". The incident bill's correct value was 13 (a final bill) -- outside
   the prompt's own stated range -- and came back at 0.665. Fortisbc's 27 days sat
   at 0.609-0.653. After rewording to "usually 25 to 130, but a final or opening
   bill can legitimately cover fewer days (the printed count is correct even when
   small)", fortisbc days medians rose to 0.96-0.98 with the same correct values.
2. No date twin knew that in a shared-year range -- "Billing period: May 19 -
   May 31, 2026" -- the trailing year applies to both dates. The extract twins
   returned null ("no full calendar date") and the start generate twin
   range-collapsed onto the end date (the 260605_0006 incident). After adding the
   shared-year rule + an anti-collapse verify step ("the start must be strictly
   earlier than the end; if your candidate equals the end date you have collapsed
   the range"), fortisbc's start_extract went null -> correct 2026-05-01 on 3/3
   replicates.

**Lessons:**
- Never state a numeric "typical range" in a CU field description unless
  out-of-range values are truly invalid -- the model reads it as a validity bound
  and marks correct out-of-range values low-confidence.
- Date-range fields need the shared-year rule spelled out with a worked example.
- Confidence floors: even with correct stable values, twin confidences hop bands
  (~0.66 / 0.74 / 0.78 / 0.90+) run-to-run; median-of-3 on a scratch analyzer is
  the minimum honest measure, and ~0.74-band medians can persist on some docs
  (burnaby days) with values still correct -- resolution passes via twin agreement,
  so judge value correctness first, confidence second.

---

## `total_invoice_amount` captured the carried-forward account balance on statement-style bills

**Symptoms (verified 2026-07-20, `samples/bug_260605_0017.pdf`):** a Waste Management
(commercial) statement-style invoice with an account summary -- Previous Balance
1,352.93 + Current Invoice Charges 1,626.20 = Total Account Balance Due 2,979.13. The
pipeline wrote `total_invoice_amount = 2979.13` and the derived
`amount_excluding_gst = 2901.68`; the correct value is this bill's current charges,
1,626.20 (amount_excluding_gst 1,548.75). `total_invoice_amount` is base-critical and
routing was `HAPPY_PATH_CANDIDATE`, so the wrong value shipped with no review.

**Cause (prompt, both twins -- not a code bug):** the `total_invoice_amount_extract` /
`_generate` descriptions said "prefer values labeled ... Balance Due, Total Due ...".
On a bill that rolls a previous balance into the total, "Total Due" / "Total Account
Balance Due" is the account balance, not this bill's charges. Both twins obeyed and
agreed at high confidence (extract 0.82, generate 0.74), so `resolve_twin` correctly
accepted the value -- `field_policy.py`/`gates.py` behaved correctly given the inputs.
The confirming tell: GST 77.45 is 5% of the current-charges pre-tax
(1626.20 - 77.45 = 1548.75) but only 2.6% of 2979.13.

**Fix (verified 2026-07-20 on scratch analyzer `generalinvoicescratch`, prompt-only):**
both descriptions carry a scoped exception (all bill types): when a Previous Balance /
Balance Forward line and a separate Current Charges / Current Invoice Charges / Total
Current Charges / New Charges line are both present, and a larger Total / Balance Due
rolls them together, return this bill's current charges (tax-inclusive), never the
carried-forward total; the generate twin adds a
`previous + current (+/- payments/adjustments) == Total Due` arithmetic check. When no
previous balance is carried forward, Total / Balance Due already equals the current
charges (default path unchanged). Scratch results (3/3 replicates each, both twins):
`bug_260605_0017` (WM, Previous Balance + Current Invoice Charges) -> 1626.20;
`bug_260601_0018` (Waste Connections, **aging-bucket** CURRENT/30/60/90 + AMOUNT DUE
11809.25) -> 6207.81. The arithmetic check generalizes the fix past the literal
"Previous Balance"/"Current Charges" labels to aging tables (CURRENT vs AMOUNT DUE with
aged arrears) without naming that layout in the prompt. Regression sweep unchanged
across all 10 municipal baselines (`out/municipal-verify-baseline/*.json`) and 5
commercial/handwritten prod-vs-scratch A/B docs. Prod general-invoice analyzer push is
the follow-up (do with `scripts/create_analyzer.py`, then re-run `scripts/test.py`).

**Reusable lesson:** "prefer Balance Due / Total Due" is wrong for statement-style bills
that carry a prior balance -- the invoice's own amount is the *current charges*, and the
GST-should-be-~5%-of-pre-tax check exposes a total that swept in a previous balance.

---

## `service_address` empty on Waste Connections route ("site block") invoices

**Symptoms (verified 2026-07-21, `samples/bug_260601_0015.pdf`):** both service_address
twins returned empty on a Waste Connections commercial invoice, so it routed to
`REVIEW_B4_CRITICAL_FIELD` (service_address is base-critical). The served site
(1942 KINGSWAY, VANCOUVER BC) is printed inside the DETAILS / line-item section as a
two-line site header with **no** SHIP TO / Service Address / Service Location label:
`(0001) NOBLE<<1942 KINGSWAY>> SITEPO 33001163` / `1942 KINGSWAY, VANCOUVER BC CSA 1740`.
The only labelled address on the page is the customer mailing block (correctly excluded).

**Cause (prompt, both twins -- not a code bug):** the description's priority list only
knew labelled sources (SHIP TO, Service Address, Prepared For, ...). The site-block
header carries no such label, so both twins returned empty and `resolve_service_address`
correctly reported passed=false / source=none.

**Fix (verified 2026-07-21 on scratch `generalinvoicescratch`, prompt-only, both twins):**
added the private waste-hauler / route "site block" as a recognised source -- a
`(nnnn) <site> SITEPO/SC# <po>` header immediately followed by a street line ending in a
`CSA <code>` marker; return the street/city/province, dropping the parenthesised index,
the site name / `<<...>>` fragment, the SITEPO/SC# number, and the trailing `CSA <code>`.
Existing SHIP TO / Prepared For / Radius Group priorities and the SOLD TO / mailing
exclusions are untouched (additive). Scratch: `bug_260601_0015` ->
`1942 KINGSWAY, VANCOUVER BC` on 3/3 (both twins); `bug_260601_0018`
(`(0001) CONNAUGHT PLAZA SC#33000152`) -> `8500 ALEXANDRA ROAD, RICHMOND BC` (already
correct on prod, unchanged).

**Regression gotcha worth remembering:** a single-run diff vs the committed
`out/municipal-verify-baseline/*.json` flagged three docs -- richmond_water (baseline
`7171 NO. 5 RD\nRICHMOND BC...` vs `7171 NO. 5 RD`), business_license (baseline `null` vs
`3237 Matapan Crescent`), landscaping (address vs null). All three were **stale baselines
/ CU run-to-run noise, not the edit**: prod and scratch returned identical values across
3 replicates each. Reconfirms the standing lesson -- service_address extract values flap
run-to-run; judge regression by prod-vs-scratch replicates, never a single run against an
old baseline.

---

## `invoice_date` silently became "today" on runs where its confidence dipped

**Symptoms (verified 2026-07-23, `samples/bug_260615_0006.pdf`):** the bill prints
`Date: June 11, 2026`, but on some runs the pipeline wrote the *processing* date
instead. `invoice_date` was the last single-method (`extract`) field trusted on
confidence alone: `build_write_values` substituted today (PST) whenever the estimated
confidence fell under 0.73. The value was never wrong -- the confidence was noisy.
Measured on the same bytes across replicates: 0.752 / 0.804 / 0.902 / 0.964 / 0.971 on
this document, and prod runs of `bchydro` (0.752 / 0.872 / 0.955) and `surrey_water`
(0.741) sat a single noise band above the bar. Nothing routed to review, because
`invoice_date` is not a critical field.

**Fix (verified 2026-07-23 on scratch `generalinvoicescratch`, 24 replicates x 6 docs):**
make it an extract + generate twin like `vendor_name` -- `invoice_date_extract` (the old
description, unchanged) plus a step-by-step `invoice_date_generate` -- so the agreement
boost carries a sub-threshold-but-correct date. Every document that prints an issue date
returned it on 3/3 replicates with both twins agreeing: `bug_260615_0006` 2026-06-11,
`bchydro` 2026-06-09, `fortisbc` 2026-05-29 (generate as low as 0.731 -- the pair still
resolves), `bug_260601_0015` 2026-05-31, `260629_0001` 2026-05-25.

**The trap this exposed -- a generate twin will invent a date when the page has none.**
`samples/business_license.pdf` (a Vancouver licence renewal notice) prints only a
payment due date and a page footer `1/13/26 10:12AM`. The extract twin correctly
returned null on 3/3, but the generate twin answered `2026-01-13` **at 0.820** -- above
the bar, so it would have auto-written the print timestamp as the invoice date, with no
review. Tightening the prompt (an explicit "a date printed with a clock time in a
header, footer or margin is a print timestamp, never the invoice date" rule plus a
"renewal notice with only a due date has no issue date" case) only degraded it: the twin
still read the same footer span, now returning a garbled `2010-03-26` at 0.588.
The durable fix is code-side -- `field_policy.NO_GENERATE_RESCUE` -- : for
`invoice_date` a confident generate may no longer stand in for an *absent* extract, so
an ungrounded value is refused and the field defaults to today (recorded in
`defaultedFields`). Replaying the stored 0.820 run through the guard now yields the
default. Twin *agreement* is untouched, and every other twin keeps its generate rescue.

**Also shipped:** a written `invoice_date` after today (America/Vancouver) routes to
`REVIEW_B4_CRITICAL_FIELD` (`invoice_date needs attention`), with the extracted date
written unchanged so the reviewer sees what the document said. The check runs on the
written value, so a defaulted (today) date can never trip it.

**Reusable lessons:**
- A `generate` twin is only as trustworthy as the thing that grounds it. On documents
  where the field genuinely does not exist, the twin answers anyway -- confidently, from
  whatever date-shaped text is on the page. Prompt exclusions do not reliably stop it
  (here it kept reading the same span at lower confidence). When a field has a safe
  deterministic fallback, refuse the ungrounded value in code instead.
- Judge such a fix by *replaying the stored raw JSON* of the bad run through the new
  code -- it proves the guard on the exact response that failed, with no CU cost.
- A second, pre-existing bug surfaced while verifying this one: the `vendor_name` twins
  never agreed on the same document. See the next entry.

---

## `vendor_name` twins never agreed on a sole proprietor's shortened name

**Symptoms (verified 2026-07-23, `samples/bug_260615_0006.pdf`):** the bill routed to
`REVIEW_B4_CRITICAL_FIELD` (`vendor_name needs attention`) on 1 of 6 replicates, while
both twins returned the same vendor every single run: extract `SIMON SIK FAI KAN`
(0.721-0.963, one run under the 0.73 bar) and generate `SIMON KAN` (0.348-0.520). Only
the confidence moved; the values never did.

**Cause (code, not prompt):** `_vendor_values_consistent` accepted equality or *substring
containment* after normalisation. `simon kan` is not a contiguous substring of
`simon sik fai kan` -- the dropped words sit in the middle -- so the pair never agreed and
nothing corroborated the extract when its noisy confidence dipped. The generate twin was
behaving correctly: its prompt asks for the short common name, which for a sole
proprietor means dropping the middle names.

**Fix (two parts, code-only -- no analyzer change):**

1. Agreement gained a third form: one name's *words* being a subset of the other's.
   Checked against every vendor twin pair stored in `out/**` (16 distinct pairs, 62 runs):
   only the two `SIMON` pairs change verdict; `City of Richmond` vs `City of Vancouver`,
   `Great West Pool And Spa` vs `Great West Plumbing`, `SIMON SIK FAI KAN` vs `DANNY KAN`
   and `Noble & Associates` vs `Noble Homes` all stay non-agreeing.
2. The written spelling is no longer unconditionally the generate twin's. `resolve_twin`'s
   `prefer_generate_when_agree` boolean became a chooser callable
   (`prefer_generate_on_agree`); the vendor chooser `_prefer_vendor_generate` keeps the
   generate spelling when both twins carry the same name after normalisation -- casing or
   a legal suffix only -- and otherwise writes **whichever twin was more confident**, ties
   going to the normalised generate name. Every other twin passes `None` and keeps the
   literal extract value exactly as before.

**Measured effect** (replaying all 41 stored replicates through the new code): the six
`SIMON` runs all reach `HAPPY_PATH_CANDIDATE` writing `SIMON SIK FAI KAN` (source
`agreement` on the 0.721 run, `extract` on the rest); the 13 casing/suffix-only pairs
(the cities, BC Hydro, `PROTECH PEST CONTROL LTD.`, `WASTE CONNECTIONS OF CANADA INC.`)
are byte-identical to before, keeping the clean generate spelling even where the extract
is far more confident (surrey 0.982 vs 0.741 still writes `City of Surrey`). The one
genuine change beyond the bug: `FortisBC Energy Inc.` is written instead of `FortisBC`
on the ~1-2 runs in 8 where the extract twin was the more confident of the two -- the
intended consequence of the rule.

**Reusable lesson:** a name comparator built on substring containment silently fails on
*interior* omissions (middle names, dropped connective words). Token-subset comparison
covers both, and pairing it with a "who was more confident" value choice avoids the
follow-on question of which spelling to trust. Judge such a change by replaying the whole
stored corpus, not one document -- that is what showed the casing pairs were unaffected.

---

## `gst_amount` returned one section's GST on a bill that taxes each section separately

**Symptoms (verified 2026-07-23, `samples/bug_260624_0015.pdf`):** a BC Hydro bill that
charges GST twice -- `TAXES ON ACCOUNT CHARGES * GST 5% $0.68` and `TAXES ON ELECTRICITY
CHARGES * GST 5% on $49.72 $2.49` -- and prints the bill's total GST in a recap,
`TAX SUMMARY GST 5% on $63.22 $3.17`. On the same bytes, runs wrote 0.68, 2.49, or the
correct 3.17. Even the correct runs barely held: `gst_amount_extract` 3.17 @ **0.664** and
`gst_amount_generate` 3.17 @ **0.519** (`out/verify_result.json`), both under the 0.73
bar, so the pair passed on twin agreement alone. `gst_amount` is critical for the
commercial bucket only, so on this municipal bill a wrong value ships
`HAPPY_PATH_CANDIDATE` -- with a wrong derived `amount_excluding_gst` -- and nothing
routes to review.

**Cause (prompt, both twins -- not a code bug):** both descriptions said only "prefer the
amount directly labeled GST, G.S.T., GST@5.0%, GST 5%". Three lines on this bill match
that equally well and none is marked as the document-level one, so the choice was left to
run-to-run chance. The trailing exclusion made it worse: *"Use Total Tax only when there
is no separate GST line"* steers the model **away** from the TAX SUMMARY recap precisely
*because* per-section GST lines exist. `field_policy.resolve_twin` behaved correctly given
those inputs.

**Fix (verified 2026-07-23 on scratch `generalinvoicescratch`, prompt-only, both twins):**
when a GST amount is printed under more than one charge section, the answer is the
bill-level GST from the tax recap (TAX SUMMARY / Sales Tax Summary / Total GST / Total
Taxes), never a single section's line; on such a bill with no recap printed, the extract
twin returns null (it has no span for a sum) and the `generate` twin -- rewritten as a
numbered procedure -- adds the section amounts (0.68 + 2.49 = 3.17). Its verify step names
the failure mode outright: an answer much smaller than ~5% of the bill's pre-tax amount
means one section's GST was returned. The Total-Tax exclusion is amended so a recap that
itemises GST on its own line counts as a GST line; a *combined* Total Tax is still refused.

Measured, 3 replicates per document per analyzer, both twins (prod `generalinvoice` vs
scratch):

| document | prod | scratch |
|---|---|---|
| `bug_260624_0015` | 3.17 x3; extract 0.586-0.820 / generate **0.457-0.464** | 3.17 x3; extract 0.817-0.955 / generate **0.663-0.902** |
| `bchydro` (one GST line, no recap) | 22.02 | 22.02 |
| `fortisbc` (prints combined `Total energy taxes & fees 11.69`) | 10.82 | 10.82 |
| `pest_control` (`Sales Tax Summary / GST@5.0%`) | 19.75 | 19.75 |
| `260105_0007` (handwritten split dollars/cents) | 4.50 | 4.50 |
| `trade8` (GST + PST) | 24.90, PST 11.90 | 24.90, PST 11.90 |
| `trade1` (PST line printed N/A) | 3.75, PST 0 | 3.75, PST 0 |

**Honest limit of the evidence:** the bad values did not reproduce on prod during
verification (3/3 correct), so the fix is judged on confidence, not on a value flip caught
in the act. The tell is the generate twin pinned at ~0.46 on prod -- the signature of
picking between equally-labelled candidates -- rising to 0.66-0.90, and the extract twin
now clearing the bar on its own instead of the pair scraping through on agreement.

**Reusable lessons:**
- When a document can print the same label more than once (per-section tax lines,
  per-site subtotals), "prefer the amount labeled X" is underdetermined and the model
  picks a different occurrence run to run. Name which occurrence covers the whole
  document and name the recap block that carries it.
- Before adding wording, check whether an existing *exclusion* is what blocks the right
  answer -- here "use Total Tax only when there is no separate GST line" was actively
  pushing the model off the correct recap line.
- A twin pair stuck in a low confidence band on a field that is otherwise easy is a
  symptom of an ambiguous prompt, not of a hard document.
