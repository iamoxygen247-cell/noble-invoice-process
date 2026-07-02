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
│  ├─ local_test.py            # posts one PDF to the running Function (imports functionapp/gates.py)
│  ├─ verify_fn.py             # smoke-test caller for the deployed Function
│  ├─ create_analyzer.py       # (re)provisions a CU analyzer from analyzers/*.json
│  └─ scorecard.py             # scoring/output helper (imported by local_test.py and verify_fn.py)
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
copy functionapp\local.settings.json.template functionapp\local.settings.json
```

Put the CU key in `AZURE_CU_KEY` in `local.settings.json` (or leave it blank to use
your `az login` identity), and set `AZURE_STORAGE_ACCOUNT`. `local.settings.json` is
gitignored — secrets never get committed.

## Run the Function locally

```cmd
cd functionapp
func start
```

Then, from another shell:

```cmd
cd scripts
python local_test.py --file "..\samples\invoice1.pdf" --source-id 0fb9c2a1-7d3e-4a55-9c10-2b8e6f4a1d77
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

## Run the harnesses

`local_test.py` and `verify_fn.py` import the canonical modules from
`functionapp/` via a small `sys.path` bootstrap at the top of each file, so the
gate thresholds they score with are exactly the ones the Function runs.
`verify_fn.py` and `create_analyzer.py` hit live Azure and need credentials
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