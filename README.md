# Noble invoice processor

Automated vendor-invoice pipeline: a PDF lands in SharePoint, a **Power Automate**
cloud flow fetches it and calls an **Azure Function** (the decision engine), which
classifies and extracts via **Azure Content Understanding**, applies routing gates,
and records the outcome on a Table Storage ledger. The flow then writes approved
invoices to **Dynamics 365 CE** or routes the rest to a SharePoint review queue.

This repo is one Git repository with several distinct concerns. Open the **repo
root** as the VS Code workspace.

## Layout

```
noble-invoice-process/
├─ functionapp/        # the ONLY thing that deploys to Azure (open root, deploy this)
│  ├─ function_app.py  # HTTP-triggered v2 entry point — must stay at this folder root
│  ├─ gates.py         # routing gates (B2 / B4 / no-child); imports field_policy
│  ├─ field_policy.py  # critical-field rules, threshold, date/amount write-values (single source of truth)
│  ├─ cu_client.py     # Content Understanding binary/url wrapper
│  ├─ ledger.py        # InvoiceExtractProcessLog client (keying, atomic claim, upsert)
│  ├─ host.json, requirements.txt
│  ├─ local.settings.json.template   # copy to local.settings.json (gitignored) for local runs
│  └─ .funcignore
├─ scripts/            # standalone prototype harnesses — NOT deployed
│  ├─ verify_fn.py             # posts one PDF to the Function (local func start by default, deployed with --base-url/--key)
│  ├─ create_analyzer.py       # (re)provisions a CU analyzer from analyzers/*.json
│  └─ scorecard.py             # scoring/output helper (imported by verify_fn.py)
├─ tests/              # offline pytest suite (no Azure) — run with `python -m pytest`
│  ├─ test_field_policy_gates.py  # gates.py + field_policy.py assertions
│  └─ test_harness_scorecard.py   # harness scorecards must agree with the Function's B4 gate
├─ analyzers/          # CU analyzer provisioning JSONs (generalinvoice, invoicerouter)
├─ docs/               # design PDFs (Design Reference, Detailed System Design, Prototype Plan)
├─ samples/            # real invoice PDFs — gitignored
├─ out/                # result JSON + HTML scorecards, captured output — gitignored
├─ pytest.ini          # scopes pytest collection to tests/
├─ .vscode/settings.json
└─ .gitignore
```

**Why this shape.** Only `functionapp/` is a deployable Azure Functions project, so
`func publish` from there zips a clean unit and never ships harnesses, PDFs, or
sample invoices. The shared logic (`gates.py`, `field_policy.py`, `ledger.py`,
`cu_client.py`) lives in exactly one place — the function — and the harnesses and
tests import it rather than carrying their own copies, so a threshold or gate
change happens once. Do not move
`function_app.py` into a `src/` subfolder; the host expects it at the project root.

## One-time setup

```cmd
cd noble-invoice-process
py -m venv .venv
.venv\Scripts\activate
pip install -r functionapp\requirements.txt
pip install pytest truststore
copy functionapp\local.settings.json.template functionapp\local.settings.json
```

`pytest` and `truststore` are installed separately and on purpose. They are **not** in
`functionapp\requirements.txt` because that file is what Azure rebuilds from server-side, and
neither belongs on the Function host — `truststore` is a local workaround for TLS-inspecting
proxies. Without them, `python -m pytest` and the pre-commit hook fail on a fresh checkout.

The project targets **Python 3.14** (migrated from 3.13 on 2026-08-20); the Function App runs the
same version. See `docs/python-314-migration.html`.

Put the CU key in `AZURE_CU_KEY` in `local.settings.json` (or leave it blank to use
your `az login` identity), and set `AZURE_STORAGE_ACCOUNT`. `local.settings.json` is
gitignored — secrets never get committed.

## Run the Function locally

```cmd
cd functionapp
func start
```

Then, from another shell (no `--base-url` needed — it defaults to the `func start` host):

```cmd
cd scripts
python verify_fn.py --file "..\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77
```

## Run the offline tests

The gate/policy suite has no Azure dependency. From the repo root:

```cmd
python -m pytest
```

`pytest.ini` scopes collection to `tests/`. The suite puts `functionapp/` on
`sys.path` itself, so it exercises the same `gates.py` / `field_policy.py` the
Function runs. You can also run it standalone:
`python tests\test_field_policy_gates.py`.

## Run the harness

`verify_fn.py` imports the canonical modules from `functionapp/` via a small
`sys.path` bootstrap at the top of the file, so the gate thresholds it scores
with are exactly the ones the Function runs. It targets the local `func start`
host by default; pass `--base-url`/`--key` for the deployed Function.
`create_analyzer.py` hits live Azure and needs credentials
(a function key / your `az login` identity).

```cmd
cd scripts
python verify_fn.py --base-url "https://<function-app>.azurewebsites.net" --key "<FUNCTION_KEY>" --file "..\samples\invoice1.pdf"
```

## Deploy

```cmd
az account set --subscription "dev-Document Intelligence"
cd functionapp
func azure functionapp publish <function-app-name>
```

or use the VS Code Azure Functions extension's **Deploy** (the `.vscode/settings.json`
points it at the `functionapp` subfolder). With `scmDoBuildDuringDeployment` enabled,
Azure rebuilds from `requirements.txt`, so the venv is never shipped.

See `functionapp/README.md` for the request/response contract, the gate A1 semantics,
the authentication model, and the Power Automate integration steps.

## Security

Secrets live only in `functionapp/local.settings.json` (gitignored) locally, and as
Key Vault references in the deployed app — never in a tracked `.py`. The CU key that
was previously hardcoded in `step24_test.py` has been removed; supply it via
`AZURE_CU_KEY` or `--key`. Regenerate that key in the Foundry portal if it was ever
committed.