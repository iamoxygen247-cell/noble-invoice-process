# Troubleshooting

Confirmed, verified environment/debugging lessons for this project. Keep entries
reusable and secret-free; machine-specific paths and account values belong in
`CLAUDE.local.md` (gitignored).

---

## Verifying analyzer / prompt changes: the regression safety net

**Why it exists:** every bug in `analyzers/create-generalinvoice-analyzer.json`
shipped because the file had no automated coverage — the whole test suite passed
without ever opening it, and "correct" was judged by eyeballing one stochastic run.
See the deposit/balance-forward entry below for the session that motivated this.

Two tiers:

- **Tier A — `tests/test_analyzer_contract.py`** (offline, ~1s, part of `pytest`).
  Asserts the analyzer JSON and the code agree: the field set equals
  `gates.FIELD_PRINT_ORDER` minus the computed finals, every `field_policy` twin
  constant resolves to a field of the right `method`, money/date `type`s are right,
  classify enums match `field_policy`, generate-step numbering is `1..N`, and the
  router routes `general_invoice` to `cu_client.DEFAULT_GENERAL_ANALYZER_ID`. A
  renamed/dropped field, a wrong type, or a mis-pointed router fails here for free.

- **Tier B — `scripts/regress.py`** (golden corpus). Provisions the working-tree
  definition as throwaway id `generalinvoicetest`, runs each PDF in
  `tests/pre-commit-test/` through the real `gates.evaluate`, and compares against a
  per-doc `<stem>.expected.json`. Each (doc, field) is **OK / WRONG / UNSTABLE**
  (replicates disagreeing → UNSTABLE, which is how a coin-flip doc is told apart
  from a clean regression). Non-zero exit on any WRONG/UNSTABLE.

**The corpus is gitignored** (`tests/pre-commit-test/`) — real customer invoices,
same as `samples/`. A missing folder is a clean skip, so a fresh clone still passes.
Every future bug-fix PDF goes in this folder with a sidecar asserting the field the
bug was about.

**Content-addressed cache is why Tier B can run on every commit.** Key is
`(analyzer-hash, pdf-hash, replicate)` under `out/regress-cache/`. When neither the
prompt nor the PDFs changed, a run makes **zero CU calls** and re-scores cached raw
JSON in ~1s; a `gates.py`/`field_policy.py` change also re-scores for free. Real CU
calls happen only when the analyzer definition actually changes.

Usage (PowerShell, from repo root):

```powershell
# set AZURE_CU_ENDPOINT + AZURE_CU_KEY first, or pass --load-local-settings
.\.venv\Scripts\python.exe scripts\regress.py                      # score the corpus
.\.venv\Scripts\python.exe scripts\regress.py --replicates 5       # stricter stability check
.\.venv\Scripts\python.exe scripts\regress.py --update-expected <stem>   # scaffold a sidecar to review
.\.venv\Scripts\python.exe scripts\regress.py --analyzer-file <path>     # A/B a control built from git show
```

**Adding a doc after a bug fix (the one command):**

```powershell
.\.venv\Scripts\python.exe scripts\regress.py --add ".\samples\<bug>.pdf" --load-local-settings
```

`--add` copies the PDF into `tests/pre-commit-test/` and scaffolds its
`<stem>.expected.json` from a few replicates, listing any UNSTABLE field it left out.
It is the only step needed beyond having the PDF — a PDF with **no sidecar is silently
skipped** (`no sidecar (not scored)`), so the copy alone buys nothing.

**Then trim.** Open the sidecar, **delete every value you have not verified against the
PDF, and keep the field the bug was about.** A sidecar is a hard assertion — an
unverified value becomes a false red later. `--update-expected <stem>` re-scaffolds a
doc already in the folder. (When the corpus was seeded, one stale baseline value —
`business_license` routing — was caught by the very first run and corrected; that is the
mechanism working, not a one-off.)

**When these run:**

- `pytest` → Tier A + the offline suite.
- **every `git commit`** → the `scripts/hooks/pre-commit` hook runs `pytest`, then
  `regress.py`. Install it with `scripts/hooks/install.ps1` (`.git/hooks` is not
  versioned). `NOBLE_SKIP_REGRESS=1` skips only Tier B, loudly — never for a prompt
  change.
- **prod analyzer push** → `scripts/create_analyzer.py` refuses to push the prod
  `generalinvoice` id unless a green regression stamp exists for the current clean
  HEAD (`out/regress/<sha>.json`, written by a green `regress.py` run). `--force`
  overrides. Scratch/test/router pushes are never gated.

So the prod-push flow is: commit → `regress.py` (green, writes the stamp) →
`create_analyzer.py`.

---

## A scratch analyzer is NOT reachable by setting `AZURE_CU_GENERAL_ANALYZER_ID`

**Symptom (verified 2026-07-23):** pushed an edited prompt to `generalinvoicescratch`, set
`AZURE_CU_GENERAL_ANALYZER_ID=generalinvoicescratch` in `functionapp/local.settings.json`,
restarted the host, and ran `scripts/test.py`. The run returned a *plausible* result --
but from the **prod** analyzer. The edited prompt was never exercised.

**Cause:** the child analyzer is chosen by the **router**, not by that env var.
`analyzers/create-router-analyzer.json` hardwires
`config.contentCategories.general_invoice.analyzerId = "generalinvoice"`, so CU always
runs the prod child. `AZURE_CU_GENERAL_ANALYZER_ID` is only used by
`gates.find_child_content()` to pick which content block to *read* from the router's
result. With it set to a scratch id the first match fails and the function falls through
to `matched category == general_invoice and fields present` -- i.e. the prod output.

**Tell:** `childSelection` in the decision JSON. `matched analyzerId == <your scratch id>`
means you really hit the scratch analyzer; `matched category == general_invoice ...` means
you silently read prod.

**How to verify a prompt change:** call the analyzer directly (no router, no function
host) and read the raw twins -- this is what the stored `out/scratch-*/*.json` raw results
are. Mirror `cu_client.analyze_binary` with `begin_analyze_binary(analyzer_id=...)`
against the scratch id, then optionally replay the saved raw JSON through the real
decision path offline with `scripts/diag.py --replay <raw.json>` to check `writeValues`.
Routing through the function only matters when the *code* changed. If you do need the
function end-to-end on a scratch prompt, you must also provision a scratch **router**
pointing at the scratch child and set `AZURE_CU_ANALYZER_ID` to it.

---

## `scripts/create_analyzer.py` fails with `CERTIFICATE_VERIFY_FAILED` behind TLS inspection

**Symptom (verified 2026-07-23):**
`CU provisioning failed: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
Basic Constraints of CA cert not marked critical`. Same root cause as the `az` entry
below, now hitting the project's own scripts. Clearing `REQUESTS_CA_BUNDLE` does **not**
help -- verification still fails.

**Fix:** inject `truststore` (already in the root `.venv`) so Python verifies against the
Windows cert store, without editing any project file:

```powershell
# scratch wrapper: injects truststore, then runs the target script
#   import truststore; truststore.inject_into_ssl()
#   runpy.run_path(sys.argv[1], run_name="__main__")
.\.venv\Scripts\python.exe <wrapper>.py scripts\create_analyzer.py --analyzer-id generalinvoicescratch
```

A probe confirming the three cases: default -> FAIL, `REQUESTS_CA_BUNDLE` removed ->
FAIL, `truststore.inject_into_ssl()` -> HTTP 200. For a locally started `func` host, the
equivalent is a `sitecustomize.py` on `PYTHONPATH` doing the same injection.

**Also note:** `func start` picks its Python worker off `PATH`, so a host launched from a
shell without the root `.venv` activated grabs global Python (3.14 here) and dies at
import with `ZoneInfoNotFoundError: 'America/Vancouver'` -- `tzdata` is installed in the
root `.venv` only. Set `languageWorkers__python__defaultExecutablePath` to
`.venv\Scripts\python.exe` when starting the host non-interactively.

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
- `func azure functionapp publish` and other .NET tools work fine **for their own HTTPS
  traffic** — but see the deploy note below: `func` shells out to `az` for auth, and that
  child process is not covered by the PowerShell profile wrapper.

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

**Follow-up 2026-07-23 -- the label-matching version of this fix was too fragile.** See
the next entry: the "Previous Balance line + Current Charges line + larger Total" shape
match fired on a bill with `BALANCE FORWARD $0.00` (returning a smaller Current-charges
box that excluded a security deposit) and missed a bill whose own charges are labelled
`ELECTRICITY CHARGES SUBTOTAL` rather than "Current Charges". Both twins were also
unstable on `bug_260605_0017` itself -- a control analyzer built from this version of the
prompt returned 1626.20, 2979.13, 1626.20 over three replicates, so the "3/3 verified"
above was a lucky run. Replaced by the subtraction rule below.

---

## `total_invoice_amount` mis-split on bills with a security deposit / a balance forward

**Symptoms (verified 2026-07-23):** two BC Hydro bills, opposite failures, same prompt.

- `samples/bug_260629_0026.pdf` (new security deposit): highlights box prints
  `Security deposit $177.00` / `Current charges $21.14` / `Total due $198.14`, with
  `BALANCE FORWARD $0.00`. The pipeline wrote `total_invoice_amount = 21.14`
  (`amount_excluding_gst` 20.13); correct is **198.14** -- the deposit is billed on this
  bill and payable on its due date, and the vendor's "Current charges" box excludes it.
- `samples/bug_260609_0031.pdf` (final bill): `BALANCE FORWARD $24.32` +
  `ELECTRICITY CHARGES SUBTOTAL $11.37` = `TOTAL DUE $35.69`. The correct value is
  **11.37**; the bill's own charges are not labelled "Current Charges" anywhere.

Both routed `HAPPY_PATH_CANDIDATE` with both twins agreeing, so the wrong values shipped
with no review. The deposit bill *flips run to run* (one local run correct, the next
wrong; twin confidences 0.61/0.69), so a single passing run proves nothing here.

**Cause:** the label-matching exception from the previous entry. On the deposit bill the
surface shape matched (a Balance Forward line exists, a Current charges box exists, Total
due is larger) even though the balance forward was **0.00**; on the final bill nothing
matched the Current-Charges label list. Note the GST cross-check does **not** catch the
deposit case and actively argues for the wrong answer: GST 1.01 is exactly 5% of 20.13,
and 20.13 + 1.01 = 21.14, because the $177.00 deposit is not taxed.

**Fix (verified 2026-07-23 on `generalinvoicescratch`, prompt-only, both twins):** state
the business rule as subtraction instead of label matching --
`this bill's amount = Total Due - amount carried forward`, treating any prior amount as
already paid. Both descriptions now (a) read the carried-forward amount *first*, (b) use
the amount actually carried forward rather than a settled `Previous bill` line
(`samples/bchydro.pdf` prints `Previous bill $627.90` with `BALANCE FORWARD $0.00` --
nothing is carried), (c) when non-zero, return the Current-Charges figure *if printed,
otherwise the bill's own tax-inclusive charges subtotal*, and (d) when zero/absent,
return the Total Due even if a smaller Current-charges box is printed. The extract twin
additionally had its "prefer values labeled ... Balance Due, Total Due" opener demoted
below the carried-forward rule -- while that opener came first, the extract twin returned
2979.13 on `bug_260605_0017` 3/3 even with the correct rule present later in the text.

Scratch results, both twins, all stable: `bug_260629_0026` -> 198.14 (5/5);
`bug_260609_0031` -> 11.37 (3/3); `bug_260605_0017` -> 1626.20 (3/3);
`bug_260601_0018` -> 6207.81 (3/3). All 10 municipal baselines unchanged
(`out/municipal-verify-baseline/*.json`), and 5 commercial/handwritten docs identical in
a control-vs-fix A/B. Replaying the raw results through `scripts/diag.py --replay`
confirms the write path: 198.14/197.13, 11.37/10.83, 1626.20/1548.75. `POLICY_VERSION`
unchanged (prompt change, not a resolution-rule change). Prod analyzer push is the
follow-up (`scripts/create_analyzer.py`).

**Reusable lessons:**

1. Encode the *business rule* (subtract what was carried forward), not the *layout* every
   vendor happens to print. Label lists fail in both directions -- false positives on a
   0.00 balance forward, false negatives on a vendor-specific subtotal name.
2. The GST-is-~5% tell only works when every charge is taxable. Deposits and other
   refundable amounts carry no GST, so that check will confirm a total that is missing
   them.
3. In an extract-method description, order is priority: a "prefer X" opener beats a
   correction stated later in the same text. Fix the opener, not just the exception.
4. Verify prompt fixes on a control analyzer built from `git show HEAD:<file>` as well as
   the edited one. Without the control, a doc that was *already* flipping looks like a
   regression your change caused (or a pass your change earned).

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

## `service_address` intermittently null on an `Attention:` block -> spurious review

**Symptoms (quantified 2026-07-24 by pooling 180 cached corpus runs -- 12 per doc across 4
analyzer versions; the service_address prompt was identical in all of them):**

| doc | runs | extract null | routed to review | distinct written values |
|---|---|---|---|---|
| `bug_260615_0006` | 12 | **4** | **4** | 2 |
| `bug_260605_0017` | 12 | 3 | 2 | 2 |
| `business_license` | 12 | 1 | 1 | 2 |
| `bug_260601_0018` | 12 | 1 | 0 | 1 (generate twin rescued it) |
| other 11 docs | 132 | 0 | 0 | 1 each |

Seven of 180 runs (~4%) sent a clean, correctly-read invoice to `REVIEW_B4_CRITICAL_FIELD`,
concentrated almost entirely in one document: `bug_260615_0006` failed **1 run in 3**, and
one three-replicate cluster failed all three, so it is not an isolated blip.

**Cause (prompt, both twins).** The address on that invoice sits under `Attention:`:

```
# SIMON SIK FAI KAN  ## INVOICE  778 237 8293 ... 4270 Salish Drive Vancouver BC   <- vendor's own block
Attention: 6240 Cooney Road, Richmond, BC Canada                                    <- the serviced property
```

`Attention` was in none of the prompt's priority tiers (SHIP TO, Service Address, Site
Address, Prepared For, ...), *and* the same prompt excludes "customer mailing address ... or
an unlabeled mailing block" -- which is exactly what an `Attention:` block resembles. The
model was pulled both ways on identical bytes and, on the runs where it decided "mailing
block", returned nothing at all. Nothing was misread; the twins simply declined to answer,
and review routing absorbed it -- the same failure family as the `invoice_date`-to-today and
`invoice_number`-to-filename bugs.

Two lesser variants on other docs: `bug_260605_0017` kept or dropped a leading
`2993 Granville Limited Partnership, ` (the partnership name repeats the street number, so
the "drop the customer name line" rule read as ambiguous), and `business_license` relies on
the loose "for municipal bills, the assessed or billed property address" tier.

**Fix (prompt-only, both twins), verified on scratch `generalinvoicescratch` at 6
replicates x 6 documents:**

1. A **tier-4 `Attention` / `Attn` fallback**, deliberately scoped to fire *only when none
   of the tier 1-3 labels appears anywhere on the document*, so it can never outrank a real
   SHIP TO block, plus an explicit carve-out for the vendor's own contact block and for an
   Attention line carrying only a person or department name.
2. The drop-the-name-line rule extended to a name that **begins with digits repeating the
   street number**, with the Granville example worked through.
3. A single **address-parts normalisation** rule in both twins: return exactly street
   (with unit), city, province/state, and postal code if printed -- never a country line,
   customer/company name, `c/o` line, attention person, or property manager.

Round 1 (rules 1-2) eliminated the failure completely -- **0 nulls in 36 runs**, versus 8
nulls in the equivalent 36 cached runs -- and `bug_260605_0017` became 6/6 identical. It
also surfaced why rule 3 was needed: two docs returned two spellings each, differing only by
a trailing `Canada` line (`bug_260615_0006`, 5/6 vs 1/6) or a prepended
`NOBLE & ASSOCIATES REALTY LTD` (`bchydro`, 1/6 -- a control that had been 12/12 stable, so
the elaboration in rule 2 had made the general "drop the name line" instruction read as
conditional). Controls held throughout: the waste-hauler site block (`bug_260601_0018`) and
`FOR SERVICE AT` (`burnaby_water`) stayed 6/6 correct.

Round 2 (rule 3 added) settled both: `bug_260615_0006` -> `6240 Cooney Road, Richmond, BC`
on **6/6 identical**, and `bchydro` back to the clean form on 6/6. `bug_260605_0017` still
shows two spellings, but they now differ only by a newline vs a space before the postal code
(`...Vancouver Bc
V6h 3j6` when the extract twin carries it, `...Vancouver Bc V6h 3j6` on
the runs where the extract returns nothing and the generate twin rescues it). Both are
correct, neither routes to review, and collapsing whitespace is not an option here the way
it was for `vendor_name` -- service addresses are legitimately multi-line. That doc's
`service_address` therefore stays out of the corpus assertions, with the reason in its
`note`.

**Reusable lessons:**
- An unrecognised *label* is as damaging as a misread value, and it fails silently: the twin
  returns nothing rather than something wrong, so only a review-rate or a replicated corpus
  reveals it.
- Elaborating a special case can weaken the general rule it hangs off -- spelling out "drop
  the name line even when it repeats the street number" made a control start *including* a
  name line. State the general normalisation explicitly rather than relying on it being
  implied by examples.
- A 1-in-3 failure needs 6+ replicates to see reliably; the standard 3 would have shown all
  green by luck about 30% of the time.

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

**Follow-up 2026-07-24 -- the blanket refusal was too blunt and silently re-caused the
original bug.** Once the corpus asserted `invoice_date` on every doc, three of fifteen --
`abbotsford_water`, `bug_260609_0031`, `bug_260629_0026` -- turned out to flip to today on
1 of 3 replicates, with exactly this shape:

```
abbotsford r2: ext=None@0.904  gen='2026-05-26'@0.977 -> 2026-07-24   (REFUSED)
```

The extract twin intermittently returns nothing on bills that plainly print their date, and
the refusal then threw away a correct, high-confidence value. The OCR text separates the
good case from the business_license case cleanly: those three print `BILLING DATE:
May 26, 2026` (month-name form), while business_license's only candidate is
`<!-- PageFooter: 1/13/26 10:12AM -->` (slashed, in a footer). So the refusal now has an
escape hatch -- `field_policy.date_corroborated_in_text` + a rescue in `gates.evaluate`
(source `"corroborated"`): **an ungrounded generate value is refused unless the same
calendar day is printed in the document text in an unambiguous month-name or ISO form.**
Slashed forms never corroborate -- they are ambiguous, and the observed false positive is
always printed that way. Simulated over all 45 cached replicates before implementing: it
repairs exactly those three docs, leaves business_license defaulting, and changes nothing
on the other eleven.

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

**Corpus anchor (added 2026-07-24, after the fix commit):** `bug_260615_0006.pdf` is in
`tests/pre-commit-test/`, asserting `invoice_date` `2026-06-11`. Every sidecar now asserts
the resolved `invoice_date`; none assert its twins, because the extract twin returns null
on roughly 1 run in 3-6 corpus-wide (see the corpus-coverage note in the `invoice_number`
entry below).
`payment_due_date` is asserted **only on bills that actually print one** (13 sidecars as of
2026-07-31). On a bill that prints no due date (`Terms: 30 Days`) it correctly defaults to
today+30 and would go red the next day -- yet the `--add` scaffold still proposes it, because
a today-relative default is stable *within* one session. That is the trap to watch for on any
bill lacking a printed due date; check the raw field is non-null before asserting it.

---

## `payment_due_date`: a correctly-read printed date discarded over a confidence dip

**Symptoms (verified 2026-07-31, corpus-wide):** the same failure class as `invoice_date`
above, on the sibling field, and it survived that fix. `payment_due_date` is a lone `extract`
-- no twin, so no agreement boost -- and `build_write_values` substitutes **today + 30**
whenever confidence falls under 0.73. CU's confidence on this field jitters right across that
bar while the *value never changes*:

```
fortisbc          2026-06-20 at 0.677 | 0.721 | 0.722 | 0.723 | ... | 0.979
business_license  2025-12-31 at 0.588 ... 0.742 | 0.884 | 0.901 | 0.959
bug_260528_0016   2026-06-16 at 0.721 | 0.803 | 0.813 | 0.900 | 0.946 | 0.973
```

**Blast radius, measured over 1,204 cached corpus runs:** 996 not defaulted; 182 defaulted
because CU returned `None` (**correct** -- no due date printed); **26 defaulted despite a
correct, printed date**. Eight of the nineteen documents that print a due date are affected,
at 2%-19% of runs.

**Why it mattered.** All 26 routed `HAPPY_PATH_CANDIDATE` -- `payment_due_date` is not
critical, so a defaulted value never reaches a reviewer and the fabricated date is written
straight through. It is recorded in the ledger's `DefaultedFields` column, but the Power
Automate design only branches on `defaultedFields` for `invoice_number`. And the error is
*systematic, not random*: BC utility terms run ~22 days, so a +30 default lands about **8 days
past** the real due date on every affected utility bill. `business_license` is the worst case
-- its due date is already in the past, so the default masked an overdue renewal as
not-yet-due.

**Fix (code-only, `gates.evaluate`):** reuse `field_policy.date_corroborated_in_text` -- the
same grounding rule as the `invoice_date` rescue. Keep the sub-threshold read when the same
calendar day is printed in the OCR text in an unambiguous month-name or ISO form
(`Due Tuesday, Jun 16, 2026`); slashed forms never corroborate. This is the *safer* direction
of that precedent: there it grounds a `generate` value the model can invent outright, here the
value is an `extract` with a span behind it and corroboration is a second check on top.
Deliberately **not** in `build_write_values`, which stays a pure function of `parsed` with no
OCR access -- the rescue layers on in gates, exactly as the invoice-date one does.

**Non-regression is structural, not statistical.** `date_corroborated_in_text(None, ...)`
returns `None`, so the 182 legitimate "no due date printed" defaults cannot be touched. Of
1,204 runs the change moves exactly the 26, with **zero** runs in the ambiguous "value present
but not corroborated" bucket. No confidence floor was added: the observed minimum is 0.588 and
any floor would be an invented number -- corroboration is the real guard.

**The corpus was the loudest symptom.** `payment_due_date` had been unasserted from three
sidecars on three separate occasions (`bug_260609_0031` 07-24, `bug_260605_0017` and
`bug_260528_0016` 07-31) because the assertion kept going red, while four more sidecars still
asserted it and were latent reds. One unfixed code behaviour was quietly eroding the net, and
each recurrence looked like a fresh mystery. Three assertions were **restored** with the fix;
`bug_260609_0031` stays unasserted because it returns a genuine `None` on 4/53 runs, which no
rescue can fix. `bug_260528_0016` r0 and `business_license` r2 *are* the sub-bar replicates, so
those two restored assertions are the end-to-end test of the rescue -- if it breaks, they go red.

**Lesson:** when a sidecar assertion is removed for flakiness, record the *mechanism*, then ask
whether the mechanism is a bug. Twice the note blamed "the extract returns null" when the
cached evidence showed a present value with a sub-threshold confidence -- a different fault
with a different fix.

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

**Corpus anchor (added 2026-07-24, after the fix commit):** `bug_260615_0006.pdf` asserts
the resolved `vendor_name` = `SIMON SIK FAI KAN` and `vendor_name_extract`.
`vendor_name_generate` is deliberately left out:
re-running it on 2026-07-24 returned `['Simon Kan', 'SIMON SIK FAI KAN', 'SIMON SIK FAI KAN']`
across three replicates -- the twin flips between the short and the full name, where in the
2026-07-23 session it had returned the short form 7/7. The *resolved* value stayed
`SIMON SIK FAI KAN` on every run either way, by both paths: equality when the twin returns
the full name, and the more-confident-spelling rule when it shortens. Good illustration of
the standing rule -- assert the resolved value, never the raw generate twin.

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

**Follow-up the same day -- the mirror-image failure: `gst_amount` null when no row says
"GST".** Two more production bills were reported (`samples/bug_260629_0025.pdf`,
`samples/bug_260629_0032.pdf`), both writing null.

* `bug_260629_0032` (FortisBC, prints `GST (5% of ' amounts) $1.17`) was a run-to-run
  flap, not a layout gap: 1.17 on 3/3 prod and 3/3 scratch when re-measured.
* `bug_260629_0025` (College Class Services, landscaping) was real and **not** covered by
  the sectioned-bill fix. Nothing on the page is labeled GST except the registration
  number `GST# 122990112RT`; the totals block reads `sub Total $796.00 / Tax $39.80 /
  Total $835.80`. Both prompts only knew how to find rows *labeled* GST, so they returned
  null -- scratch 0/3, prod 1/3 (the one prod hit came back at 0.963, so it was a coin
  flip, not a hard miss). `gst_amount` is critical for the commercial bucket, so this one
  routed to review rather than shipping bad data.

Amendment to both twins: when no row anywhere is labeled GST and the totals block shows a
single generically labeled tax row (Tax, Taxes, Sales Tax, Tax amount), that row IS the
GST when its amount is about 5% of the subtotal -- the printed GST registration number
corroborates that the vendor charges GST -- and is explicitly NOT returned when the single
amount is about 12% of the subtotal (GST and PST combined). The registration number itself
stays excluded.

After the amendment, 3 replicates per document on scratch, both twins: `bug_260629_0025`
-> **39.80 3/3** (was 0/3), `bug_260629_0032` -> 1.17 3/3, and the whole earlier
regression set unchanged (`bug_260624_0015` 3.17, `bchydro` 22.02, `fortisbc` 10.82,
`pest_control` 19.75, `trade8` 24.90 / PST 11.90, `trade1` 3.75 / PST 0, `260105_0007`
4.50). Confidence still hops bands run-to-run on correct values (the `bug_260624_0015`
generate twin ranged 0.26-0.90 across this round with the value never moving) -- judge
values first, confidence second.

**Reusable lessons:**
- When a document can print the same label more than once (per-section tax lines,
  per-site subtotals), "prefer the amount labeled X" is underdetermined and the model
  picks a different occurrence run to run. Name which occurrence covers the whole
  document and name the recap block that carries it.
- The opposite gap costs just as much: a prompt built entirely on a label finds nothing
  when the vendor labels the row generically ("Tax"). Give such a field a
  label-independent fallback anchored on arithmetic (~5% of the subtotal) plus a
  disqualifier for the lookalike (~12% = GST + PST combined).
- Before adding wording, check whether an existing *exclusion* is what blocks the right
  answer -- here "use Total Tax only when there is no separate GST line" was actively
  pushing the model off the correct recap line.
- A twin pair stuck in a low confidence band on a field that is otherwise easy is a
  symptom of an ambiguous prompt, not of a hard document.

---

## `gst_amount`: the same bug again on a bill with no recap -- fixed in code, not the prompt

**Symptoms (measured 2026-07-31, `samples/bug_260528_0016.pdf`):** a FortisBC gas bill
that taxes each charge section separately -- `GST (5% of ' amounts) $0.90` under
`Gas charges`, `GST (5% of # amounts) $0.75` under `Other charges & adjustments` -- and
prints **no** bill-level tax recap. Correct `gst_amount` is 1.65; production wrote
**0.90**, so `amount_excluding_gst` shipped as 33.79 instead of 33.04. The bill is
municipal, where `gst_amount` is not critical, so it shipped `HAPPY_PATH_CANDIDATE` with
no review.

**Cause -- two layers, and the second is why the previous prompt fix could not help:**

1. *Both* twins returned the first section's line: `gst_amount_extract` 0.90 @ **0.517**
   and `gst_amount_generate` 0.90 @ **0.461** (`out/verify_scorecard.html`). The
   sectioned-bill branch added in `c24dad1` illustrates sections with BC Hydro's
   vocabulary (`TAXES ON ACCOUNT CHARGES`); FortisBC uses category headers and footnote
   symbols, and the branch did not trigger. The ~0.5 confidence band is the same
   "choosing between equally-labelled candidates" signature recorded above.
2. Because the twins **agree**, the pair passes on corroboration and
   `field_policy.resolve_twin` is doing exactly what it should. There is no
   twin-resolution change that fixes this, and no confidence change either: raising
   confidence on 0.90 only ships the wrong value harder, and lowering it cannot route a
   field that is not critical for its bucket.

**Fix (shipped 2026-07-31, code-only -- the analyzer is untouched, so no prod push):**
`gates.evaluate` reads the GST amounts printed in the OCR markdown
(`field_policy.find_gst_line_amounts`) and, on **municipal** bills only, resolves them
(`resolve_sectioned_gst`): with two or more amounts, the answer is the printed recap --
the one amount equal to the sum of the others -- or, with no recap, their sum. It is
written only when it satisfies the bill's arithmetic (`gst_consistent_with_total`:
GST ≈ 5% of `total − gst − pst`) **and** the value CU resolved does not. Source
`"sectioned_sum"`, confidence 1.0, plus an advisory; `amount_excluding_gst` is
recomputed.

**Why the guards are shaped the way they are (all measured, not assumed):**

| document | bucket | residual vs the 5% identity | why it must not fire |
|---|---|---|---|
| `bug_260528_0016` (the bug), correct 1.65 | municipal | 0.1% | *must* fire |
| same bill, wrong 0.90 | municipal | 88% | the value being replaced |
| `fortisbc` 228.08 / 10.82 | municipal | **0.40%** | the BC clean energy levy is charged but NOT GST-taxable -- a systematic miss on every FortisBC bill, so the tolerance must admit it |
| `trade11` 222.88 / 9.95 | commercial | **7.0%** | an admin fee printed "incl. 5% GST" -- must stay outside |
| `bug_260629_0026` 198.14 / 1.01 | municipal | **876%** | an untaxed $177 security deposit |

Hence the tolerance `max(2 cents, 1% of expected)` -- it sits in the real gap between
≤0.4% (correct values, rounding + untaxed levy) and ≥7% (everything else). **Do not
widen it**; the tests assert both `fortisbc` and `trade11` so a later widening fails
loudly.

`bug_260629_0026` is the one landmine still in scope, and note *why* it is safe: it does
**not** have a single GST line (an earlier reading of this was wrong -- it prints three:
0.68, 0.33, and a recap 1.01). It is declined because the recap branch identifies
1.01 = 0.68 + 0.33, which is what CU already resolved. Its sidecar asserts
`gst_amount 1.01` / `amount_excluding_gst 197.13`, so `regress.py` is the tripwire.

**Measured blast radius:** across all 22 corpus documents x 5 replicates the rule fires
on exactly one document (`bug_260528_0016` -> 1.65, 5/5), and `find_gst_line_amounts`
returns an identical result on every replicate of every document. `pytest` 96 passed;
`regress.py` 387/387 expectations OK with 0 CU calls.

**Corpus:** both `bug_260528_0016` (no recap) and `bug_260624_0015` (recap printed) were
added. The latter motivated the 2026-07-23 prompt fix but had never been added, so the
recap branch had no coverage at all until now.

**Reusable lessons:**
- When **both** twins agree on a wrong value at ~0.5 confidence, the prompt is
  underdetermined for that document shape and no resolution-rule or threshold change can
  reach it. That is the point to stop layering prompt wording (this would have been the
  third layer on a ~450-word description) and derive the value deterministically.
- Confidence is not a fix for a wrong value. Check *which gate is actually failing*
  before tuning confidence -- here nothing was failing, and the field was not even
  critical for its bucket.
- Pair a text-derived value with an independent arithmetic check. Either alone is
  unsafe: text alone would sum a multi-invoice statement, arithmetic alone cannot
  propose a value.
- Scoping a rule to the bucket where the pattern actually occurs (municipal utilities)
  removed two of the three known landmines for free.
- Read the real cached OCR markdown before writing any text rule. CU renders the same
  vendor's bill as an HTML table on one document and as flat lines on another, and the
  amount lives in a *different cell* from its label -- a line-based regex written from
  the PDF's visual layout would have failed on both.

---

## `invoice_number` on water / gas bills: the filename IS the answer

**Business rule (confirmed by the product owner, 2026-07-24):** water, sewer, and
metered-utility bills, and natural gas bills such as FortisBC, **do not have an invoice
number**. The SharePoint filename is the intended value, supplied by the municipal
filename fallback in `gates.evaluate`. A city business licence / permit renewal notice
*does* have one -- its licence number -- and a BC Hydro electricity bill prints a real
invoice number. Both of those were already correct.

**Symptom (found by the corpus):** `burnaby_water`, `richmond_water` and `vancouver_water`
came back UNSTABLE, flipping between the filename and a short number printed beside the
bill's title:

```
burnaby r0: ext='1243'@0.974  gen='1243'@0.829  -> '1243'            (wrong)
        r1: ext=None@0.782    gen=None@0.782    -> 'burnaby_water'   (correct)
```

That number -- the `1243` in `# UTILITY NOTICE 1243`, the `11802` in `# UTILITY BILL
11802`, the `1740` / `12339` after a `Metered Utility Bill` heading -- is a **notice or
form number, not an invoice number**. The twins picked it up on roughly 1 run in 3, and
because `invoice_number` is critical for the municipal bucket the bill then carried a wrong
identifier while still routing `HAPPY_PATH_CANDIDATE`.

**Wrong turn worth recording.** The first attempt read the layout and concluded the
opposite: that these bills *do* print a bill number and the filename fallback was masking
it. That change made all four docs return the notice number -- a regression, caught only
because the product owner recognised it. The OCR layout genuinely looks like an invoice
number (unlabeled digits, right after the title, distinct from the ACCT NUMBER row); the
document cannot tell you it is not one. **This was a business rule, not something derivable
from the page, and it should have been asked rather than inferred.**

**Fix (prompt-only, both twins):** keep the genre rule from `4d424ea` and make it decisive
rather than hedged -- "usually has only a customer account number" became "have only a
customer account number and NO invoice number: return null/empty" -- plus an explicit trap
warning naming the number beside the title as a notice/form number that must never be
returned. BC Hydro and the licence-number path are called out so they stay unaffected.

**Reusable lessons:**
- A fallback that substitutes a plausible value (filename, today's date) hides the failure
  it compensates for. Both invoice-number-to-filename and invoice-date-to-today were
  invisible in production and surfaced only when the corpus asserted the field across
  replicates. Assert the fields the fallbacks protect.
- When a corpus assertion and the model disagree, the assertion may be encoding a *business
  rule* the document does not state. Reading the layout harder cannot settle that -- ask.
- A hedged prompt claim ("usually has no invoice number") leaves the model free to disagree
  whenever the page looks otherwise. If the rule is absolute, say so, and name the specific
  lookalike to reject.

**Corpus coverage (2026-07-24): 273 assertions across 15 docs.** Every sidecar
(`tests/pre-commit-test/<stem>.expected.json`) asserts the **complete `writeValues` set**
the Function produces -- all of `field_policy.WRITE_FIELDS` plus the derived
`amount_excluding_gst` -- not just the critical fields. Stably-null values are asserted
too: `"gst_amount": null` on a water bill is a real assertion ("this bill charges no
GST"), and `"po_or_job_number": null` pins that a municipal bill carries none.

A key is left out only for one of four reasons, and each sidecar's `note` ends with a
machine-generated `|| NOT asserted here: ...` list so the note can never drift from the
file:

1. **Unstable across replicates.** `number_of_days` on 2 docs; the billing-period dates on
   `bug_260601_0018` and `vancouver_water` (the latter flips between the billing period and
   the meter-read dates). **`invoice_description` is excluded by rule on all 15**, not by
   measurement: across 3 replicates x 15 docs it reworded itself on 7 -- a trailing period
   (`Electricity utility charges` / `...charges.`), a synonym (`Waste and recycling dumpster
   service` / `Dumpster and recycling service`), `&` vs `and`. Any 3-run sample where free
   text agrees is luck, so asserting it anywhere guarantees future red runs.
2. **Date-dependent.** A *defaulted* `invoice_date` or `payment_due_date` is today /
   today+30 -- stable within one run, red the next day. Three bills print no due date
   (`bug_260601_0018`, `bug_260615_0006`, `business_license`) and `business_license`
   prints no issue date.
3. **Verified wrong.** `billing_period_start_date` on `bug_260605_0017`: both twins return
   `2006-01-26` on 3/3 -- a year misparse of the printed `Service Period: 06/01/26-06/30/26`
   (June 2026). Stable but incorrect, so asserting it would lock in the bug. **Open item:
   a slashed 2-digit-year range is misread as year 2006.**
4. **Genuinely non-deterministic in the pipeline.** `vendor_name` on `fortisbc` (resolved
   spelling follows whichever twin is more confident) and `bug_260605_0017`;
   `service_address` on three docs whose extract intermittently returns null; and
   `routingDecision` on those same docs, because a null base-critical field sends the run
   to review.

**The `invoice_date` twins are not asserted on any document.** Building this coverage
showed the `invoice_date_extract` twin returning `null` on roughly 1 run in 3-6 across at
least five unrelated docs (`abbotsford_water`, `bchydro`, `bug_260609_0031`,
`bug_260629_0026`, `fortisbc`) -- a property of the field, not of particular documents,
and precisely what the corroborated rescue exists to absorb. The *resolved* `invoice_date`
is stable on all 15. Same story for `service_address`. **That intermittent null on a
base-critical field is a real production behaviour worth its own investigation -- it means
a clean invoice occasionally lands in the review queue for no document-side reason.**

The rule throughout: assert the resolved value, never a raw twin, and never a value that
moved across replicates.

---

## `service_address` empty when the invoice is addressed only to the Bill To block

**Symptoms (verified 2026-07-24, `samples/bug_260629_0012.pdf`, JMEC Electric):** a small
trade/contractor invoice with **no** SHIP TO / Service Address / Attention block. The only
address that names a location is the Bill To block (`Noble & Associates, #307-7480 Gilbert
Road, Richmond BC, [Job# 11024565]`). Both `service_address` twins correctly returned empty
(the prompt excludes Bill To / customer-mailing addresses), so the invoice routed to
`REVIEW_B4_CRITICAL_FIELD` even though a usable address was on the page.

**Cause (by design, not a bug):** `service_address` is a base-critical twin whose prompt
deliberately excludes the Bill To block. With no other candidate the field is empty and the
critical-field gate sends the doc to review.

**Fix (code + prompt, additive):** a new **twinned** `bill_to_address` field
(`bill_to_address_extract` / `bill_to_address_generate`) captures the Bill To block. It is
**internal-only** — registered in `field_policy.TWIN_FIELDS` and `gates.FIELD_PRINT_ORDER`
but **not** in `WRITE_FIELDS` (no Dynamics/Dataverse column). A code rescue in
`gates.evaluate`, placed in the pre-`evaluate_b4` rescue region (alongside the PO / filename /
billing-start rescues), promotes `bill_to_address` to `service_address` **only when the
resolved `service_address` is empty** AND the Bill To clears the confidence bar (threshold or
twin agreement) AND it is **not** Noble's own head office
(`field_policy.is_noble_office_address`, matched against
`NOBLE_OFFICE_ADDRESSES = ("155-13988 Maycrest Way, Richmond BC  V6V3C3",)`). A
present-but-low `service_address` is never overwritten (it found a real address and still
routes to review, mirroring the billing-start rule). The `service_address` prompt itself is
untouched, so the existing Bill-To *exclusion* corpus cannot regress. Scratch CU regression:
JMEC → `#307-7480 Gilbert Road\nRichmond, BC`, `HAPPY_PATH_CANDIDATE`, stable 3/3 across two
independent live runs.

> **Superseded in part, 2026-07-31.** The rescue now fires when the resolved
> `service_address` is empty **or is itself Noble's head office**, and
> `is_noble_office_address` is no longer a bare `_address_tokens_agree` call. See
> "`service_address` took Noble's own office from the SHIP TO block" below.

**Verification trap worth remembering (the control-run discipline).** Right after the change,
the full corpus CU regression showed **12** `UNSTABLE` (doc, field) checks — none `WRONG`, and
none on `service_address` / `bill_to_address`. That looked alarming, but adding a field to a CU
prompt does not obviously cause it. The decisive check was a **live** control: re-running the
**pre-change (HEAD) analyzer** with `--analyzer-file <git-show> --force` (fresh CU calls, not
the cache — a *cached* control is frozen and cannot show instability). Results:

| run | analyzer | non-JMEC `UNSTABLE` |
|---|---|---|
| regression run 1 | current (with `bill_to_address`) | 12 |
| confirmatory run 2 | current | 4 |
| live control | HEAD (no `bill_to_address`) | 2 |

Run 2 (4) sits next to the control (2), so run 1's "12" was a high-variance draw, not a
systematic increase. Every flip in all three runs is the same inherent-noise set already
catalogued above (`billing_period_end_date` blank↔date, `payment_due_date` → today+30 default,
`vendor_name` newline/casing); `bug_260601_0018`'s `billing_period_end_date` flips in **all
three**. **Lesson:** judge a prompt change against a *live, forced* HEAD control on the same
corpus — a single regression snapshot is a noisy instability estimator, and a cached control
proves nothing. Do not weaken the other docs' sidecars over inherent noise.

**Rollout order:** push the analyzer to prod **before** the function deploy. The rescue is
inert (never harmful) if the deployed analyzer lacks `bill_to_address` — it just leaves
`service_address` empty as before — but the fix only takes effect once the field is live.

---

## `service_address` took Noble's own office from the SHIP TO block

**Symptoms (verified 2026-07-31, `samples/bug_260703_0038.pdf`, Alpha Integrated Systems):** the
invoice carries two customer blocks naming the same customer — **Sold to** `8631 Alexandra Road,
Richmond BC V6X 1C3` (the serviced property) and **Ship to** `Unit 155 - 13988 Maycrest Way`
(**Noble's own head office** — the manager receives the paperwork). `service_address` was written
as the Maycrest Way office. Both twins returned it confidently and identically on every replicate
(extract 0.744 / 0.829 / 0.781, generate 0.902 / 0.896 / 0.886), so **no twin-resolution rule can
help**: the twins agree, just on the wrong block.

**Cause — an invariant enforced on only one of two paths.** The rule *"Noble's own office is never
a service address"* already existed: `field_policy.NOBLE_OFFICE_ADDRESSES` +
`is_noble_office_address`, whose docstring says exactly that. But `gates.evaluate` only consulted it
when *promoting* a Bill To block into `service_address`. A `service_address` read straight off the
page was never checked against it. The guard fired on the value rescued **in** and never on the
value read **out**. The prompt's SHIP-TO-over-SOLD-TO priority is right everywhere else and was not
the thing to weaken.

**Fix — code + prompt, converging on the same value.**

1. **`gates.evaluate`** — the Bill To rescue now triggers on `empty OR Noble's own office`, not just
   `empty`. On a match it promotes `bill_to_address` (source `bill_to_fallback`); if the Bill To is
   itself the office or below the bar, `service_address` is **cleared** (`None`, `passed=False`,
   source `noble_office_rejected`, confidence 0.0 so the B4 reason does not quote the office read's
   own high confidence) and B4 routes the doc to review. Writing the paying party's address into
   Dynamics is worse than sending the doc to a human.
2. **`is_noble_office_address` tightened.** Bare `_address_tokens_agree` scores over the *smaller*
   token set, so `'Richmond, BC'` (1.000) and `'Unit 200 - 13988 Maycrest Way'` (0.714) both matched
   the office. Harmless while a match only *blocks* a promotion — destructive now that a match
   *discards* an address. It now also requires the office's purely-numeric tokens (`155`, `13988`;
   postal-code tokens are alphanumeric and excluded, since `V6V 3C3` / `V6V3C3` tokenize
   differently). Validated by replaying the cached corpus: **all nine** spellings of the office that
   actually occur in `bill_to_address` still match; the three false positives no longer do.
3. **The analyzer prompt is UNTOUCHED — a blacklist sentence was written, measured, and reverted.**
   See "the prompt attempt" below. No prod analyzer push is needed for this fix.

**Verification — the code half A/B's for free.** Because `regress.py` caches on
`(analyzer hash, pdf hash, replicate)`, a **code-only** change re-scores every cached CU result
with zero CU calls. The model output is byte-identical, so the comparison has *no* noise at all
and needs no live-control argument — unlike a prompt change. Sequence the work to exploit that:
land and verify the code half *before* touching the analyzer JSON.

| stage | CU calls | result |
|---|---|---|
| code only, cached corpus | **0** (69 cache hits) | 403/403 OK across 23 docs. The code change moved **exactly one** (doc, field) value in the whole corpus — the target. |
| live forced HEAD control (prod prompt) + new code | 69 | `bug_260703_0038` **3/3** correct: the twins still return the office, the guard rejects it, `bill_to_fallback` writes `8631 Alexandra Road`. This is the proof the fix needs no prompt change. |

### The prompt attempt: measured harmful, reverted (2026-07-31)

A one-sentence blacklist was added to **both** `service_address` twins — *that address is the
property manager's own office and is never a serviced property; a SHIP TO block holding it names no
service address, so return an empty string.* Deliberately a blacklist and not a redirect to SOLD TO,
since the prompt forbids returning SOLD TO in six other clauses. It **worked on the target doc**
(10/10, both twins empty → the Bill To rescue) and the full-corpus run was **0 WRONG, 2 UNSTABLE vs
the live control's 2** — by aggregate count, indistinguishable.

**The aggregate count hid the regression.** The *identity* of the unstable key was the signal: the
new prompt destabilised `service_address` on `260629_0010`, a doc that carries the blacklisted
Maycrest address **in its Bill To** while its real service address hides in a line-item job block.
A targeted A/B on that one doc:

| prompt | runs writing `service_address = None` |
|---|---|
| blacklist | **3 / 15 (20 %)** |
| HEAD | **0 / 13 (0 %)** |

(one-tailed Fisher p = 0.139 — not significant on its own, but the mechanism is specific and the
direction is the exact field edited). The pre-existing flake on that doc is the *extract* twin's
confident null (0.782, ~5/10 runs); the **generate** twin always covered it. The blacklist made the
generate twin null out **too** — its closing "return an empty string" is a salient new exit, and
this doc is precisely where the address is hardest to find.

**Cost/benefit decided it, not the p-value.** The benefit was *zero*: the code guard alone already
writes the correct address under the unmodified prompt (3/3, table above), because both paths end in
the same Bill To rescue. The cost was a base-critical field going blank on ~1 in 5 runs of a
real invoice, plus a prod analyzer push dragging the whole pending prompt backlog with it. Reverted.
**Lesson:** when a code path and a prompt change converge on the same value, the prompt half must
justify itself on its own — and "no measurable regression in aggregate" is not the same as "no
regression," because a targeted hypothesis deserves a targeted A/B on the doc that motivates it.
This is the second field (after `vendor_name` dba) where the prompt attempt lost to the code fix.

**A forced control run overwrites cache and can un-hide old flakes.** `--force` on the HEAD control
replaced `bug_260528_0016`'s cached draws, after which the corpus went red on `payment_due_date` and
`invoice_description` — neither related to the change. `payment_due_date` is the instructive one: CU
returns the correct `2026-06-16` on 5/5 runs, but its confidence straddles the 0.73 bar
(0.721 / 0.813 / 0.803 / 0.946 / 0.973) and the sub-bar run substitutes the today+30 default. Both
keys were unasserted per the corpus's own rule. **A green corpus can be a lucky draw**; a forced
re-run is what tells you which assertions were real. That a correct, stable, printed date is
discarded over a 0.009 confidence dip is a standing open item.

**Do not find the repo root by walking up from `__file__` in a scratchpad script.** A helper written to
`…/Temp/claude/…/scratchpad/` used `while not (p / "scripts").is_file(): p = p.parent` to find the
repo root. Outside the repo that loop cannot terminate — `Path.parent` of a drive root returns
itself — so it spun at 100% CPU for 24 minutes and made zero CU calls, and piping it through
`tail` hid the fact that not even the first `print` had run. Take the repo root from `cwd` with an
explicit failure if it is wrong, and run live scripts unbuffered (`python -u`) without a pipe so
progress is visible.

---

## `vendor_name` drops the "dba" trade name — FIXED in code (4 prompt attempts failed first)

> **Status 2026-07-31: FIXED, code-side, prompt untouched.** The fix is one branch in
> `field_policy._prefer_vendor_generate`: when the extract twin carries a "dba" connector,
> the printed name wins over the generate twin's short common name, regardless of
> confidence. `analyzers/create-generalinvoice-analyzer.json` is **unchanged** — a fourth
> prompt attempt was measured and reverted too (attempt 4 below).
>
> The reproduction PDF is now a corpus anchor at `tests/pre-commit-test/bug_260504_0021.pdf`.
> Its sidecar does **not** assert `vendor_name` (see below); the real guard is the
> deterministic dba block in `tests/test_field_policy_gates.py::test_vendor_extract_generate_twin`.

**The fault that was actually fixed — #3, the only one never attempted.** Of the three
faults catalogued below, only #3 fired on the 2026-07-31 reproduction. Replaying the
observed twins through the real resolver:

```
extract  = 'Graffiti Guys Removal Services\ndba Goodbye Graffiti Surrey'  @ 0.662
generate = 'Goodbye Graffiti'                                             @ 0.710
agree = True (substring containment)   prefer_generate = True (0.710 >= 0.662)
=> RESOLVED 'Goodbye Graffiti', passed=True, source=agreement  -- the bug
```

The extract twin had the vendor **right** and the confidence tiebreak threw it away.
Attempts 1-3 were all prompt-side, which is why three rounds of prompt work never reached
it. Lesson: when a twin is correct and the *arbitration* discards it, fix the arbitration.

**Why this fix is cheap and safe:** it touches no prompt, so `regress.py` re-scores the
cached raw CU results for free (0 CU calls), no prod analyzer push is needed, and there is
no cross-field coupling risk. It is a no-op on all 20 known corpus and test vendor values —
none contains a dba connector — and all 10 pinned vendor cases (FortisBC ×2, SIMON ×2,
LevEllen, ACME, Whoever, empty-extract rescue, CITY OF SURREY, Xpert) are byte-identical.

**Verified live, 10 replicates, both arms** — `writeValues.vendor_name` was never
`Goodbye Graffiti` in 20/20 runs. The written value legitimately flips between two
acceptable forms because the extract twin itself truncates at the line break on some runs:
`Graffiti Guys Removal Services dba Goodbye Graffiti Surrey` (8/10) and
`Graffiti Guys Removal Services` (2/10). The business decision (2026-07-31) is that both
are acceptable, so the sidecar asserts neither.

### Attempt 4 — re-ranking the sources in `vendor_name_generate`. Measured HARMFUL, reverted.

Rewrote the printed-before-logo rule as an explicit ranked list, logo last:
*"Prefer printed text over a stylized logo …: (1) remittance/remit-to; (2) footer legal name
or a 'does business as' clause; (3) contact block or website domain; (4) a stylized logo …,
only when the name is printed nowhere else."* Same-day A/B, 10 replicates per arm, on the
target document:

| `vendor_name_generate` | HEAD | attempt 4 |
|---|---|---|
| `Graffiti Guys Removal Services` (legal name) | **8/10** | **0/10** |
| `Goodbye Graffiti` (trade name) | 2/10 | **10/10** |

It made the twin *worse* on the document it was written for. Two probable causes, both in
the edit: it **deleted** the sentence *"When the name appears both as a stylized logo and as
printed body text, use the printed text"* — exactly the both-present tiebreak this bill needs
— and it converted a rejection (*"rather than a stylized logo"*) into a **permission**
(*"(4) a logo … only when"*). That is Lesson 1 again from the other direction: naming the bad
pattern as an allowed fallback makes it available.

**Corrected premise worth keeping.** It was assumed (in an earlier draft of this section, and
again in 2026-07-31 planning) that the generate twin returns the trade name because that *is*
the short common name, and that `Goodbye Graffiti` came from printed text rather than the
logo. Both are wrong: under the shipped prompt the twin returns the **legal name 8/10**. The
`Goodbye Graffiti` in the bug report was the 2/10 minority case.

**Method note — a cached control proves nothing, restated the hard way.** Attempt 4's first
corpus run showed 2 UNSTABLE docs (`abbotsford_water total_invoice_amount_generate`,
`surrey_water billing_period_start_date`), each stable across 12 cached buckets, which looked
like proof the edit caused them. It was not. A fresh 5-replicate run on the **unedited** HEAD
prompt produced its own novel UNSTABLE (`bug_260605_0017 payment_due_date`, likewise stable
in 12 buckets). Each bucket holds only 3-5 replicates of a given field, so a ~1-in-5 flip
simply had not landed yet; every fresh 5-replicate corpus run surfaces one or two, in
whichever bucket is fresh. **At n=5 the corpus has a background flake rate of 1-2 UNSTABLE
(doc, field) pairs — a raw UNSTABLE count cannot attribute causation.** What settled attempt 4
was a same-day, both-arms A/B on the single document under test, in a reserved replicate band
(`r100+`), which cost 20 calls instead of ~400.

**Known pre-existing instability, not caused by this change:** `bug_260605_0017`
`payment_due_date` defaults to today+30 on roughly 1 run in 5 (its cached `r4` in bucket
`2ecdbe8dca80` holds such a run, so `--replicates 5` stays red on that pair until the cache
is rebuilt). The default 3-replicate run the pre-commit hook and the prod-push gate use is
green: 348/348 across 20 docs.

**Symptoms (verified 2026-07-29, `samples/bug_260504_0021.pdf`, Goodbye Graffiti inv 37766):**
the bill prints the vendor on two lines — `Graffiti Guys Removal Services` / `dba Goodbye
Graffiti Surrey` — and the resolved `vendor_name` coin-flipped across three replicates:
`Goodbye Graffiti` (the logo wordmark), the correct full pair, and `Graffiti Guys Removal
Services`. All three routed `HAPPY_PATH_CANDIDATE`, so nothing flagged it.

**Cause (three independent faults, all still present):**

1. `vendor_name_extract` truncates at the line break on some runs (`Graffiti Guys Removal
   Services` at 0.981) and returns the full pair on others (0.468 / 0.728, with a `\n`).
2. `vendor_name_generate` answers with the logo (`Goodbye Graffiti`, 0.609) — its prompt
   asks for "the short common name the vendor is known by".
3. `_prefer_vendor_generate` then picks the **more confident** of two spellings that
   "agree" by substring containment. On the first replicate the correct full string
   (0.468) therefore lost to the logo (0.609).

### What was tried, and what each attempt cost

Each was measured on the 20-doc corpus at 5 replicates, against a live HEAD control.

**Attempt 1 — the rule in `vendor_name_extract`.** Fixed the target doc. Broke `fortisbc`
outright: its bill prints the disclaimer *"FortisBC Energy Inc. does business as FortisBC."*
and the twin returned that **entire sentence** 5/5 where the control was 8/8 stable
`FortisBC Energy Inc.` Confidence fell corpus-wide: control `260629_0010` 0.94→0.74,
`fortisbc` 0.91→0.76; `260521_0024` −0.197, `burnaby_water` −0.172, `vancouver_water`
−0.165, `richmond_water` −0.148. Also truncated `PRIORITY appliance service` → `PRIORITY`
2/5 on an unrelated doc.

**Attempt 2 — the rule in `vendor_name_generate` only.** Commercial docs improved
(`bug_260504_0021` +0.333, `bug_260615_0006` +0.501, `bug_260601_0018` +0.349) but **every
municipal doc regressed**: `surrey_water` **−0.410** (2/5→5/5 sub-threshold, and its casing
destabilised so `City of Surrey` flipped to `CITY OF SURREY` 2/5 — a corpus failure),
`abbotsford_water` −0.265, `west_van_water` −0.250, plus `bchydro` and `260521_0024` pushed
under the bar. A trade-name rule has nothing to say about `City of Surrey`, so for those
documents it is pure noise in the prompt.

**Attempt 3 — a dedicated `vendor_dba_name` twin + a promotion in `gates.evaluate`,** with
both vendor prompts byte-identical to prod. This *worked* for vendor names: target doc 5/5
correct, the new field returned null 5/5 on the four control docs, and the municipal
confidence lost in attempt 2 came back (`surrey_water` 0.39→0.74, `west_van_water`
0.46→0.64, `abbotsford_water` 0.56→0.78). **But it wrecked `invoice_date`** — same day,
with and without the new field: `fortisbc` extract 5/5 dated → **3/5 null**,
`west_van_water` 3/5 dated → **5/5 null**, and the corpus went `WRONG` on
`west_van_water writeValues.invoice_date` (defaulted to today on every replicate).
`invoice_date` defaults to today when the extract returns nothing, so this silently writes
a wrong date — strictly worse than the bug being fixed.

**The claim that broke attempt 3 — and the lesson that matters most here:** "an additive
field is safe by construction because it does not edit any existing prompt". **That is
false.** The analyzer is a single CU call; adding a field changes the model's behaviour on
*other* fields. The suspected driver is bulk — `vendor_dba_name_generate` was a 6-step
reasoning prompt, roughly two-thirds of the added text — but that was not isolated before
the work was reverted. **Any new field must be judged by a full corpus run against a
same-day live control, exactly like a prompt edit.** The `bill_to_address` section above
hints at this; attempt 3 is the proof.

**Lesson 1 — a prohibition primes the pattern.** The first draft described the connector by
its spelled-out phrasing ("doing business as"). FortisBC bills print the disclaimer
*"FortisBC Energy Inc. does business as FortisBC."*, and `vendor_name_extract` returned that
**entire sentence** 5/5 (live control: 8/8 stable `FortisBC Energy Inc.`). Adding a
prohibition — *"Never answer with a sentence of prose such as 'X Inc. does business as Y'"* —
did **not** fix it; the generate twin then produced exactly that sentence on 2/5 runs. What
worked was a **positive worked example**: *"a footer reading 'FortisBC Energy Inc. does
business as FortisBC.' gives 'FortisBC'"* → 5/5 `FortisBC`. State what to answer, not what
to avoid; naming the bad pattern makes it available.

**Lesson 2 (the expensive one) — do not teach an existing prompt a narrow rule; add a
field.** Two full iterations were burned learning this, and both failed the same way: a rule
that applies to a handful of documents dilutes the prompt for every other document.

*Attempt 1, the rule in `vendor_name_extract`:* same-day live control `260629_0010`
0.94→0.74 and `fortisbc` 0.91→0.76; cross-day `260521_0024` −0.197, `burnaby_water` −0.172,
`vancouver_water` −0.165, `richmond_water` −0.148. It also truncated
`PRIORITY appliance service` → `PRIORITY` on 2/5 runs of an unrelated doc.

*Attempt 2, the rule in `vendor_name_generate` only:* the split was starkly systematic —
commercial docs improved (`bug_260504_0021` +0.333, `bug_260615_0006` +0.501,
`bug_260601_0018` +0.349) while **every municipal doc regressed**: `surrey_water` **−0.410**
(2/5→5/5 sub-threshold, and its casing destabilised so `City of Surrey` flipped to
`CITY OF SURREY` 2/5), `abbotsford_water` −0.265, `west_van_water` −0.250, plus `bchydro`
and `260521_0024` pushed under the bar. Obvious in hindsight: a trade-name rule has nothing
to say about `City of Surrey`, so for those documents it is pure noise in the prompt.

*And a dedicated field did not escape it either* — see attempt 3 above, which moved the
damage from `vendor_name` to `invoice_date`. **A prompt is a shared resource: every sentence
added for one document is paid for by all the others, and adding a whole field is the
biggest edit of all, not an exemption from the rule.**

**Lesson 3 — scope a confidence gate to the fields you changed.** A sub-threshold-rate sweep
over every `(doc, field)` pair at n=5 returned **37 "regressed" vs 39 "improved"**, with
untouched fields swinging ±0.3 — symmetric noise, no discriminating power, and it would have
justified any conclusion. The identical metric restricted to the two edited fields gave a
clean directional signal (12 docs down and large, 8 up and small) that located the real
regression. A confidence gate is only meaningful on the fields under test.

**Technique worth reusing — the targeted live control.** A full 5-replicate corpus control is
100 CU calls. Running only the docs that flipped, against `git show HEAD:analyzers/...`, and
writing the results to a **reserved replicate band (`r100+`)** answers the same question for
~25 calls without overwriting the existing cached baseline. Extract the HEAD analyzer with
Python, not PowerShell `>` redirection — the latter adds a BOM and changes the file hash.

---

## `number_of_days_generate` invented a 1-day count

**Symptoms (verified 2026-07-29):** on bills that print **no billing period at all**, the
reasoning twin answered `1` while `number_of_days_extract` correctly returned nothing at
0.904 every run — `260629_0010` 2/5 (0.447–0.468), `bug_260629_0012` 4/5 (one at **0.984**),
`bug_260601_0018` 3/5, `bug_260605_0017` 2/5. Reproduced on a live HEAD control, so it is
independent of any prompt change. Same family as the `invoice_date` print-timestamp case.

**Cause:** `build_write_values` passes non-date fields through **regardless of confidence** —
`write[DAYS_FINAL] = days if days is not None else ""` never consulted the twin resolution,
so an ungrounded value reached Dynamics with `passed=False`.

**Fix:** `DAYS_FINAL` added to `NO_GENERATE_RESCUE`, plus a reliability gate on the written
value. Verified by replaying all 100 cached results: the four flipping docs go stably blank
and all eleven legitimate counts survive 5/5.

**Trade-off, accepted deliberately (confirmed with the user 2026-07-30):** this removes the
generate-only "prose day count" rescue (`bchydro`-style *"used over 30 days"*) that a unit
test documented. Justified because every real day count in the corpus is span-grounded —
`bchydro`'s 30 comes from the **extract** twin at 0.71–0.98, not from prose — and because
confidence alone cannot filter the invented values (one landed at 0.984). It feeds the tenant
utility-sharing math and the billing-start derivation, where the standing policy is already
"blank beats a substituted value". Twin **agreement** still writes a sub-threshold count.

### The same invented `1` came back through the gate's own precondition (2026-08-18)

The reliability gate above was later re-keyed from confidence onto the period — *"is there a
period?" separates the two cases cleanly* — because confidence alone could not (one invented
count landed at 0.984, while a legitimate generate-only count sat below the bar). The premise
is right. The **test** was too loose: it asked whether `write[BILLING_END_FINAL]` was
non-empty, and that write value is itself informational, so it carries below-bar values.

On `bug_260601_0018` — a commercial invoice printing neither a period nor a day count — one
read in 90 had the generate twin offer end `2026-06-30` at **0.516** and count `1` at
**0.238**, both against confident-null extract twins (0.876). The unread end date satisfied
"is there a period?", so **one guess licensed the other**, and the A2/A4 machinery then did
its job on poisoned input: derive start = end − 1 + 1 → a 1-day period → implausible →
re-read the printed range → `2026-06-01..2026-06-30` → `reconcile_number_of_days` promoted the
invented `1` to a fully-invented **30**. Routed `HAPPY_PATH_CANDIDATE`, so it auto-wrote.

**Fix:** the precondition now also requires the end date's own resolution to have `passed`.
One clause, in the same expression.

**Reusable lesson — a below-bar value must never serve as another field's precondition.**
Informational fields are written unconditionally by design; that makes their *write value* a
statement about what CU offered, not about what the page says. When one field gates another,
read the **resolution** (`passed`), never the write value's emptiness. The same shape produced
the `invoice_date` print-timestamp bug and the original invented `1`.

**Two verification lessons, both earned here:**

- **`--replicates 3` and `--all-cached` are different tests.** The run that declared this
  corpus green scored the first 3 replicates per doc and passed 611/611. The defect sits at
  **r6**. Run `--all-cached` before calling anything green — same cost, zero CU calls.
- **Diff every cached decision, not just the corpus.** The 611 assertions cover what sidecars
  pin; replaying all **2,327** cached decisions across every analyzer version is what proved
  the one-clause change touched exactly the 2 intended decisions, kept all 46 legitimate
  reconciliations, and left `bug_260609_0031`'s generate-only 19 at 101/101.

---

## `260629_0010` asserted a routing decision that was always a coin-flip

**Symptoms:** the sidecar asserted `routingDecision: HAPPY_PATH_CANDIDATE`, but the doc
returns `REVIEW_B4_CRITICAL_FIELD` on roughly 2 runs in 5 — including on a **live HEAD
control with the unmodified prod prompt**, so it flakes for anyone's change.

**Cause:** `service_address_extract` returns a *confident null* (~0.78) on those runs, and the
generate twin that then supplies the address straddles the 0.73 bar (0.62–0.87 observed).
The written `service_address` is byte-identical on every run; only the routing moves.

**Fix:** the `routingDecision` assertion removed and the mechanism recorded in the sidecar
note; every value assertion kept. This is *not* corpus-loosening in the forbidden sense — the
corpus's own rule is that an unstable key is never asserted, and asserting a coin-flip
guarantees a future red run. The intermittent-null extract remains a standing open item.

---

## A Python update kills a venv whose base **install layout** moved (not the patch bump)

**Symptoms (verified 2026-08-14, after 3.13.14 -> 3.13.15):** `functionapp\.venv` will not
run at all:

```
did not find executable at '...\Programs\Python\Python313\python.exe':
The system cannot find the path specified.
```

The root `.venv` was healthy on 3.13.15 at the same moment. Nothing loud failed — `pytest`,
the pre-commit hook and deploys all use the root venv or an absolute path — so the dead venv
sits unnoticed until VS Code offers it as an interpreter for the `functionapp` subpath
(`azureFunctions.projectSubpath` points there).

**Cause:** *not* the patch bump. The update also migrated the interpreter from the
python.org-style layout (`%LOCALAPPDATA%\Programs\Python\Python313`) to the Python Install
Manager layout (`%LOCALAPPDATA%\Python\pythoncore-3.13-64`) and deleted the old directory. A
venv hard-codes its base interpreter as an absolute path in `pyvenv.cfg`, and on Windows the
venv's `python.exe` is a copy that loads `python313.dll` and the stdlib from that `home`. When
the directory disappears, the venv is dead. The root `.venv` survived only because it had
already been recreated against the new layout.

**Diagnosis — `pyvenv.cfg` names the base; check every venv in the repo, not just the root:**

```powershell
Get-Content .\.venv\pyvenv.cfg
Get-Content .\functionapp\.venv\pyvenv.cfg
```

If `home` names a directory that no longer exists, the venv is unrecoverable. Recreate it —
there is nothing to repair in place, and `--upgrade` cannot help.

**Fix:**

```powershell
Remove-Item -Recurse -Force ".\functionapp\.venv"
& "$env:LOCALAPPDATA\Python\pythoncore-3.13-64\python.exe" -m venv ".\functionapp\.venv"
.\functionapp\.venv\Scripts\python.exe -m pip install -r .\functionapp\requirements.txt
.\functionapp\.venv\Scripts\python.exe -m pip install truststore   # local dev only
```

`truststore` is deliberately **not** in `functionapp\requirements.txt` — it is the
TLS-inspection workaround for this machine and is unwanted on the Azure Linux host — but a
`func start` driven from this venv needs it for the outbound CU call (see the
`sitecustomize.py` shim entries above).

**Why it should not recur:** the Install Manager directory is keyed to the *minor* version
(`pythoncore-3.13-64`), not the patch, so 3.13.x updates land in place and leave `home` valid.
Only another layout migration, or dropping 3.13, breaks it again.

**Worth running after any Python update** (each venv, then the two test tiers):

```powershell
.\.venv\Scripts\python.exe --version ; .\.venv\Scripts\python.exe -m pip check
.\functionapp\.venv\Scripts\python.exe --version ; .\functionapp\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\regress.py --load-local-settings   # cache hit, 0 CU calls
```

**Unrelated but adjacent:** bare `python` on PATH is 3.14 here and `py -0p` marks 3.14 the
default. That is fine and intentional — the pre-commit hook calls the venv python by absolute
path and `.vscode\settings.json` pins the root `.venv`. It matters only for `func start`,
which needs `VIRTUAL_ENV` set; see the `ZoneInfoNotFoundError` entry above.

## Adding the narrative fields: four prompt failures a single run each would have missed

**Context (2026-08-17, `commercial-narrative-v9`):** added five lone `generate` fields —
`diagnosis_solution`, `recommendation`, `warranty` and the Traditional Chinese
`diagnosis_solution_zh_hant` / `recommendation_zh_hant`. Largest analyzer edit made so far.
Every one of the failures below was found by scaffolding **one document at a time** before the
full corpus run, and every one was invisible in the first replicate.

**1. A capability stated last reads as an exception.** `diagnosis_solution` was written as a
diagnosis procedure with "for a pure goods sale, describe what was supplied" tacked onto the
end of step 4. On the appliance-purchase invoice it returned `''` on **1 run in 3** — the model
weighed the fault-finding framing of steps 1-3 over the trailing clause. Fix: name both cases
in the opening sentence, give the branch its own step ("decide which kind of invoice this is"),
and add an explicit anti-rule — *"Never answer with an empty string merely because no problem
was diagnosed."* Verified 5/5 non-empty. Same shape as the `service_address` lesson at line 754:
**in a generate prompt, order is priority — a case stated last is read as a footnote.**

**2. A language instruction leaks into the neighbouring field.** After that fix, `warranty`
(English-only, no language instruction of its own) came back in **Traditional Chinese on 5/5
replicates**. The `_zh_hant` fields' "Do not answer in English" was steering a field that never
asked for a translation. Two mitigations, applied together: state the language **positively and
explicitly on every field in the group** ("Write the summary in ENGLISH. Other fields in this
schema ask for Traditional Chinese; this one does not."), and **group the schema by language**
so an English field is not adjacent to a Chinese one. Verified English 5/5 afterwards.
**A field with no instruction on some axis inherits its neighbours' — silence is not a default.**

**3. "Keep X in the printed form" is read as "include X".** The `_zh_hant` prompts said *"Keep
brand names, model numbers, serial numbers and dates in the form printed on the invoice; do not
translate them"* — meant as a transliteration rule. The Chinese summaries promptly grew model
numbers, serial numbers, warranty terms and a recycling fee that the English twin correctly
omitted, so the two languages described different things. Fix: anchor the Chinese to the English
scope first (*"Report the same facts, at the same level of detail, that a plain-language English
summary would report"*), list what not to add, and only then make the transliteration rule
conditional (*"Where you do mention a brand name or a date, keep it..."*).

**4. The character limit still is not a limit.** Same lesson as `invoice_description` (line 422),
now measured on a longer field: `warranty` produced **403 characters against a 400-character
budget** on 1 run in 3, by enumerating an exclusion list. Asking for fewer characters does not
work; **removing the thing that makes it long does** — "at most two short sentences… never
enumerate the individually excluded items; write 'with exclusions' instead of listing them."
Because enforcement is prompt-only by decision, the Dataverse columns are sized 1000/1000/500
against 800/800/400 limits so an overshoot cannot fail the row write.

**Two side-effects worth knowing:**

- **Non-ASCII output breaks the CLI scripts on a redirected stdout.** `regress.py`, `test.py`
  and `diag.py` print observed field values; on Windows a *piped* stdout defaults to cp1252, so
  the first Chinese value raises `UnicodeEncodeError` — and the pre-commit hook runs exactly that
  way, so a real FAIL would surface as an encoding traceback. All three now call
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`. A console stdout was already
  UTF-8, which is why this is invisible when you run them by hand.
- **The municipal blanking is a code gate, not a prompt promise.** The prompts do say "return an
  empty string for a municipal utility bill", but `build_write_values` forces all five to `""`
  for the municipal bucket regardless. That makes them deterministic and therefore *assertable*
  on all 15 municipal corpus docs — free-text fields are normally unassertable (line 1330), so
  the gate is what buys the regression coverage back.

**Reusable lesson:** when adding a *group* of related generate fields, scaffold them one
document at a time and read the values before running the corpus. Three of these four failures
produced a plausible-looking value on the first replicate and would have shipped.

## `vendor_name` on a stylized-logo letterhead — fixed in code by domain corroboration

**Symptom (2026-08-17, `260629_0024` PRIORITY appliance service):** the written vendor was
`Vancouver Central Dispatch` — a different company, printed in the top-right contact block.
Routing flipped to `REVIEW_B4_CRITICAL_FIELD` because the two vendor twins disagreed and both
sat far below the 0.73 bar (extract 0.319, generate 0.418).

**Cause:** the real vendor name on this letterhead exists *only* as a stylized graphic
wordmark. `vendor_name_extract`'s own prompt says to read "clearly printed text … **rather
than a stylized logo or graphic wordmark**", and offers "the vendor contact block" as a
source — so the dispatch service's plain-text block is arguably the *prompt-compliant*
answer. No prompt wording fixes this without breaking the logo rule everywhere else.

**Fix:** `field_policy.vendor_domain_tiebreak` + a rescue in `gates.evaluate`. The vendor's own
web/e-mail domain is printed on the same letterhead, is machine readable where the logo is
not, and no field prompt competes over it. When the twins name genuinely different vendors and
exactly one matches a printed domain label, that one wins (`source = "domain_corroborated"`).

**Two guards that the corpus proved are load-bearing — both were added after a first version
regressed other documents:**

1. **Skip when the twins are *consistent*.** Without this the rule fired on `delta_water`
   ("The Corporation of Delta" vs "Delta"), `west_van_water` ("District of West Vancouver" vs
   "West Vancouver") and `recommend_240124_0001` ("CAMBIE ROOFING CONTRACTORS LTD." vs "Cambie
   Roofing") — a city's own domain always matches the SHORT form, so it stripped "District of"
   from correct names. Those are one vendor in two spellings; there is no tie to break.
2. **A prefix match needs 60% overlap.** `'vancouver'` prefixes `'Vancouver Water Works'`,
   which let `www.vancouver.ca` promote a wrong municipal vendor. Caught by a unit test before
   it ever reached the corpus.

After both guards the rescue fires on **1 of 29 corpus documents** — the one it was written
for. Measured at n=12 on the analyzer with and without it: `260629_0024` routing goes from
**2/12** `HAPPY_PATH_CANDIDATE` to **12/12** on both.

**Reusable lesson:** when a field's correct answer is only available as an image, look for a
*different* machine-readable carrier of the same fact rather than rewriting the prompt. A
domain, an account number, a GST number — evidence no prompt is fighting over.

## A cached-green corpus hid three coin-flip assertions for weeks

**What happened (2026-08-17):** editing the analyzer JSON changed its hash, which invalidated
the whole content-addressed cache and re-rolled all 29 documents. Six (doc, field) rows went
red. The first read was "the new fields broke existing extraction", and a second CU analyzer
was nearly built to isolate them. A live HEAD control at **n=12** showed the opposite:

| row | HEAD (unmodified prod prompt) | verdict |
|---|---|---|
| `260629_0024` vendor / routing | correct **2/12** | assertion was a ~17% outcome |
| `fortisbc` `vendor_name_extract` | `FortisBC Energy Inc.` **7/12** | coin flip, and a raw twin |
| `fortisbc` `invoice_date_extract` | dated **8/12** | inherently unstable; shipped value fine 12/12 |
| `260521_0024` `number_of_days` | unstable on both arms | pre-existing |
| `west_van_water` vendor casing | identical on both arms | pre-existing |

**None of the six was caused by the change.** Every one was a long-standing instability whose
sidecar had frozen one lucky roll.

**Two process lessons, both already half-written in this file and both re-learned the hard way:**

- **n=5 cannot separate 4/5 from 3/5, and a 5/5 sample of a 67%-true field is common.** A
  regression was claimed twice on n=5 evidence and withdrawn both times. For any (doc, field)
  comparison that will drive a decision, use n≥12 per arm.
- **Only a `--force` re-roll tells you which assertions are real.** A green run off the cache
  proves nothing about stability; it proves the cache still holds the roll it was written from.

## `service_address` was two bugs, and neither showed up in the sidecars

**2026-08-18.** `B1` in `open-defects.md` described one defect on two documents at "roughly 1
run in 3". Re-scoring **all 1,633 cached `service_address` resolutions** (24 docs × 12 analyzer
versions) through `gates.evaluate` found something else: 41 failures, **five** documents, and
**two** mechanisms that need different fixes.

| Shape | n | What CU did | Fix |
|---|---|---|---|
| generate-only, below bar | 28 | `service_address_extract` returned a **confident null** — value absent, confidence 0.782/0.837. The generate twin read the address correctly but at 0.41–0.87, straddling the 0.73 bar | `address_corroborated_by_span` |
| both twins null | 13 | nothing read at all | correct review, except on `business_license` |

**A confident null is not a weak read.** 0.782 is CU's confidence that there is *nothing to
extract*. The extract method is span-grounded and label-steered, so when the address sits
outside every labelled block its prompt lists — a line-item job note (`Job# 11024580 | key
stuck: 8631 Alexandra Road`), a bare `Attention:` heading, a `Locations` table column — it
declines, correctly by its own rule. The generate twin reasons over the page and *does* answer,
reporting the weak label evidence as a low confidence. So "the address is clearly printed" and
"the extract twin returns nothing" are not in contradiction, and **the pipeline was writing the
value while simultaneously sending the document to review.**

**Grounding by span beats grounding by page text.** The generate twin carries `spans` into the
OCR markdown. `date_corroborated_in_text` (the earlier precedent) can only ask whether a value
appears *somewhere* on the page — too weak for an address, since the vendor's own address is
printed too. A span says *where CU read it*, so a letterhead address cannot be laundered by a
page-wide hit. All 28 cases were span-grounded and none was Noble's office.

**When no label exists at all, read the layout in code.** `business_license` (City of Vancouver
renewal) prints the licensed premises only in a `Locations` table column; the one address block
on the page is the c/o mailing block, which is Noble's own office and which both twins correctly
refuse. A prompt change was considered and rejected: it cannot guarantee the both-null run, it
risks the cross-field damage that prompt edits have caused before, and it needs a prod push.
`licence_location_address` reads the column deterministically instead — 10/10 stable, including
the run where CU returned nothing. **Blast radius was measured before writing it: exactly one
document in the corpus has such a column.**

**Process:** the two documents' sidecars had `service_address` and `routingDecision` deleted to
keep the corpus green. That is backwards — `service_address` is base-critical, so dropping it
removes the only guard on the field that decides routing. Standing rule now: an unstable
critical field is a bug to fix, never an assertion to delete. All 29 sidecars assert it again.

## Writing Python through a bash heredoc mangles backslashes — use `chr()` or a real file

**2026-08-18, twice in one session.**

1. A regex written as `r"...\b(ave|...)"` inside a `<<'PY'` heredoc reached Python as `\b`,
   which is a **valid escape** (backspace, 0x08), so the file was written containing literal
   control characters instead of word boundaries. `\s` and `\d` survived only because they are
   *invalid* escapes (they emit a `SyntaxWarning` and pass through). The fix that worked:
   `s.replace(chr(8), chr(92) + "b")` — build the backslash by code point and never re-escape.
2. `s.replace('_ROW = re.compile(r"<tr>.*?</tr>", re.S)\n', '')` also matched the tail of
   `_TABLE_ROW = re.compile(r"<tr>.*?</tr>", re.S)`, silently truncating an unrelated constant
   to `_TABLE`. Collection then failed with `NameError`.

**Rule:** for anything containing backslashes or short identifiers, use the `Write`/`Edit`
tools, not a heredoc-fed `str.replace`. If a script must do it, anchor the match on a unique
surrounding line and assert the occurrence count before writing.

## `git checkout` on the analyzer silently invalidates the whole regression cache

**2026-08-18.** Reverting an analyzer edit with
`git checkout analyzers/create-generalinvoice-analyzer.json` restored the *content* exactly —
`git diff` clean — but the working tree file came back with **LF** line endings where it had
been **CRLF**. `regress.py` keys its cache on `sha256(analyzer_file.read_bytes())[:12]`, so the
identical prompt now hashed `c110842176f5` instead of `cd1e585c2f1f`, every one of the 406
cached replicates missed, and the next `regress.py` run started re-calling CU for the entire
corpus. It looked like a hang; it was a full live re-roll.

**Detect:** the hash `regress.py` prints in its header is not the one your cache directory is
named after, and the run reports `0 cache hits`.

**Fix** (restores the byte-identical file, and with it the cache):

```python
b = pathlib.Path("analyzers/create-generalinvoice-analyzer.json").read_bytes()
assert b.count(b"\r\n") == 0
pathlib.Path("analyzers/create-generalinvoice-analyzer.json").write_bytes(b.replace(b"\n", b"\r\n"))
```

**Why it bites:** git stores this file with LF (hence the standing "CRLF will be replaced by LF"
warning) while the working copy has always been CRLF, so *any* checkout, stash pop, or fresh
clone produces a cache-invalidating hash for an unchanged prompt. Verified corpus state is tied
to the CRLF bytes. Before assuming a prompt change busted the cache, check the line endings.

## Editing only the narrative character limits damaged `vendor_name` and `account_number`

**2026-08-18.** C4 asked for all five narrative limits to be set to 500. The edit touched
nothing but the `Aim for N characters...` sentence inside five `description` strings — five
lines, JSON otherwise byte-identical. The first live 3-replicate roll of the new analyzer broke
two assertions in fields with no relationship to those prompts:

| Assertion | 14 previous analyzer versions | new analyzer, n=3 |
|---|---|---|
| `delta_water.vendor_name` | `City of Delta` **68/68** | `Delta` 2, `City of Delta` 1 |
| `recommend_241105_1061.account_number` | `null` **14/14** | `#11020375` (its PO number) on 1 |

Neither had ever flipped in the cache's history, so this is not the usual re-roll exposing a
long-standing coin flip — the control arms are large and clean. It is the cross-field coupling
already recorded for this analyzer, reaching further than expected: a *character-limit* edit on
generate-only narrative fields moved an extract twin on an unrelated municipal bill.

**Lessons, both re-learned rather than new:**

- Treat ANY change to `analyzers/*.json` as capable of moving ANY field, however local the edit
  looks. Budget a full live corpus roll for it, and never assume a "cosmetic" prompt edit is
  cheap.
- Prefer code-side fixes. Every defect fixed in this session that landed — A2, A3, A4, B1, B5,
  B6b, B6c — was code-side, verified offline against 1,749 cached decisions, and needed no
  analyzer push. The one prompt change attempted was reverted.
- A prompt cannot *guarantee* an output constraint. If a length must be bounded (because the
  Dataverse column rejects the row and the invoice is lost), bound it in code or in the column
  size — not in the instruction.

---

## The tracked pre-commit hook is not the hook that runs

**Symptom (found 2026-08-18, while shipping the `number_of_days` fix).** The commit's own
Tier B output read:

```
x3 replicates per doc (87 scored: 0 CU calls, 87 cache hits)
```

87 = 29 docs x 3. But `scripts/hooks/pre-commit` passes `--all-cached`, which should have
scored every replicate on disk (197 for the current analyzer, 450 at the time C1 shipped).

**Cause.** `.git/hooks/` is **not versioned**. `scripts/hooks/pre-commit` is only the *source*;
`scripts/hooks/install.ps1` copies it into place. C1 edited the source and never re-ran the
installer, so the hook that actually executed was a stale pre-C1 copy without the flag. Every
commit between C1 and this one was gated on 87 decisions rather than 197 — including the
commits that shipped A1-A5, B1, B3, B5, B6b and B6c.

The defect found that day (`bug_260601_0018.number_of_days`, at replicate **r6**) is precisely
one the real hook would have caught and the stale one could not. C1's own Resolved row claimed
"the pre-commit hook now passes it" — true of the tracked file, false of the running hook.

**Fix.**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\hooks\install.ps1
tail -1 .git/hooks/pre-commit   # must end with --all-cached
```

**Reusable lesson — editing a hook's source does not change behaviour; installing it does.**
Anything under `.git/` is per-checkout state that git will never carry for you: hooks,
`config`, `info/exclude`. After changing `scripts/hooks/pre-commit`, re-run the installer and
**verify against the running copy**, not the tracked one. The same applies to a fresh clone,
where no hook is installed at all and the corpus gate is silently absent.

Cheapest standing check: read the scored count in the hook's own output. `87` means three
replicates per doc; the `--all-cached` number is larger and grows as replicates accumulate.

---

## `func azure functionapp publish` fails "Unable to connect to Azure" — it shells out to raw `az`

**Symptom (verified 2026-08-18, first real deploy since the truststore fix):**

```
Unable to connect to Azure. Make sure you have the `az` CLI or `Az.Accounts`
PowerShell module installed and logged in and try again
```

...even though `az account show` in the same shell returns the right subscription.

**Cause, in two parts.**

1. `az account show` proves nothing about connectivity — it only reads the local token
   cache. The entry above says so; it is easy to read it as "auth is fine". The honest probe
   is a network call: `az account get-access-token`.
2. The truststore fix is installed as a **PowerShell profile function** named `az`. That
   shadows the real CLI only inside an interactive PowerShell session that loaded the
   profile. `func` spawns its own child process and invokes `az.cmd` off `PATH` directly, so
   it gets the raw CLI, which fails TLS. A non-interactive shell (`-NoProfile`, CI, this
   agent's tool calls) has the same problem for plain `az`.

**Fix — a `PATH` shim, not a profile function.** Put an `az.cmd` in a directory prepended to
`PATH`, so *any* child process resolves `az` to the truststore bootstrap:

```bat
@echo off
"C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe" "%LOCALAPPDATA%\az-truststore\azrun.py" %*
```

```powershell
$env:PATH = "<shim dir>;$env:PATH"
az account get-access-token --query expiresOn -o tsv   # must succeed before deploying
func azure functionapp publish func-invoiceprocess-westus --build remote
```

Session-scoped by default. Making it permanent means putting the shim dir on the user `PATH`
— which would also make the profile function redundant.

**Two things that look like failures and are not:**

- **A trailing SSL error after "The deployment was successful!"** `func` fetches the invoke
  URLs as a last step and that call goes through the inspector. The deploy has already
  landed; the process still exits non-zero. Read the log, not the exit code.
- **`Local python version '3.14.7' is different from ... '3.13'`.** Only matters for a *local*
  build. With `--build remote` the packages are built on Azure against the app's configured
  3.13, so the warning is noise. (Do not "fix" it by moving the app to 3.14 — Flex
  Consumption has no remote-build support there.)

**Also flaky:** `az functionapp show` intermittently dies with `ConnectionResetError 10054`
mid-handshake while `az functionapp function list` succeeds seconds later. Same known reset;
retry once, then stop chasing it and verify another way.
