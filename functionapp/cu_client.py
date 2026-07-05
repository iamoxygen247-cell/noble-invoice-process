"""
cu_client.py — Content Understanding wrapper for the decision engine.

Uses the exact GA SDK calls validated in step24_test.py:
  - ContentUnderstandingClient(endpoint, credential, api_version)
  - begin_analyze_binary(analyzer_id, binary_input, content_type)  [primary]
  - begin_analyze(analyzer_id, inputs=[AnalysisInput(url=...)])     [optional]
  - poller.result(); result.as_dict() -> the full result with 'contents' at top level

Transport: BINARY is the production path for this pipeline (the Logic App reads
the SharePoint bytes and the Function POSTs them to CU). A url path is kept only
for ad-hoc testing with a Blob SAS URL; it is not used in production.

Auth: key auth (AZURE_CU_KEY) is the validated default. If no key is set, the
client falls back to DefaultAzureCredential (managed identity in Azure). For the
AAD path the calling identity needs the 'Cognitive Services User' role on the
Foundry resource; the endpoint must have a custom subdomain (it does). Key auth
via a Key Vault reference is recommended for the prototype because it is what has
been validated end to end.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any, Dict, Optional

DEFAULT_API_VERSION = "2025-11-01"
DEFAULT_ROUTER_ANALYZER_ID = "invoicerouter"
DEFAULT_GENERAL_ANALYZER_ID = "generalinvoice"
# Cap on the analyze long-running operation. CU runs complete in seconds; without
# a cap a hung LRO holds the invocation until the platform kills it and leaves the
# A1-claimed ledger row blocking reprocessing until the lease expires.
DEFAULT_ANALYZE_TIMEOUT_SECONDS = 120.0


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def endpoint() -> str:
    # Deliberately no default: the endpoint names an environment, and a silent
    # fallback would let a misconfigured app call another environment's CU.
    value = os.getenv("AZURE_CU_ENDPOINT")
    if not value:
        raise RuntimeError(
            "AZURE_CU_ENDPOINT is not set; set it to this environment's Content "
            "Understanding endpoint, e.g. https://<resource>.services.ai.azure.com/"
        )
    return value.rstrip("/") + "/"


def api_version() -> str:
    return _env("AZURE_CU_API_VERSION", DEFAULT_API_VERSION)


def router_analyzer_id() -> str:
    return _env("AZURE_CU_ANALYZER_ID", DEFAULT_ROUTER_ANALYZER_ID)


def general_invoice_analyzer_id() -> str:
    return _env("AZURE_CU_GENERAL_ANALYZER_ID", DEFAULT_GENERAL_ANALYZER_ID)


def analyze_timeout_seconds() -> float:
    """Analyze LRO timeout, overridable via AZURE_CU_TIMEOUT_SECONDS. A missing,
    non-numeric, or non-positive value falls back to the default."""
    raw = _env("AZURE_CU_TIMEOUT_SECONDS", str(DEFAULT_ANALYZE_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ANALYZE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_ANALYZE_TIMEOUT_SECONDS


def _build_credential():
    """Key auth if AZURE_CU_KEY is set; otherwise managed identity / AAD."""
    key = os.getenv("AZURE_CU_KEY")
    if key:
        from azure.core.credentials import AzureKeyCredential

        return AzureKeyCredential(key)

    from azure.identity import DefaultAzureCredential

    # AZURE_CLIENT_ID disambiguates when several managed identities are present.
    return DefaultAzureCredential()


def _client():
    from azure.ai.contentunderstanding import ContentUnderstandingClient

    return ContentUnderstandingClient(
        endpoint=endpoint(),
        credential=_build_credential(),
        api_version=api_version(),
    )


def _as_dict(result: Any) -> Dict[str, Any]:
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    return dict(result)


def _poll_result(poller: Any) -> Dict[str, Any]:
    """Wait for the analyze LRO with a hard cap. LROPoller.wait(timeout) returns
    (without raising) when the timeout elapses before completion, so done() is
    the reliable signal; raise TimeoutError so the Function returns a clean 502
    instead of hanging until the host kills the invocation."""
    timeout = analyze_timeout_seconds()
    poller.wait(timeout=timeout)
    if not poller.done():
        raise TimeoutError(
            f"Content Understanding analyze did not complete within {timeout:.0f}s"
        )
    return _as_dict(poller.result())


def _guess_content_type(file_name: Optional[str]) -> str:
    if file_name:
        guessed, _ = mimetypes.guess_type(file_name)
        if guessed:
            return guessed
        if file_name.lower().endswith(".pdf"):
            return "application/pdf"
    return "application/pdf"


def analyze_binary(content_bytes: bytes, file_name: Optional[str] = None) -> Dict[str, Any]:
    """Submit raw document bytes to the router analyzer and return the full result dict."""
    client = _client()
    content_type = _guess_content_type(file_name)
    analyzer_id = router_analyzer_id()

    # The installed SDK may or may not accept content_type; mirror step24's fallback.
    try:
        poller = client.begin_analyze_binary(
            analyzer_id=analyzer_id,
            binary_input=content_bytes,
            content_type=content_type,
        )
    except TypeError:
        poller = client.begin_analyze_binary(
            analyzer_id=analyzer_id,
            binary_input=content_bytes,
        )
    return _poll_result(poller)


def analyze_url(url: str) -> Dict[str, Any]:
    """Optional: submit a Blob SAS URL (ad-hoc testing only, not the production path)."""
    from azure.ai.contentunderstanding.models import AnalysisInput

    client = _client()
    poller = client.begin_analyze(
        analyzer_id=router_analyzer_id(),
        inputs=[AnalysisInput(url=url)],
    )
    return _poll_result(poller)
