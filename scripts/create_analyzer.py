"""
create_analyzer.py — (re)provision a Content Understanding analyzer from a JSON
definition in analyzers/.

Editing an analyzers/*.json file changes nothing in the CU service on its own;
run this to push the definition so the change takes effect. By default it
provisions the general invoice analyzer (the one that owns po_or_job_number).

Endpoint/auth mirror functionapp/cu_client.py: AZURE_CU_KEY if set, otherwise
DefaultAzureCredential (your `az login` locally, managed identity in Azure);
the endpoint comes from --endpoint or AZURE_CU_ENDPOINT (required — no baked-in
default, so the script cannot silently target the wrong environment);
api-version comes from the same env var or default as cu_client. The
identity needs 'Cognitive Services User' on the CU resource. allow_replace=True,
so an existing analyzer with the same id is overwritten.

--load-local-settings seeds AZURE_CU_* (endpoint, key, api-version) from
functionapp/local.settings.json when unset — the same convenience flag regress.py
has. Use it on a dev box behind a TLS inspector that blocks every
DefaultAzureCredential token path, where the CU key is the only working auth.

Run from the repo root (use --load-local-settings for key auth on this dev
machine; omit it where `az login` works or in Azure via managed identity):
    .\\.venv\\Scripts\\python.exe scripts\\create_analyzer.py --load-local-settings
    .\\.venv\\Scripts\\python.exe scripts\\create_analyzer.py --load-local-settings --file analyzers\\create-router-analyzer.json --analyzer-id invoicerouter
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

# TLS: this dev machine sits behind a TLS-inspecting agent that OpenSSL
# strict-rejects; verify against the Windows store instead, like func/.NET (see
# docs/ai/troubleshooting.md). No-op where truststore is absent (e.g. in Azure).
os.environ.pop("REQUESTS_CA_BUNDLE", None)
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - truststore is a dev-only dependency
    pass

# Reuse the canonical CU endpoint/api-version from functionapp/cu_client.py so
# this script targets exactly what the Function uses.
_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent
_FUNCTIONAPP = _REPO / "functionapp"
if _FUNCTIONAPP.is_dir() and str(_FUNCTIONAPP) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONAPP))

import cu_client  # noqa: E402  -- imported after the sys.path bootstrap above


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(_REPO), capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def _require_green_regression() -> None:
    """Abort a PROD general-analyzer push unless a green regression run for the
    current clean HEAD is on file. This is the structural guard against pushing a
    prompt to production without the golden-corpus check (scripts/regress.py) --
    the exact gap that let the deposit/balance-forward bugs ship. Scratch, test,
    and router pushes never reach here. --force overrides.
    """
    sha = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    stamp = _REPO / "out" / "regress" / f"{sha}.json"
    hint = (
        "Run the golden-corpus regression first, then re-push:\n"
        "    .\\.venv\\Scripts\\python.exe scripts\\regress.py\n"
        "(or pass --force to override, e.g. an emergency roll-back)."
    )
    if not sha:
        raise SystemExit("Refusing prod push: not in a git repo / no HEAD.\n" + hint)
    if dirty:
        raise SystemExit(
            "Refusing prod push: working tree is dirty, so a regression stamp would "
            "not describe what you are pushing. Commit or stash first.\n" + hint
        )
    if not stamp.is_file():
        raise SystemExit(
            f"Refusing prod push: no green regression stamp for HEAD ({sha[:12]}).\n" + hint
        )
    print(f"Prod push gate: green regression stamp found for HEAD {sha[:12]}.")


def _build_credential():
    """Key auth if AZURE_CU_KEY is set; otherwise DefaultAzureCredential -- the
    same choice cu_client makes (az login locally, managed identity in Azure)."""
    key = os.getenv("AZURE_CU_KEY")
    if key:
        from azure.core.credentials import AzureKeyCredential

        return AzureKeyCredential(key)
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _load_local_settings() -> None:
    """Seed AZURE_CU_* from functionapp/local.settings.json when not already set.
    Mirrors regress.py's flag so a prod push needs no manual env seeding on this dev
    machine, where the CU key is the only auth that works behind the TLS inspector.
    Never overrides an env var already present, so an explicit shell or CI still wins."""
    settings = _FUNCTIONAPP / "local.settings.json"
    if not settings.is_file():
        return
    # utf-8-sig: local.settings.json is often written by PowerShell with a BOM.
    values = json.loads(settings.read_text(encoding="utf-8-sig")).get("Values", {})
    for key in ("AZURE_CU_ENDPOINT", "AZURE_CU_KEY", "AZURE_CU_API_VERSION"):
        if not os.getenv(key) and values.get(key):
            os.environ[key] = values[key]


def main() -> int:
    ap = argparse.ArgumentParser(description="(Re)provision a CU analyzer from a JSON definition.")
    ap.add_argument(
        "--file",
        default=str(_REPO / "analyzers" / "create-generalinvoice-analyzer.json"),
        help="Analyzer definition JSON. Default: analyzers/create-generalinvoice-analyzer.json",
    )
    ap.add_argument(
        "--analyzer-id",
        default=cu_client.general_invoice_analyzer_id(),
        help="Analyzer id to create/replace in CU. Default: %(default)s",
    )
    ap.add_argument("--endpoint", default=None,
                    help="CU endpoint. Default: the AZURE_CU_ENDPOINT environment variable (required if this flag is omitted).")
    ap.add_argument("--api-version", default=None,
                    help=f"CU API version. Default: the AZURE_CU_API_VERSION environment variable, else {cu_client.DEFAULT_API_VERSION}.")
    ap.add_argument("--force", action="store_true",
                    help="skip the prod push gate (a green regression stamp for a clean HEAD). "
                         "Only affects a push to the prod general-invoice id.")
    ap.add_argument("--load-local-settings", action="store_true",
                    help="seed AZURE_CU_* env (endpoint, key, api-version) from functionapp/local.settings.json "
                         "when unset. Dev convenience for key auth behind a TLS inspector that blocks az login.")
    args = ap.parse_args()

    # Seed key auth (and endpoint/api-version) before resolving anything, so a dev box behind
    # the TLS inspector needs no manual env seeding. Never overrides values already in the env.
    if args.load_local_settings:
        _load_local_settings()

    if not args.endpoint:
        args.endpoint = cu_client.endpoint()  # raises with a clear message if AZURE_CU_ENDPOINT is unset
    if not args.api_version:
        args.api_version = cu_client.api_version()

    # Prod push gate: only a push to the literal prod general-invoice id is gated;
    # scratch / test / router pushes are unaffected. Checked against the constant,
    # not the env-overridable id, so the gate can't be dodged via an env var.
    if args.analyzer_id == cu_client.DEFAULT_GENERAL_ANALYZER_ID and not args.force:
        _require_green_regression()

    path = pathlib.Path(args.file)
    if not path.is_file():
        raise SystemExit(f"Definition file not found: {path}")
    definition = json.loads(path.read_text(encoding="utf-8"))

    from azure.ai.contentunderstanding import ContentUnderstandingClient
    from azure.core.exceptions import AzureError

    client = ContentUnderstandingClient(
        endpoint=args.endpoint,
        credential=_build_credential(),
        api_version=args.api_version,
    )

    print(f"Provisioning analyzer '{args.analyzer_id}' at {args.endpoint}")
    print(f"  from {path}")
    print("  allow_replace=True -- overwrites any existing analyzer with this id")
    try:
        poller = client.begin_create_analyzer(args.analyzer_id, resource=definition, allow_replace=True)
        result = poller.result()
    except AzureError as exc:
        raise SystemExit(f"CU provisioning failed: {exc}")
    finally:
        client.close()

    status = getattr(result, "status", None)
    print(f"Done. analyzer_id={getattr(result, 'analyzer_id', args.analyzer_id)}"
          + (f", status={status}" if status else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
