"""
test_field_policy_gates.py — offline verification suite.

Encodes the agreed v8 bill-type requirements as assertions over synthetic
Content Understanding results. No Azure dependency: exercises only gates.py +
field_policy.py, matching the "gates are offline-testable" principle.

Run under pytest (the project standard), from the repo root:
    .\\.venv\\Scripts\\python.exe -m pytest

Or standalone:
    .\\.venv\\Scripts\\python.exe tests\\test_field_policy_gates.py
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime

# gates.py / field_policy.py are the single source of truth and live in
# functionapp/. Put that folder on sys.path before importing them so this suite
# resolves the same modules the Function runs, under both pytest and a standalone
# run. (No conftest.py: this one shim covers both runners.)
_FUNCTIONAPP = pathlib.Path(__file__).resolve().parent.parent / "functionapp"
if str(_FUNCTIONAPP) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONAPP))

import field_policy  # noqa: E402  -- imported after the sys.path bootstrap above
import gates  # noqa: E402

THRESHOLD = field_policy.THRESHOLD  # 0.73
FIXED_NOW = datetime(2026, 6, 29, 10, 0, 0, tzinfo=field_policy.BUSINESS_TZ)
TODAY = "2026-06-29"
DUE_30 = "2026-07-29"


def check(label: str, cond: bool, detail: str = "") -> None:
    assert cond, f"{label}" + (f"  [{detail}]" if detail else "")


# --- CU field / result builders ----------------------------------------------


def fstr(value, conf):
    return {"valueString": value, "confidence": conf}


def fnum(value, conf):
    return {"valueNumber": value, "confidence": conf}


def fdate(value, conf):
    return {"valueDate": value, "confidence": conf}


def cu_result(fields, category="general_invoice", analyzer="generalinvoice",
              markdown=None, router_markdown=None):
    """contents[0] = router result (segment category); contents[1] = child fields.
    markdown / router_markdown attach OCR markdown to the child / router entry."""
    router = {"segments": [{"category": category}]}
    child = {"analyzerId": analyzer, "category": category, "fields": fields}
    if router_markdown is not None:
        router["markdown"] = router_markdown
    if markdown is not None:
        child["markdown"] = markdown
    return {"contents": [router, child]}


def commercial_fields(**overrides):
    base = {
        "vendor_name_extract": fstr("Bob's Plumbing Ltd.", 0.97),
        "service_address_extract": fstr("123 Main St, Vancouver BC", 0.95),
        "total_invoice_amount_extract": fnum(105.0, 0.96),
        "po_or_job_number_extract": fstr("11024580", 0.91),
        "gst_amount_extract": fnum(5.0, 0.93),
        "invoice_date": fdate("2026-05-01", 0.95),
        "payment_due_date": fdate("2026-05-31", 0.94),
        "invoice_number_extract": fstr("INV-2201", 0.92),
        "bill_type": fstr("commercial", 0.9),
        "is_handwritten": fstr("no", 0.97),
        "invoice_description": fstr("Electrical repair work.", 0.8),
        "anomaly_flag": fstr("", None),
    }
    base.update(overrides)
    return base


def municipal_fields(**overrides):
    base = {
        "vendor_name_extract": fstr("City of Vancouver", 0.98),
        "service_address_extract": fstr("456 Oak Ave, Vancouver BC", 0.95),
        "total_invoice_amount_extract": fnum(220.0, 0.96),
        # municipal bills usually carry no PO and no GST, but must carry the
        # biller's account number and an invoice/licence number (municipal delta)
        "account_number_extract": fstr("123456789012", 0.95),
        "invoice_number_extract": fstr("BL-123456", 0.93),
        "invoice_date": fdate("2026-05-10", 0.95),
        "payment_due_date": fdate("2026-06-10", 0.94),
        "bill_type": fstr("municipal", 0.9),
        "is_handwritten": fstr("no", 0.97),
        "invoice_description": fstr("Annual business license renewal.", 0.8),
        "anomaly_flag": fstr("", None),
    }
    base.update(overrides)
    return base


def ev(fields, category="general_invoice", file_name=""):
    return gates.evaluate(cu_result(fields, category=category), THRESHOLD, file_name=file_name)


# --- field_policy unit checks ------------------------------------------------


def test_policy_constants_and_buckets():
    print("\n[field_policy: buckets and criticality]")
    check("threshold is 0.73", THRESHOLD == 0.73, str(THRESHOLD))
    check("base critical = vendor/address/total",
          field_policy.BASE_CRITICAL == ("vendor_name", "service_address", "total_invoice_amount"))
    check("commercial delta = po + gst",
          field_policy.COMMERCIAL_DELTA == ("po_or_job_number", "gst_amount"))
    check("municipal delta = account + invoice number",
          field_policy.MUNICIPAL_DELTA == ("account_number", "invoice_number"))

    commercial = field_policy.critical_fields("commercial")
    municipal = field_policy.critical_fields("municipal")
    check("commercial critical has 5 fields incl po + gst",
          set(commercial) == {"vendor_name", "service_address", "total_invoice_amount",
                              "po_or_job_number", "gst_amount"}, str(commercial))
    check("commercial critical excludes account/invoice number",
          "account_number" not in commercial and "invoice_number" not in commercial, str(commercial))
    check("municipal critical = base + account + invoice number (no po, no gst)",
          set(municipal) == {"vendor_name", "service_address", "total_invoice_amount",
                             "account_number", "invoice_number"}, str(municipal))

    # resolve_bucket: only explicit 'municipal' relaxes; everything else is strict.
    check("resolve 'municipal' -> municipal", field_policy.resolve_bucket("municipal") == "municipal")
    check("resolve 'Municipal' (case) -> municipal", field_policy.resolve_bucket("Municipal") == "municipal")
    check("resolve 'commercial' -> commercial", field_policy.resolve_bucket("commercial") == "commercial")
    check("resolve None -> commercial (strict default)", field_policy.resolve_bucket(None) == "commercial")
    check("resolve '' -> commercial", field_policy.resolve_bucket("") == "commercial")
    check("resolve unexpected label -> commercial", field_policy.resolve_bucket("government") == "commercial")


def test_date_defaulting_and_derivation():
    print("\n[field_policy: date defaulting + derived amount]")
    # both dates good, high conf -> kept, normalised; amount derived
    parsed = gates.parse_fields(commercial_fields())
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("good invoice_date kept", wv["invoice_date"] == "2026-05-01", wv["invoice_date"])
    check("good payment_due_date kept", wv["payment_due_date"] == "2026-05-31", wv["payment_due_date"])
    check("no defaults when both reliable", defaulted == [], str(defaulted))
    check("amount_excluding_gst derived (105 - 5)", wv["amount_excluding_gst"] == 100.0, str(wv["amount_excluding_gst"]))

    # missing invoice_date -> today; low-conf payment_due_date -> today+30
    parsed = gates.parse_fields(commercial_fields(
        invoice_date=fdate(None, None),
        payment_due_date=fdate("2026-05-31", 0.40),
    ))
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("missing invoice_date -> today (PST)", wv["invoice_date"] == TODAY, wv["invoice_date"])
    check("low-conf payment_due_date -> today+30", wv["payment_due_date"] == DUE_30, wv["payment_due_date"])
    check("both dates recorded as defaulted",
          set(defaulted) == {"invoice_date", "payment_due_date"}, str(defaulted))

    # date confidence exactly at threshold is reliable (>=)
    parsed = gates.parse_fields(commercial_fields(invoice_date=fdate("2026-05-01", THRESHOLD)))
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("date conf == threshold is kept (>=)",
          wv["invoice_date"] == "2026-05-01" and "invoice_date" not in defaulted)

    # non-ISO but unambiguous month-name normalises to YYYY-MM-DD
    parsed = gates.parse_fields(commercial_fields(invoice_date=fdate("May 1, 2026", 0.95)))
    wv, _ = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("month-name date normalised to YYYY-MM-DD", wv["invoice_date"] == "2026-05-01", wv["invoice_date"])

    # ambiguous numeric date treated as unparseable -> defaulted (safer than wrong guess)
    parsed = gates.parse_fields(commercial_fields(invoice_date=fdate("03/04/2026", 0.95)))
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("ambiguous numeric date -> defaulted to today",
          wv["invoice_date"] == TODAY and "invoice_date" in defaulted, wv["invoice_date"])

    # municipal with no GST -> amount_excluding_gst is None (not an error)
    parsed = gates.parse_fields(municipal_fields())
    wv, _ = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("no GST -> amount_excluding_gst None", wv["amount_excluding_gst"] is None, str(wv["amount_excluding_gst"]))

    # all output dates match YYYY-MM-DD shape
    import re
    ok = re.fullmatch(r"\d{4}-\d{2}-\d{2}", wv["invoice_date"]) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", wv["payment_due_date"])
    check("municipal output dates are YYYY-MM-DD", bool(ok))


# --- commercial routing ------------------------------------------------------


def test_commercial_routing():
    print("\n[gates: commercial bucket]")
    r = ev(commercial_fields())
    check("commercial happy path", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("bucket resolved commercial", r["policyBucket"] == "commercial")
    check("billType surfaced", r["billType"] == "commercial")
    check("policyVersion stamped", r["policyVersion"] == field_policy.POLICY_VERSION)

    # PO missing -> review (po is critical for commercial)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("", None)))
    check("commercial missing PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("review reason mentions po", any("po_or_job_number" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))

    # PO low confidence -> review
    r = ev(commercial_fields(po_or_job_number_extract=fstr("JOB-1", 0.50)))
    check("commercial low-conf PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # GST missing -> review (gst critical for commercial; agreed)
    r = ev(commercial_fields(gst_amount_extract=fnum(None, None)))
    check("commercial missing GST -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("review reason mentions gst", any("gst_amount" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))

    # base field low confidence -> review
    r = ev(commercial_fields(vendor_name_extract=fstr("Bob", 0.60)))
    check("commercial low-conf vendor -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # threshold boundary: 0.73 passes, 0.72 fails
    r = ev(commercial_fields(total_invoice_amount_extract=fnum(105.0, 0.73)))
    check("conf == 0.73 passes", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    r = ev(commercial_fields(total_invoice_amount_extract=fnum(105.0, 0.72)))
    check("conf == 0.72 fails", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # po_or_job_number format: exactly 8 digits starting 110/330 (analyzer prompt + B4 gate)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("11024580", 0.95)))
    check("commercial 110-prefixed PO -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    r = ev(commercial_fields(po_or_job_number_extract=fstr("33001022", 0.95)))
    check("commercial 330-prefixed PO -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("JOB-4471", 0.95)))
    check("commercial alphanumeric PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("PO format review reason names the field",
          any("po_or_job_number" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))
    r = ev(commercial_fields(po_or_job_number_extract=fstr("1234567", 0.95)))
    check("commercial 7-digit PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("123456789", 0.95)))
    check("commercial 9-digit PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("12345678", 0.95)))
    check("commercial 8-digit PO without 110/330 prefix -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])


# --- municipal routing -------------------------------------------------------


def test_municipal_routing():
    print("\n[gates: municipal bucket]")
    r = ev(municipal_fields())
    check("municipal happy path (no PO/GST present)", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("bucket resolved municipal", r["policyBucket"] == "municipal")

    # invoice_number is municipal-critical (bill-type-v2): absent/empty -> review
    r = ev(municipal_fields(invoice_number_extract=fstr("", None)))
    check("municipal missing invoice_number -> review (municipal-critical)",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])

    # municipal base field missing -> review
    r = ev(municipal_fields(service_address_extract=fstr("", None)))
    check("municipal missing service_address -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # municipal with garbled GST present but low conf -> STILL happy (gst not critical for municipal)
    r = ev(municipal_fields(gst_amount_extract=fnum(99.0, 0.10)))
    check("municipal low-conf GST -> still happy (gst not critical here)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

    # po format is enforced only where po is critical (commercial); a municipal
    # bill with a malformed po is unaffected (po is not in its critical set)
    r = ev(municipal_fields(po_or_job_number_extract=fstr("JOB-4471", 0.95)))
    check("municipal malformed PO -> still happy (po not critical here)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)


# --- conservative default + known residual -----------------------------------


def test_default_and_residual():
    print("\n[gates: conservative default + documented residual]")
    # bill_type missing -> strict (commercial). A bill with no PO/GST then reviews.
    fields = municipal_fields(bill_type=fstr("", None))  # municipal-looking but unlabeled, no PO/GST
    r = ev(fields)
    check("missing bill_type -> commercial bucket (strict)", r["policyBucket"] == "commercial")
    check("unlabeled bill lacking PO/GST -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # The municipal delta narrows the old residual: a trade invoice (no PO/GST, no
    # account or invoice number) mislabeled municipal is now caught by the delta
    # requirement instead of auto-writing.
    trade_as_municipal = {
        "vendor_name_extract": fstr("Bob's Plumbing Ltd.", 0.97),
        "service_address_extract": fstr("123 Main St", 0.95),
        "total_invoice_amount_extract": fnum(105.0, 0.96),
        "bill_type": fstr("municipal", 0.9),  # WRONG label
        "is_handwritten": fstr("no", 0.97),
    }
    r = ev(trade_as_municipal)
    check("mislabeled trade lacking account/invoice numbers -> review (delta defends)",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])

    # REMAINING RESIDUAL: a mislabeled trade invoice that ALSO carries account- and
    # invoice-like values still auto-writes. Defended only at the prompt layer
    # (issuer-not-customer tie-breaker), never by this gate.
    r = ev(dict(trade_as_municipal,
                account_number_extract=fstr("55-1234", 0.9),
                invoice_number_extract=fstr("INV-8801", 0.9)))
    check("RESIDUAL: mislabeled trade WITH account+invoice numbers auto-writes (known)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])


# --- handwriting retired, B2 reject, no-child --------------------------------


def test_handwriting_b2_nochild():
    print("\n[gates: handwriting advisory, B2 reject, no-child]")
    # handwritten = yes but criticals clear -> auto-writes (B3 retired)
    r = ev(commercial_fields(is_handwritten=fstr("yes", 0.99)))
    check("handwritten=yes still auto-writes (B3 retired)", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    check("is_handwritten surfaced as advisory", r["isHandwritten"] == "yes")
    check("is_handwritten confidence surfaced", r["isHandwrittenConfidence"] == 0.99)

    # undetermined handwriting -> still auto-writes (full retirement per agreement)
    r = ev(commercial_fields(is_handwritten=fstr("", None)))
    check("undetermined handwriting -> auto-writes (full retirement)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

    # category other -> reject
    r = ev(commercial_fields(), category="other")
    check("category other -> reject", r["routingDecision"] == gates.REJECT_B2_OTHER_CATEGORY)
    check("reject effectiveDocumentType is other", r["effectiveDocumentType"] == "other")

    # no child fields -> review_no_child
    no_child = {"contents": [{"segments": [{"category": "general_invoice"}]}]}
    r = gates.evaluate(no_child, THRESHOLD)
    check("no child extraction -> review", r["routingDecision"] == gates.REVIEW_NO_CHILD_EXTRACTION)


# --- response shape: no removed blocks, write block present ------------------


def test_response_shape():
    print("\n[response shape: agreed additions/removals]")
    r = ev(commercial_fields())
    check("has writeValues block", "writeValues" in r)
    check("has defaultedFields", "defaultedFields" in r)
    check("has billType / policyBucket / policyVersion",
          all(k in r for k in ("billType", "policyBucket", "policyVersion")))
    check("NO gst math block (gate removed)", "gst" not in r)
    check("NO vendorCategory (replaced by billType)", "vendorCategory" not in r)
    check("writeValues carries derived amount_excluding_gst", "amount_excluding_gst" in r["writeValues"])
    # fields.vendor_name is the computed final (value + effective confidence); the raw
    # extraction is carried separately as fields.vendor_name_extract.
    check("fields.vendor_name is the resolved final value",
          r["fields"]["vendor_name"]["value"] == "Bob's Plumbing Ltd.", str(r["fields"].get("vendor_name")))
    check("fields.vendor_name carries effective confidence",
          r["fields"]["vendor_name"]["confidence"] == 0.97)
    check("fields.vendor_name_extract carries the raw extraction",
          r["fields"]["vendor_name_extract"]["confidence"] == 0.97, str(r["fields"].get("vendor_name_extract")))
    check("vendorResolution block present with source",
          isinstance(r["resolutions"].get("vendor_name"), dict) and r["resolutions"]["vendor_name"]["source"] == "extract",
          str(r["resolutions"].get("vendor_name")))
    check("fields.service_address is the resolved final value",
          r["fields"]["service_address"]["value"] == "123 Main St, Vancouver BC", str(r["fields"].get("service_address")))
    check("fields.service_address_extract carries the raw extraction",
          r["fields"]["service_address_extract"]["confidence"] == 0.95, str(r["fields"].get("service_address_extract")))
    check("serviceAddressResolution block present with source",
          isinstance(r["resolutions"].get("service_address"), dict) and r["resolutions"]["service_address"]["source"] == "extract",
          str(r["resolutions"].get("service_address")))
    # invoice_number is twinned now: the final is resolved from the extract.
    check("fields.invoice_number is the resolved final value",
          r["fields"]["invoice_number"]["value"] == "INV-2201", str(r["fields"].get("invoice_number")))
    check("invoiceNumberResolution source = extract",
          r["resolutions"]["invoice_number"]["source"] == "extract", str(r["resolutions"].get("invoice_number")))
    # account_number final + resolution are injected even when the twins are absent
    # (this commercial fixture carries no account number).
    check("fields.account_number final injected", "account_number" in r["fields"], str(list(r["fields"])))
    check("accountNumberResolution present with source none (absent, non-critical here)",
          r["resolutions"]["account_number"]["source"] == "none"
          and r["resolutions"]["account_number"]["passed"] is False,
          str(r["resolutions"].get("account_number")))
    # decision enum is exactly the agreed reduced set
    allowed = {gates.HAPPY_PATH_CANDIDATE, gates.REVIEW_B4_CRITICAL_FIELD,
               gates.REJECT_B2_OTHER_CATEGORY, gates.REVIEW_NO_CHILD_EXTRACTION}
    check("routingDecision in reduced enum", r["routingDecision"] in allowed)


def test_field_format_rules():
    print("\n[field_policy: per-field format rules]")
    vr = field_policy.format_violation_reason
    check("110-prefixed 8-digit po ok", vr("po_or_job_number", "11024580") is None)
    check("330-prefixed 8-digit po ok", vr("po_or_job_number", "33001022") is None)
    check("8-digit po without 110/330 prefix violates", vr("po_or_job_number", "12345678") is not None)
    check("leading-zero 8-digit po violates (not 110/330)", vr("po_or_job_number", "00471234") is not None)
    check("7-digit po violates", vr("po_or_job_number", "1102458") is not None)
    check("9-digit po violates (0008's label artifact)", vr("po_or_job_number", "330001022") is not None)
    check("spaced po violates on the resolved value", vr("po_or_job_number", "1102 4580") is not None)
    check("alphanumeric po violates", vr("po_or_job_number", "JOB-4471") is not None)
    check("empty po is not a format violation", vr("po_or_job_number", "") is None)
    check("None po is not a format violation", vr("po_or_job_number", None) is None)
    check("field without a rule is always ok", vr("vendor_name", "anything") is None)
    check("account_number has no format rule", vr("account_number", "12345-001") is None)
    check("invoice_number has no format rule", vr("invoice_number", "INV-2201") is None)


def test_find_po_candidates():
    print("\n[field_policy: OCR-text PO candidate scanner]")
    find = field_policy.find_po_candidates
    check("labeled Job# found", find("Job# 11024580") == ["11024580"])
    check("bare # next to customer name found",
          find("NOBLE & ASSOCIATES PROPERTY MANAGEMENT # 11022266") == ["11022266"])
    check("bracketed duplicates dedupe to one",
          find("[Job# 11024565] work done ... [Job# 11024565]") == ["11024565"])
    check("9-digit label artifact skipped, true value found (0008 shape)",
          find("Customer PO No. : 330001022\nSouth Fraser Plaza - Contract #33001022") == ["33001022"])
    check("spaced digits normalised", find("PO 1102 4580 due") == ["11024580"])
    check("spaced 9-digit run rejected", find("ref 3300 01022 x") == [])
    check("table pipes bound the run", find("| PO | 33001022 | $105.00 |") == ["33001022"])
    check("adjacent amount not merged in (contiguous still wins)",
          find("11024580 5.00") == ["11024580"])
    check("two distinct candidates in order of appearance",
          find("Job# 11024580 and PO 33001022") == ["11024580", "33001022"])
    check("wrong prefix ignored", find("Invoice 12345678") == [])
    check("phone number ignored", find("call 604 330 1022 now") == [])
    check("10 contiguous digits ignored", find("GST 1102458012") == [])
    check("empty text -> no candidates", find("") == [])
    check("None text -> no candidates", find(None) == [])


def test_vendor_extract_generate_twin():
    print("\n[gates: vendor extract primary + generate twin]")

    # extract high, no generate twin present -> passes on the extract alone (back-compat).
    r = ev(commercial_fields(vendor_name_extract=fstr("Xpert Mechanical", 0.95)))
    check("extract high, no twin -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])

    # extract low, generate high, SAME vendor -> passes; the clean generate value is written.
    fields = commercial_fields(
        vendor_name_extract=fstr("FortisBC Energy Inc.", 0.40),
        vendor_name_generate=fstr("FortisBC", 0.90),
    )
    r = ev(fields)
    check("extract low + generate high (same) -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    wv, _ = field_policy.build_write_values(gates.parse_fields(fields), THRESHOLD)
    check("writes clean generate value when the two are consistent",
          wv["vendor_name"] == "FortisBC", str(wv["vendor_name"]))
    check("no disagree advisory when consistent",
          not any("disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))
    check("final vendor_name = clean generate value (rescued, consistent)",
          r["fields"]["vendor_name"]["value"] == "FortisBC", str(r["fields"].get("vendor_name")))
    check("vendorResolution.source = agreement (below-threshold extract rescued by agreeing generate)",
          r["resolutions"]["vendor_name"]["source"] == "agreement", str(r["resolutions"].get("vendor_name")))

    # extract low, generate high, DISAGREE -> review (agreement-gated: a disagreeing
    # generate never rescues); the literal extract is retained; disagree advisory raised.
    fields = commercial_fields(
        vendor_name_extract=fstr("LevEllen Holdings Corp.", 0.30),
        vendor_name_generate=fstr("Great West Pool And Spa", 0.90),
    )
    r = ev(fields)
    check("extract low + generate high (disagree) -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("review reason names vendor",
          any("vendor_name" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))
    check("disagree advisory present when they differ",
          any("disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))
    check("writes the literal extract on disagreement",
          r["writeValues"]["vendor_name"] == "LevEllen Holdings Corp.", str(r["writeValues"].get("vendor_name")))

    # both pass but DIFFERENT -> passes on extract's own confidence; extract value kept;
    # disagree advisory raised.
    fields = commercial_fields(
        vendor_name_extract=fstr("ACME Plumbing", 0.95),
        vendor_name_generate=fstr("Something Else", 0.95),
    )
    r = ev(fields)
    check("both pass but differ -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    check("both pass but differ -> keep extract value",
          r["writeValues"]["vendor_name"] == "ACME Plumbing", str(r["writeValues"].get("vendor_name")))
    check("both pass but differ -> disagree advisory",
          any("disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # both pass and CONSISTENT -> happy; clean generate name written; source = generate.
    fields = commercial_fields(
        vendor_name_extract=fstr("FortisBC Energy Inc.", 0.95),
        vendor_name_generate=fstr("FortisBC", 0.95),
    )
    r = ev(fields)
    check("both pass consistent -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    check("both pass consistent -> clean generate name written",
          r["writeValues"]["vendor_name"] == "FortisBC", str(r["writeValues"].get("vendor_name")))
    check("source = generate when both clear and agree",
          r["resolutions"]["vendor_name"]["source"] == "generate", str(r["resolutions"].get("vendor_name")))

    # both below threshold but AGREE -> pass (agreement boost: two reads corroborate).
    fields = commercial_fields(
        vendor_name_extract=fstr("Whoever", 0.40),
        vendor_name_generate=fstr("Whoever", 0.45),
    )
    r = ev(fields)
    check("both below but agree -> happy (agreement boost)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("source = agreement (both below, corroborated)",
          r["resolutions"]["vendor_name"]["source"] == "agreement", str(r["resolutions"].get("vendor_name")))

    # both below threshold and DISAGREE -> review (no corroboration).
    fields = commercial_fields(
        vendor_name_extract=fstr("Alpha Plumbing", 0.40),
        vendor_name_generate=fstr("Beta Roofing", 0.45),
    )
    r = ev(fields)
    check("both below + disagree -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("review reason names vendor",
          any("vendor_name" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))

    # extract absent + confident generate -> generate rescues (nothing to disagree with).
    fields = commercial_fields(
        vendor_name_extract=fstr("", None),
        vendor_name_generate=fstr("FortisBC", 0.90),
    )
    r = ev(fields)
    check("vendor extract empty + confident generate -> happy (rescue)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("vendor rescue writes the generate value", r["writeValues"]["vendor_name"] == "FortisBC",
          str(r["writeValues"].get("vendor_name")))

    # consistency helper: suffixes / case / spacing ignored; genuinely different names are not.
    consistent = field_policy._vendor_values_consistent
    check("legal suffix ignored in comparison", consistent("FortisBC Energy Inc.", "FortisBC"))
    check("case and spacing ignored", consistent("bc  hydro", "BC Hydro"))
    check("different vendors are not consistent", not consistent("ACME Plumbing", "Bob Roofing"))


def test_service_address_extract_generate_twin():
    print("\n[gates: service_address extract (authoritative) + generate (validator)]")

    # extract passes alone, no generate twin -> happy on the extract (back-compat).
    r = ev(commercial_fields(service_address_extract=fstr("1606 Broadway W, Vancouver BC", 0.95)))
    check("extract high, no twin -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("source = extract when extract passes",
          r["resolutions"]["service_address"]["source"] == "extract", str(r["resolutions"].get("service_address")))

    # extract low, generate high, AGREE -> rescued; final = literal extract; conf = max.
    fields = commercial_fields(
        service_address_extract=fstr("1606 Broadway W, Vancouver BC", 0.60),
        service_address_generate=fstr("1606 Broadway W Vancouver BC", 0.90),
    )
    r = ev(fields)
    check("extract low + generate high (agree) -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("source = agreement when the twin rescues",
          r["resolutions"]["service_address"]["source"] == "agreement", str(r["resolutions"].get("service_address")))
    check("writes the literal extract value (not the generate)",
          r["writeValues"]["service_address"] == "1606 Broadway W, Vancouver BC", str(r["writeValues"].get("service_address")))
    check("final confidence lifted to max on agreement",
          r["fields"]["service_address"]["confidence"] == 0.90, str(r["fields"].get("service_address")))
    check("no disagree advisory when they agree",
          not any("service_address" in a and "disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # extract low, generate high, DISAGREE -> review; disagree advisory; value stays extract.
    fields = commercial_fields(
        service_address_extract=fstr("1606 Broadway W, Vancouver BC", 0.60),
        service_address_generate=fstr("500 Robson St, Burnaby BC", 0.90),
    )
    r = ev(fields)
    check("extract low + generate high (disagree) -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("review reason names service_address",
          any("service_address" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))
    check("disagree advisory present when they differ",
          any("service_address" in a and "disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))
    check("writes the literal extract value on disagreement",
          r["writeValues"]["service_address"] == "1606 Broadway W, Vancouver BC", str(r["writeValues"].get("service_address")))

    # both below threshold but AGREE -> pass (agreement boost).
    fields = commercial_fields(
        service_address_extract=fstr("1606 Broadway W", 0.50),
        service_address_generate=fstr("1606 Broadway W", 0.60),
    )
    r = ev(fields)
    check("both below but agree -> happy (agreement boost)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("source = agreement", r["resolutions"]["service_address"]["source"] == "agreement",
          str(r["resolutions"].get("service_address")))

    # both below threshold and DISAGREE -> review.
    fields = commercial_fields(
        service_address_extract=fstr("1606 Broadway W", 0.50),
        service_address_generate=fstr("500 Robson St", 0.60),
    )
    r = ev(fields)
    check("both below + disagree -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # extract passes on its own but generate disagrees -> happy (trust extract), but advisory raised.
    fields = commercial_fields(
        service_address_extract=fstr("1606 Broadway W, Vancouver BC", 0.90),
        service_address_generate=fstr("Somewhere Else", 0.88),
    )
    r = ev(fields)
    check("extract passes, generate disagrees -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    check("source = extract when extract passes alone", r["resolutions"]["service_address"]["source"] == "extract")
    check("disagree advisory still raised when extract passes",
          any("service_address" in a and "disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # token overlap helper: reordered/unit-format and subset agree; different streets do not.
    agree = field_policy._address_tokens_agree
    check("reordered / unit-format variants agree",
          agree("#113 - 8531 Alexandra Rd, Richmond BC", "8531 Alexandra Rd Unit 113, Richmond BC"))
    check("subset (address minus company line) agrees",
          agree("1100289 BC LTD 1606 Broadway W Vancouver BC", "1606 Broadway W Vancouver BC"))
    check("different streets do not agree",
          not agree("1606 Broadway W Vancouver BC", "500 Robson St Burnaby BC"))


def test_amount_and_po_twins():
    print("\n[gates: total / gst / po twins (numeric + digit agreement)]")

    # total_invoice_amount: both below threshold but the numbers agree -> pass (rescued).
    fields = commercial_fields(
        total_invoice_amount_extract=fnum(228.08, 0.60),
        total_invoice_amount_generate=fnum(228.08, 0.65),
    )
    r = ev(fields)
    check("total both below + equal -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("total source = agreement", r["resolutions"]["total_invoice_amount"]["source"] == "agreement",
          str(r["resolutions"].get("total_invoice_amount")))
    check("total writeValue is the resolved amount", r["writeValues"]["total_invoice_amount"] == 228.08,
          str(r["writeValues"].get("total_invoice_amount")))

    # total: both below and the numbers differ -> review.
    fields = commercial_fields(
        total_invoice_amount_extract=fnum(228.08, 0.60),
        total_invoice_amount_generate=fnum(500.00, 0.65),
    )
    r = ev(fields)
    check("total both below + differ -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("review reason names total", any("total_invoice_amount" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))

    # gst (commercial critical): both below but agree -> pass.
    fields = commercial_fields(
        gst_amount_extract=fnum(5.0, 0.60),
        gst_amount_generate=fnum(5.0, 0.65),
    )
    r = ev(fields)
    check("gst both below + equal -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("gst source = agreement", r["resolutions"]["gst_amount"]["source"] == "agreement")

    # gst is not critical for municipal -> an absent gst twin is still happy.
    r = ev(municipal_fields())
    check("municipal happy with no gst twin", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

    # po: both present and agree on 8 digits (below threshold) -> pass.
    fields = commercial_fields(
        po_or_job_number_extract=fstr("11024580", 0.60),
        po_or_job_number_generate=fstr("11024580", 0.60),
    )
    r = ev(fields)
    check("po both below + same 8 digits -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("po source = agreement", r["resolutions"]["po_or_job_number"]["source"] == "agreement")

    # po: agree on a NON-8-digit value -> review (format still enforced on the resolved value).
    fields = commercial_fields(
        po_or_job_number_extract=fstr("1234567", 0.60),
        po_or_job_number_generate=fstr("1234567", 0.60),
    )
    r = ev(fields)
    check("po agrees but not 8 digits -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("po format review reason names the field", any("po_or_job_number" in x for x in r["reviewReasons"]))

    # po: extract empty, generate found a confident 8-digit value -> generate rescue (pass).
    fields = commercial_fields(
        po_or_job_number_extract=fstr("", None),
        po_or_job_number_generate=fstr("33001022", 0.95),
    )
    r = ev(fields)
    check("po extract empty + confident generate -> happy (rescue)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("po rescue source = generate", r["resolutions"]["po_or_job_number"]["source"] == "generate")
    check("po rescue writes the generate value", r["writeValues"]["po_or_job_number"] == "33001022",
          str(r["writeValues"].get("po_or_job_number")))

    # po: extract empty, generate present but LOW confidence -> review (nothing corroborates it).
    r = ev(commercial_fields(
        po_or_job_number_extract=fstr("", None),
        po_or_job_number_generate=fstr("11024580", 0.50),
    ))
    check("po extract empty + low generate -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # numeric / po agreement helpers.
    check("amounts equal to the cent agree", field_policy._amounts_agree(228.08, 228.084))
    check("amounts differing by a cent do not agree", not field_policy._amounts_agree(228.08, 228.09))
    check("None amount does not agree", not field_policy._amounts_agree(228.08, None))
    check("same digit sequences agree", field_policy._po_values_agree("00471234", "00471234"))
    check("different digits do not agree", not field_policy._po_values_agree("00471234", "00471235"))
    check("empty po does not agree", not field_policy._po_values_agree("", "12345678"))


def test_po_ocr_rescue():
    print("\n[gates: PO rescue from OCR markdown]")

    def ev_md(fields, **kw):
        return gates.evaluate(cu_result(fields, **kw), THRESHOLD)

    # CU twins empty + exactly one candidate in the child markdown -> rescued, happy.
    r = ev_md(commercial_fields(po_or_job_number_extract=fstr("", None)),
              markdown="Invoice 4471\nJob# 11024580\nTotal $105.00")
    check("single OCR candidate -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("rescued value written", r["writeValues"]["po_or_job_number"] == "11024580",
          str(r["writeValues"].get("po_or_job_number")))
    check("fields.po carries the rescued value",
          r["fields"]["po_or_job_number"]["value"] == "11024580", str(r["fields"].get("po_or_job_number")))
    check("resolution source = ocr", r["resolutions"]["po_or_job_number"]["source"] == "ocr",
          str(r["resolutions"].get("po_or_job_number")))
    check("rescue advisory raised",
          any("po_or_job_number rescued from OCR text: 11024580" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # markdown on the router entry only -> still found (all contents entries scanned).
    r = ev_md(commercial_fields(po_or_job_number_extract=fstr("", None)),
              router_markdown="NOBLE & ASSOCIATES PROPERTY MANAGEMENT # 11022266")
    check("router-entry markdown also scanned", r["writeValues"]["po_or_job_number"] == "11022266",
          str(r["writeValues"].get("po_or_job_number")))
    check("router-entry rescue -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

    # spaced digits in the markdown normalise to the contiguous number.
    r = ev_md(commercial_fields(po_or_job_number_extract=fstr("", None)),
              markdown="PO No. 1102 4580")
    check("spaced OCR digits rescued contiguous", r["writeValues"]["po_or_job_number"] == "11024580",
          str(r["writeValues"].get("po_or_job_number")))

    # two distinct candidates -> review, both listed for the reviewer.
    r = ev_md(commercial_fields(po_or_job_number_extract=fstr("", None)),
              markdown="Job# 11024580 ... PO 33001022")
    check("two OCR candidates -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("review summary names the field",
          r["reviewReasons"] == ["po_or_job_number needs attention"], str(r["reviewReasons"]))
    check("advisory lists both candidates",
          any("11024580" in a and "33001022" in a and "multiple" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))
    check("no value written on ambiguity", r["writeValues"]["po_or_job_number"] in (None, ""),
          str(r["writeValues"].get("po_or_job_number")))

    # zero candidates in markdown -> review exactly as before the rescue existed.
    r = ev_md(commercial_fields(po_or_job_number_extract=fstr("", None)),
              markdown="No purchase order on this invoice. Account 99887766.")
    check("no OCR candidate -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("review summary names the field",
          r["reviewReasons"] == ["po_or_job_number needs attention"], str(r["reviewReasons"]))
    check("twin-clear failure detail moved to advisoryFlags",
          any("po_or_job_number_extract/po_or_job_number_generate" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # 0008 shape: CU confidently returns the 9-digit label artifact; the true
    # 8-digit value is elsewhere in the text -> rescue overrides the bad value.
    r = ev_md(commercial_fields(po_or_job_number_extract=fstr("330001022", 0.95)),
              markdown="Customer PO No. : 330001022\nSouth Fraser Plaza - Contract #33001022")
    check("format-violating CU value overridden by rescue -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("override writes the true 8-digit value", r["writeValues"]["po_or_job_number"] == "33001022",
          str(r["writeValues"].get("po_or_job_number")))
    check("override advisory records the discarded CU value",
          any("330001022" in a and "rescued" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # municipal bucket: PO is not critical -> rescue never fires, no value invented.
    r = ev_md(municipal_fields(), markdown="Job# 11024580")
    check("municipal -> no rescue, still happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    check("municipal -> no rescued write", r["writeValues"].get("po_or_job_number") in (None, ""),
          str(r["writeValues"].get("po_or_job_number")))
    check("municipal -> no rescue advisory", not any("rescued from OCR" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # PO already resolved and valid -> markdown (even a different number) is ignored.
    r = ev_md(commercial_fields(), markdown="unrelated 33001022")
    check("valid CU value untouched by markdown", r["writeValues"]["po_or_job_number"] == "11024580",
          str(r["writeValues"].get("po_or_job_number")))
    check("source stays extract", r["resolutions"]["po_or_job_number"]["source"] == "extract")
    check("no rescue advisory when CU value stands",
          not any("rescued from OCR" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))


def test_account_number_twin():
    print("\n[gates: account_number twin -- municipal critical, commercial informational]")

    # municipal missing account entirely -> review (required for municipal).
    fields = municipal_fields()
    del fields["account_number_extract"]
    r = ev(fields)
    check("municipal missing account -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("review summary names account_number",
          r["reviewReasons"] == ["account_number needs attention"], str(r["reviewReasons"]))
    check("account twin diagnostic moved to advisoryFlags",
          any("account_number_extract/account_number_generate" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # municipal confident extract -> happy; printed form (dashes kept) written; source = extract.
    r = ev(municipal_fields(account_number_extract=fstr("12345-001", 0.95)))
    check("municipal confident extract -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("printed form (dashes kept) written", r["writeValues"]["account_number"] == "12345-001",
          str(r["writeValues"].get("account_number")))
    check("account source = extract", r["resolutions"]["account_number"]["source"] == "extract",
          str(r["resolutions"].get("account_number")))

    # municipal extract low + generate low but agreeing across punctuation/spacing ->
    # happy via agreement (letters+digits comparison); the literal extract is written.
    fields = municipal_fields(
        account_number_extract=fstr("123456789012", 0.50),
        account_number_generate=fstr("1234-5678-9012", 0.60),
    )
    r = ev(fields)
    check("municipal low twins agreeing (punctuation ignored) -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("account source = agreement", r["resolutions"]["account_number"]["source"] == "agreement",
          str(r["resolutions"].get("account_number")))
    check("writes the literal extract value", r["writeValues"]["account_number"] == "123456789012",
          str(r["writeValues"].get("account_number")))

    # municipal disagreeing low twins -> review + disagree advisory.
    fields = municipal_fields(
        account_number_extract=fstr("123456789012", 0.50),
        account_number_generate=fstr("999999", 0.60),
    )
    r = ev(fields)
    check("municipal disagreeing low twins -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("account disagree advisory raised",
          any("account_number" in a and "disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # municipal extract absent + confident generate -> generate rescue.
    fields = municipal_fields(account_number_extract=fstr("", None),
                              account_number_generate=fstr("123456789012", 0.90))
    r = ev(fields)
    check("municipal rescue by confident generate -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("account rescue source = generate", r["resolutions"]["account_number"]["source"] == "generate",
          str(r["resolutions"].get("account_number")))

    # commercial missing account -> still happy (purely informational there).
    r = ev(commercial_fields())
    check("commercial without account -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("writeValues carries the account_number key (None)", "account_number" in r["writeValues"],
          str(list(r["writeValues"])))

    # commercial account present at low confidence -> still happy (never blocks), value written.
    r = ev(commercial_fields(account_number_extract=fstr("A-778812", 0.40)))
    check("commercial low-conf account -> still happy (never blocks)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("commercial account written to writeValues",
          r["writeValues"]["account_number"] == "A-778812", str(r["writeValues"].get("account_number")))

    # identifier agreement helper: letters+digits only, case-insensitive.
    agree = field_policy._identifier_values_agree
    check("spacing/dash variants agree", agree("123456789012", "1234-5678-9012"))
    check("dot vs dash agree", agree("12345-001", "12345.001"))
    check("case ignored", agree("AB1234", "ab-1234"))
    check("different numbers do not agree", not agree("12345-001", "12345-002"))
    check("empty never agrees", not agree("", "123"))
    check("None never agrees", not agree(None, "123"))


def test_invoice_number_twin():
    print("\n[gates: invoice_number twin -- municipal critical, commercial informational]")

    # municipal missing invoice number entirely -> review (required for municipal).
    fields = municipal_fields()
    del fields["invoice_number_extract"]
    r = ev(fields)
    check("municipal missing invoice number -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("review summary names invoice_number",
          r["reviewReasons"] == ["invoice_number needs attention"], str(r["reviewReasons"]))
    check("invoice twin diagnostic moved to advisoryFlags",
          any("invoice_number_extract/invoice_number_generate" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # commercial missing invoice number -> still happy (optional for commercial).
    fields = commercial_fields()
    del fields["invoice_number_extract"]
    r = ev(fields)
    check("commercial without invoice number -> happy (optional)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])

    # municipal low twins agreeing across case/spacing -> happy via agreement;
    # the literal extract is written.
    fields = municipal_fields(
        invoice_number_extract=fstr("INV-2201", 0.50),
        invoice_number_generate=fstr("inv 2201", 0.60),
    )
    r = ev(fields)
    check("municipal low twins agreeing (case/spacing ignored) -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("invoice source = agreement", r["resolutions"]["invoice_number"]["source"] == "agreement",
          str(r["resolutions"].get("invoice_number")))
    check("writes the literal extract value", r["writeValues"]["invoice_number"] == "INV-2201",
          str(r["writeValues"].get("invoice_number")))

    # municipal disagreeing low twins -> review.
    fields = municipal_fields(
        invoice_number_extract=fstr("BL-123456", 0.50),
        invoice_number_generate=fstr("BL-999999", 0.60),
    )
    r = ev(fields)
    check("municipal disagreeing low twins -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])

    # municipal extract absent + confident generate -> generate rescue (licence number read
    # by the reasoning twin).
    fields = municipal_fields(invoice_number_extract=fstr("", None),
                              invoice_number_generate=fstr("BL-123456", 0.90))
    r = ev(fields)
    check("municipal rescue by confident generate -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("invoice rescue source = generate", r["resolutions"]["invoice_number"]["source"] == "generate",
          str(r["resolutions"].get("invoice_number")))

    # commercial invoice number present at low confidence -> still happy, value written.
    r = ev(commercial_fields(invoice_number_extract=fstr("INV-9944", 0.40)))
    check("commercial low-conf invoice number -> still happy (never blocks)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("commercial invoice number written to writeValues",
          r["writeValues"]["invoice_number"] == "INV-9944", str(r["writeValues"].get("invoice_number")))


def test_invoice_number_filename_fallback():
    print("\n[gates: invoice_number filename fallback -- municipal only, twins empty]")

    # municipal, both twins empty, filename sent -> defaulted from the filename stem.
    fields = municipal_fields(invoice_number_extract=fstr("", None))
    r = ev(fields, file_name="BL 2026-0417.pdf")
    check("municipal empty twins + filename -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("stem written (extension stripped)",
          r["writeValues"]["invoice_number"] == "BL 2026-0417", str(r["writeValues"].get("invoice_number")))
    check("resolution source = filename",
          r["resolutions"]["invoice_number"]["source"] == "filename", str(r["resolutions"].get("invoice_number")))
    check("defaultedFields records the default",
          "invoice_number" in r["defaultedFields"], str(r["defaultedFields"]))
    check("fallback advisory raised",
          any("invoice_number defaulted from the SharePoint filename: BL 2026-0417" in a
              for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))
    check("fields.invoice_number carries the defaulted value",
          r["fields"]["invoice_number"]["value"] == "BL 2026-0417", str(r["fields"].get("invoice_number")))

    # twins absent entirely (not just empty) -> same fallback.
    fields = municipal_fields()
    del fields["invoice_number_extract"]
    r = ev(fields, file_name="utility-bill.pdf")
    check("municipal absent twins + filename -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("absent twins write the stem", r["writeValues"]["invoice_number"] == "utility-bill",
          str(r["writeValues"].get("invoice_number")))

    # no filename -> review exactly as before the fallback existed.
    r = ev(municipal_fields(invoice_number_extract=fstr("", None)))
    check("municipal empty twins, no filename -> review (unchanged)",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("no filename -> nothing defaulted", "invoice_number" not in r["defaultedFields"],
          str(r["defaultedFields"]))

    # a present-but-low-confidence value is never overwritten by the filename.
    r = ev(municipal_fields(invoice_number_extract=fstr("BL-1234", 0.40)),
           file_name="something-else.pdf")
    check("low-conf present value + filename -> still review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("present value not overwritten", r["writeValues"]["invoice_number"] == "BL-1234",
          str(r["writeValues"].get("invoice_number")))
    check("no fallback advisory when a value is present",
          not any("defaulted from the SharePoint filename" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # commercial: invoice_number is not critical -> fallback never fires.
    fields = commercial_fields()
    del fields["invoice_number_extract"]
    r = ev(fields, file_name="commercial-invoice.pdf")
    check("commercial empty twins + filename -> happy (not critical)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("commercial invoice_number stays empty (municipal-only fallback)",
          r["writeValues"].get("invoice_number") in (None, ""), str(r["writeValues"].get("invoice_number")))
    check("commercial -> no fallback advisory",
          not any("defaulted from the SharePoint filename" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # helper: stem stripping and blanks.
    default = field_policy.invoice_number_default
    check("stem strips the extension", default("BL 2026-0417.pdf") == "BL 2026-0417")
    check("multi-dot name keeps inner dots", default("bill.2026.pdf") == "bill.2026")
    check("no extension passes through", default("BL-2026") == "BL-2026")
    check("whitespace stripped", default("  invoice1.pdf  ") == "invoice1")
    check("blank filename -> None", default("   ") is None)
    check("None filename -> None", default(None) is None)


def test_b4_review_summary():
    print("\n[gates: B4 reviewReasons summary -- one message, diagnostics in advisoryFlags]")

    # one failing field -> "<field> needs attention".
    r = ev(commercial_fields(vendor_name_extract=fstr("Bob", 0.60)))
    check("single failure summary", r["reviewReasons"] == ["vendor_name needs attention"],
          str(r["reviewReasons"]))
    check("diagnostic detail moved to advisoryFlags",
          any(a.startswith("B4 ") and "vendor_name" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # two failing fields -> "a and b need attention" (critical-set order).
    r = ev(commercial_fields(vendor_name_extract=fstr("Bob", 0.60),
                             gst_amount_extract=fnum(5.0, 0.40)))
    check("two-failure summary", r["reviewReasons"] == ["vendor_name and gst_amount need attention"],
          str(r["reviewReasons"]))

    # three failing fields -> "a, b and c need attention".
    r = ev(commercial_fields(vendor_name_extract=fstr("Bob", 0.60),
                             service_address_extract=fstr("123 Main St", 0.50),
                             gst_amount_extract=fnum(5.0, 0.40)))
    check("three-failure summary",
          r["reviewReasons"] == ["vendor_name, service_address and gst_amount need attention"],
          str(r["reviewReasons"]))

    # happy path unchanged: no reasons, no B4 diagnostics.
    r = ev(commercial_fields())
    check("happy path has no review reasons", r["reviewReasons"] == [], str(r["reviewReasons"]))
    check("happy path has no B4 advisories",
          not any(a.startswith("B4 ") for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # grammar helper directly.
    check("summary of one", gates.b4_summary(["vendor_name"]) == "vendor_name needs attention")
    check("summary of two",
          gates.b4_summary(["vendor_name", "po_or_job_number"])
          == "vendor_name and po_or_job_number need attention")
    check("summary of three", gates.b4_summary(["a", "b", "c"]) == "a, b and c need attention")
    check("summary of none", gates.b4_summary([]) == "")


def main():
    test_policy_constants_and_buckets()
    test_field_format_rules()
    test_find_po_candidates()
    test_date_defaulting_and_derivation()
    test_commercial_routing()
    test_municipal_routing()
    test_default_and_residual()
    test_handwriting_b2_nochild()
    test_response_shape()
    test_vendor_extract_generate_twin()
    test_service_address_extract_generate_twin()
    test_amount_and_po_twins()
    test_account_number_twin()
    test_invoice_number_twin()
    test_invoice_number_filename_fallback()
    test_b4_review_summary()
    test_po_ocr_rescue()

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
