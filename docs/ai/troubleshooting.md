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
