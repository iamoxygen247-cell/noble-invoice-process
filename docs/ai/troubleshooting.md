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
Az.Accounts PowerShell module installed and logged in` — even though `az account show`
(via the truststore wrapper) shows a valid login.

**Cause:** `func` shells out to the raw `az.cmd` on PATH (not the PowerShell profile
wrapper) to fetch an ARM token. When the cached access token has expired, `az.cmd`
must refresh over the network and dies on the TLS-inspection SSL failure above, so
`func` sees no credential.

**Fix (verified 2026-07-07):** pre-warm the az token cache through the truststore
bootstrap, then publish — `az.cmd` serves `func` the cached token without a network call:

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
local issuer certificate`. The host, the Azurite-backed ledger, and gate A1 all work — only
the outbound HTTPS call to Content Understanding fails.

**Cause:** the same transparent TLS-inspecting agent as the `az` entry above, but here it hits
the **Python worker** the func host spawns. The `.NET` func host trusts the agent root via
SChannel, but the Python worker uses `certifi`, which doesn't include it. This is not
`az`-specific — any venv-Python outbound HTTPS (the Functions worker, or the standalone
harness scripts) is affected.

**Fix:** make the worker verify via the Windows cert store with
[`truststore`](https://pypi.org/project/truststore/), exactly like the `az` fix. For a local
run, put a `sitecustomize.py` on `PYTHONPATH` (auto-loaded at interpreter startup) before
`func start`:

```python
# sitecustomize.py — on a dir added to PYTHONPATH
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

**Fix (verified 2026-07-02):** run it with key auth + the truststore bootstrap in one go —
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
initialize with `ModuleNotFoundError: No module named 'tzdata'` →
`ZoneInfoNotFoundError` from `field_policy.py`'s `ZoneInfo("America/Vancouver")`, and port
7071 never comes up.

**Cause:** Core Tools spawned its own bundled Python (3.14, under
`...\Azure Functions Core Tools\workers\python\...`) instead of the project venv. Putting
`.venv\Scripts` on `PATH` is **not** enough — the host only picks the project interpreter
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
elsewhere in this doc are unaffected — only Python reads hit it.

**Fix:** read with `encoding="utf-8-sig"` (strips the BOM when present, harmless when
absent):

```python
settings = json.loads(path.read_text(encoding="utf-8-sig"))["Values"]
```

---

## CU misreads handwritten receipt-book amounts (drops the cents) and mislabels `is_handwritten`

**Symptoms (confirmed 2026-07-09, `samples/handwritten/260105_0007.pdf`):** a carbon-copy
receipt-book invoice with amounts written in **split dollars | cents columns** ("94 | 50"
with a printed vertical rule, no decimal point) extracted as integer `94`/`4` — the cents
sub-column was dropped by CU itself (both twins; no Python bug). The same document was
classified `is_handwritten = no` even after a targeted classify-prompt rewrite was
verified deployed (fetched the live analyzer definition to confirm before concluding).

**Fixes (verified end-to-end 2026-07-10):**

* **Amounts:** the four amount field descriptions in
  `analyzers/create-generalinvoice-analyzer.json` now explain the split dollars/cents
  sub-column convention with a concrete example ("94 and 50 in adjacent sub-columns means
  94.50"). After that, extract AND generate both read 94.50 / 4.50 stably (7/7 replicate
  analyze calls).
* **is_handwritten:** a classify-prompt rewrite alone was NOT enough — the label itself
  flips run-to-run on borderline documents (not just the confidence; the same bytes
  returned yes and no on consecutive analyze calls). Fixed with the generate-reasoning-twin
  pattern (`is_handwritten_generate`, like `po_or_job_number_generate`): concrete
  receipt-book genre cues, "OCR recognition errors in values are evidence of handwriting",
  and an explicit tie-break — *"when the evidence is mixed or you are unsure, answer yes"*.
  `gates.py resolve_is_handwritten()` surfaces **yes if either twin says yes** (advisory
  flag; a missed handwritten doc is the costly direction). Printed invoices still return
  a clean `no` (the tie-break does not fire on them).

**Reusable lessons:** (1) when a CU prompt fix "doesn't work", first GET the live analyzer
definition and compare — the JSON edit may simply not be provisioned; (2) CU classify
labels near the decision boundary are unstable run-to-run, so judge fixes on several
replicate analyze calls, never one; (3) iterate prompt candidates on a scratch analyzer id
(`create_analyzer.py --analyzer-id <scratch>` — ids cannot contain `-`) so the analyzer
the Function uses stays untouched until the wording is proven.

---

## Investigating a bad extraction (runbook, added 2026-07-10)

Every processed run persists its raw CU result and decision JSON as blobs in the
`invoice-diagnostics` container, with paths stamped on the ledger row
(`RawResultBlob`/`DecisionBlob`, plus `AnalyzerId`, `CuDurationMs`, and — on failed
runs — `FailedStage`/`LastError`). Full design/decision record:
`docs/invoice-diagnostics-design.html`; detailed step-by-step runbook (env setup, RBAC,
fault-domain table, symptom quick reference): `docs/invoice-diagnostics-runbook.html`.

1. **Look up the run** by the SharePoint item GUID — prints the ledger row and downloads
   both blobs to `out\diag\<rk>\`:

   ```powershell
   .\.venv\Scripts\python.exe scripts\diag.py --source-id <guid>
   ```

   A row stuck at `Received`: read `FailedStage`/`LastError` (CU timeout, auth, throttle).
   Runs that predate the sidecar have no blobs — only the ledger stamps.

2. **Read the raw confidences** in `<ts>-raw.json` — the per-field values/confidences CU
   actually returned for that run (a re-run is not evidence; labels flap, see the entry
   above).

3. **Split the fault domain** with an offline replay (runs `gates.evaluate` on the stored
   raw JSON, no CU call, no cost):

   ```powershell
   .\.venv\Scripts\python.exe scripts\diag.py --replay out\diag\<rk>\<ts>-raw.json
   ```

   Replay matches the stored `<ts>-decision.json` but the values are wrong → the CU
   analyzer misread the document: fix the prompt on a scratch analyzer with replicates
   (previous entry). Replay differs from what you expect → the bug is in
   `gates.py`/`field_policy.py`: fix the code and re-replay the same stored JSON as the
   regression check (`--field-threshold` to test threshold sensitivity).
