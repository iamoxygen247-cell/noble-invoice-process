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


def fint(value, conf):
    return {"valueInteger": value, "confidence": conf}


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
        "invoice_date_extract": fdate("2026-05-01", 0.95),
        "payment_due_date": fdate("2026-05-31", 0.94),
        "invoice_number_extract": fstr("INV-2201", 0.92),
        "bill_type": fstr("commercial", 0.9),
        "sub_bill_type": fstr("repair", 0.9),
        "sub_bill_type_generate": fstr("repair", 0.85),
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
        "invoice_date_extract": fdate("2026-05-10", 0.95),
        "payment_due_date": fdate("2026-06-10", 0.94),
        "bill_type": fstr("municipal", 0.9),
        "sub_bill_type": fstr("business_license", 0.9),
        "sub_bill_type_generate": fstr("business_license", 0.85),
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
        invoice_date_extract=fdate(None, None),
        payment_due_date=fdate("2026-05-31", 0.40),
    ))
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("missing invoice_date -> today (PST)", wv["invoice_date"] == TODAY, wv["invoice_date"])
    check("low-conf payment_due_date -> today+30", wv["payment_due_date"] == DUE_30, wv["payment_due_date"])
    check("both dates recorded as defaulted",
          set(defaulted) == {"invoice_date", "payment_due_date"}, str(defaulted))

    # date confidence exactly at threshold is reliable (>=)
    parsed = gates.parse_fields(commercial_fields(invoice_date_extract=fdate("2026-05-01", THRESHOLD)))
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("date conf == threshold is kept (>=)",
          wv["invoice_date"] == "2026-05-01" and "invoice_date" not in defaulted)

    # non-ISO but unambiguous month-name normalises to YYYY-MM-DD
    parsed = gates.parse_fields(commercial_fields(invoice_date_extract=fdate("May 1, 2026", 0.95)))
    wv, _ = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("month-name date normalised to YYYY-MM-DD", wv["invoice_date"] == "2026-05-01", wv["invoice_date"])

    # ambiguous numeric date treated as unparseable -> defaulted (safer than wrong guess)
    parsed = gates.parse_fields(commercial_fields(invoice_date_extract=fdate("03/04/2026", 0.95)))
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

    # twin resolution: either twin saying yes wins (advisory recall over precision)
    r = ev(commercial_fields(is_handwritten=fstr("no", 0.88),
                             is_handwritten_generate=fstr("yes", 0.61)))
    check("classify no + generate yes -> yes", r["isHandwritten"] == "yes")
    check("yes confidence comes from the yes twin", r["isHandwrittenConfidence"] == 0.61)
    r = ev(commercial_fields(is_handwritten=fstr("yes", 0.55),
                             is_handwritten_generate=fstr("no", 0.9)))
    check("classify yes + generate no -> yes", r["isHandwritten"] == "yes")
    check("yes confidence from the classify twin", r["isHandwrittenConfidence"] == 0.55)
    r = ev(commercial_fields(is_handwritten=fstr("yes", 0.55),
                             is_handwritten_generate=fstr("yes", 0.8)))
    check("both yes -> yes at max confidence", r["isHandwritten"] == "yes"
          and r["isHandwrittenConfidence"] == 0.8, str(r["isHandwrittenConfidence"]))
    r = ev(commercial_fields(is_handwritten=fstr("no", 0.88),
                             is_handwritten_generate=fstr("no", 0.61)))
    check("both no -> no, classify confidence kept", r["isHandwritten"] == "no"
          and r["isHandwrittenConfidence"] == 0.88, str(r["isHandwrittenConfidence"]))
    r = ev(commercial_fields(is_handwritten=fstr("", None),
                             is_handwritten_generate=fstr("no", 0.7)))
    check("classify empty -> generate label fills in", r["isHandwritten"] == "no"
          and r["isHandwrittenConfidence"] == 0.7, str(r["isHandwrittenConfidence"]))
    # generate twin absent (older analyzer) -> classify alone, exactly as before
    r = ev(commercial_fields(is_handwritten=fstr("no", 0.88)))
    check("generate absent -> classify value unchanged", r["isHandwritten"] == "no"
          and r["isHandwrittenConfidence"] == 0.88, str(r["isHandwrittenConfidence"]))

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
    check("has billType / subBillType / policyBucket / policyVersion",
          all(k in r for k in ("billType", "subBillType", "policyBucket", "policyVersion")))
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

    # A "dba" bill prints the legal entity AND the trade name; the generate twin returns
    # only the trade name, which agrees by substring containment. The printed name wins
    # regardless of confidence -- without this the more confident trade name is written.
    dba = "Graffiti Guys Removal Services\ndba Goodbye Graffiti Surrey"
    fields = commercial_fields(
        vendor_name_extract=fstr(dba, 0.662),
        vendor_name_generate=fstr("Goodbye Graffiti", 0.710),
    )
    r = ev(fields)
    check("dba: printed name beats a MORE confident trade name",
          r["writeValues"]["vendor_name"] == "Graffiti Guys Removal Services dba Goodbye Graffiti Surrey",
          str(r["writeValues"].get("vendor_name")))
    check("dba: below threshold but corroborated -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("dba: source = agreement", r["resolutions"]["vendor_name"]["source"] == "agreement",
          str(r["resolutions"].get("vendor_name")))

    # Same bill, a replicate where CU truncated the extract at the line break: no dba
    # connector to see, extract clears on its own, legal name written.
    fields = commercial_fields(
        vendor_name_extract=fstr("Graffiti Guys Removal Services", 0.981),
        vendor_name_generate=fstr("Goodbye Graffiti", 0.710),
    )
    r = ev(fields)
    check("dba truncated by CU -> extract stands on its own",
          r["writeValues"]["vendor_name"] == "Graffiti Guys Removal Services",
          str(r["writeValues"].get("vendor_name")))
    check("dba truncated -> source = extract",
          r["resolutions"]["vendor_name"]["source"] == "extract", str(r["resolutions"].get("vendor_name")))

    # dba detector: connector spellings recognised, ordinary vendor names untouched.
    has_dba = field_policy._has_dba_clause
    check("dba connector recognised", has_dba("ABC Services dba Xyz"))
    check("d/b/a recognised", has_dba("ABC Services d/b/a Xyz"))
    check("d.b.a. recognised", has_dba("ABC Services D.B.A. Xyz"))
    check("dba after a newline recognised", has_dba(dba))
    check("dba in parentheses recognised", has_dba("ABC Services (dba Xyz)"))
    for name in ("City of Surrey", "BC Hydro", "FortisBC Energy Inc.", "SIMON SIK FAI KAN",
                 "PRIORITY appliance service", "WASTE CONNECTIONS OF CANADA", "JMEC Electric",
                 "District of West Vancouver", "Vangate Locksmith"):
        check(f"no false dba match: {name}", not has_dba(name))
    check("dba inside a word is not a connector", not has_dba("Dbanks Roofing"))
    check("dba detector ignores non-strings", not has_dba(None))

    # consistency helper: suffixes / case / spacing ignored; genuinely different names are not.
    consistent = field_policy._vendor_values_consistent
    check("legal suffix ignored in comparison", consistent("FortisBC Energy Inc.", "FortisBC"))
    check("case and spacing ignored", consistent("bc  hydro", "BC Hydro"))
    check("different vendors are not consistent", not consistent("ACME Plumbing", "Bob Roofing"))

    # A shortened PERSONAL name: the dropped words sit in the middle, so substring
    # containment never sees it -- the token-subset form does.
    check("shortened personal name is the same vendor",
          consistent("SIMON SIK FAI KAN", "SIMON KAN"))
    check("shortened personal name, mixed case", consistent("SIMON SIK FAI KAN", "Simon Kan"))
    check("a shared surname alone is not the same vendor",
          not consistent("SIMON SIK FAI KAN", "DANNY KAN"))
    check("shared leading words are not the same vendor",
          not consistent("Great West Pool And Spa", "Great West Plumbing"))
    check("sibling municipalities are not the same vendor",
          not consistent("City of Richmond", "City of Vancouver"))
    check("customer vs vendor sharing one word is not the same vendor",
          not consistent("Noble & Associates", "Noble Homes"))

    # When the two spellings genuinely differ, the more confident twin is written. The
    # extract read the printed name at 0.72; the generate shortened it at 0.45.
    fields = commercial_fields(
        vendor_name_extract=fstr("SIMON SIK FAI KAN", 0.72),
        vendor_name_generate=fstr("SIMON KAN", 0.45),
    )
    r = ev(fields)
    check("sub-threshold extract rescued by the shortened personal name -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("writes the more confident (printed) spelling",
          r["writeValues"]["vendor_name"] == "SIMON SIK FAI KAN", str(r["writeValues"].get("vendor_name")))
    check("source = agreement (extract below the bar, corroborated)",
          r["resolutions"]["vendor_name"]["source"] == "agreement", str(r["resolutions"].get("vendor_name")))
    check("agreeing twins raise no disagree advisory",
          not any("disagree" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # Same pair with both twins clearing the bar -> still the more confident spelling.
    r = ev(commercial_fields(
        vendor_name_extract=fstr("SIMON SIK FAI KAN", 0.95),
        vendor_name_generate=fstr("SIMON KAN", 0.80),
    ))
    check("both clear: more confident spelling written",
          r["writeValues"]["vendor_name"] == "SIMON SIK FAI KAN", str(r["writeValues"].get("vendor_name")))
    check("both clear: source names the twin that supplied the value",
          r["resolutions"]["vendor_name"]["source"] == "extract", str(r["resolutions"].get("vendor_name")))

    # Same NAME in different casing -> the generate's clean spelling is kept even when
    # the extract is more confident (confidence only decides genuinely different names).
    r = ev(commercial_fields(
        vendor_name_extract=fstr("CITY OF SURREY", 0.98),
        vendor_name_generate=fstr("City of Surrey", 0.76),
    ))
    check("casing-only difference keeps the clean generate spelling",
          r["writeValues"]["vendor_name"] == "City of Surrey", str(r["writeValues"].get("vendor_name")))

    # Legal suffix only -> same name after normalisation, so the suffix stays dropped
    # even though the extract is far more confident.
    r = ev(commercial_fields(
        vendor_name_extract=fstr("PROTECH PEST CONTROL LTD.", 0.90),
        vendor_name_generate=fstr("PROTECH PEST CONTROL", 0.43),
    ))
    check("legal suffix stays dropped regardless of confidence",
          r["writeValues"]["vendor_name"] == "PROTECH PEST CONTROL", str(r["writeValues"].get("vendor_name")))

    # An OCR line break inside a company name is an artifact -- it flips run to run on the
    # same document, so the written value collapses whitespace.
    r = ev(commercial_fields(
        vendor_name_extract=fstr("WASTE MANAGEMENT\nOF CANADA CORPORATION", 0.95),
        vendor_name_generate=fstr("Waste Management", 0.50),
    ))
    check("newline inside the vendor name is collapsed",
          r["writeValues"]["vendor_name"] == "WASTE MANAGEMENT OF CANADA CORPORATION",
          repr(r["writeValues"].get("vendor_name")))

    check("service_address keeps its legitimate multi-line form",
          "\n" in str(ev(commercial_fields(
              service_address_extract=fstr("2985 GRANVILLE ST\nVANCOUVER BC", 0.95),
          ))["writeValues"]["service_address"]))


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


def test_bill_to_address_backfills_empty_service_address():
    print("\n[gates: bill_to_address backfills an empty service_address]")

    # No SHIP TO / Service Address on the page: both service_address twins empty and the
    # invoice is addressed only to the Bill To block (JMEC Electric, bug_260629_0012).
    r = ev(commercial_fields(
        service_address_extract=fstr("", None),
        bill_to_address_extract=fstr("#307-7480 Gilbert Road, Richmond BC", 0.90),
    ))
    check("empty service_address + confident non-office Bill To -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("service_address source = bill_to_fallback",
          r["resolutions"]["service_address"]["source"] == "bill_to_fallback",
          str(r["resolutions"].get("service_address")))
    check("service_address written from the Bill To block",
          r["writeValues"]["service_address"] == "#307-7480 Gilbert Road, Richmond BC",
          str(r["writeValues"].get("service_address")))
    check("backfill advisory raised",
          any("backfilled from the Bill To block" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # Bill To is Noble's own head office -> names no serviced location -> not promoted -> review.
    r = ev(commercial_fields(
        service_address_extract=fstr("", None),
        bill_to_address_extract=fstr("155 - 13988 Maycrest Way, Richmond, BC V6V 3C3", 0.95),
    ))
    check("empty service_address + Bill To = Noble head office -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("head-office Bill To is not promoted",
          r["resolutions"]["service_address"]["source"] != "bill_to_fallback",
          str(r["resolutions"].get("service_address")))
    check("no backfill advisory for the head office",
          not any("backfilled from the Bill To block" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # A real (present) service_address is never overwritten by the Bill To, even below the bar.
    r = ev(commercial_fields(
        service_address_extract=fstr("999 Real Site St, Vancouver BC", 0.50),
        bill_to_address_extract=fstr("#307-7480 Gilbert Road, Richmond BC", 0.95),
    ))
    check("present-but-low service_address is not overwritten by the Bill To",
          r["writeValues"]["service_address"] == "999 Real Site St, Vancouver BC",
          str(r["writeValues"].get("service_address")))
    check("present-but-low service_address still routes to review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])

    # Below-threshold Bill To with no corroborating twin -> not promoted -> review.
    r = ev(commercial_fields(
        service_address_extract=fstr("", None),
        bill_to_address_extract=fstr("#307-7480 Gilbert Road, Richmond BC", 0.50),
    ))
    check("empty service_address + below-bar Bill To (no twin) -> review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])
    check("below-bar Bill To is not promoted",
          r["resolutions"]["service_address"]["source"] != "bill_to_fallback",
          str(r["resolutions"].get("service_address")))

    # Below-threshold Bill To twins that AGREE clear the bar (agreement boost) -> promoted.
    r = ev(commercial_fields(
        service_address_extract=fstr("", None),
        bill_to_address_extract=fstr("#307-7480 Gilbert Road, Richmond BC", 0.50),
        bill_to_address_generate=fstr("307-7480 Gilbert Road, Richmond BC", 0.60),
    ))
    check("empty service_address + agreeing sub-bar Bill To twins -> happy",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("agreeing Bill To twins are promoted (source bill_to_fallback)",
          r["resolutions"]["service_address"]["source"] == "bill_to_fallback",
          str(r["resolutions"].get("service_address")))


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


def test_pst_twin_and_zero_default():
    print("\n[gates: pst twin (informational only) + written 0 default]")

    # pst is in no bucket's critical set: absent twins (the common service-only
    # invoice) never gate, and the written value defaults to 0.
    r = ev(commercial_fields())
    check("no pst twins -> still happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("no pst twins -> resolution is none/failed (informational)",
          r["resolutions"]["pst_amount"]["passed"] is False
          and r["resolutions"]["pst_amount"]["source"] == "none",
          str(r["resolutions"].get("pst_amount")))
    check("no pst twins -> writeValues carries 0", r["writeValues"]["pst_amount"] == 0,
          str(r["writeValues"].get("pst_amount")))
    check("pst not in commercial criticals",
          "pst_amount" not in field_policy.critical_fields("commercial"))
    check("pst not in municipal criticals",
          "pst_amount" not in field_policy.critical_fields("municipal"))

    # confident extract flows to writeValues; amount_excluding_gst stays total - gst.
    r = ev(commercial_fields(pst_amount_extract=fnum(18.90, 0.93)))
    check("pst extract writes the amount", r["writeValues"]["pst_amount"] == 18.90,
          str(r["writeValues"].get("pst_amount")))
    check("pst source = extract", r["resolutions"]["pst_amount"]["source"] == "extract")
    check("amount_excluding_gst unchanged by pst (105 - 5)",
          r["writeValues"]["amount_excluding_gst"] == 100.0, str(r["writeValues"].get("amount_excluding_gst")))

    # both twins below threshold but equal -> agreement carries it, like gst.
    r = ev(commercial_fields(
        pst_amount_extract=fnum(34.75, 0.60),
        pst_amount_generate=fnum(34.75, 0.65),
    ))
    check("pst both below + equal -> agreement", r["resolutions"]["pst_amount"]["source"] == "agreement")
    check("pst agreement writes the value", r["writeValues"]["pst_amount"] == 34.75,
          str(r["writeValues"].get("pst_amount")))

    # a low-confidence, disagreeing pst never routes to review (informational only).
    r = ev(commercial_fields(
        pst_amount_extract=fnum(18.90, 0.40),
        pst_amount_generate=fnum(5.0, 0.40),
    ))
    check("low/conflicting pst still happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])

    # 'N/A' and empty-string resolutions normalise to 0 in the written values.
    parsed = gates.parse_fields(commercial_fields(pst_amount_generate=fstr("N/A", 0.90)))
    wv, _ = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("'N/A' pst -> written 0", wv["pst_amount"] == 0, str(wv["pst_amount"]))

    parsed = gates.parse_fields(commercial_fields(pst_amount_extract=fstr("", None)))
    wv, _ = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("empty-string pst -> written 0", wv["pst_amount"] == 0, str(wv["pst_amount"]))

    # a genuine 0.0 from CU passes through (0 is not missing), source generate rescue.
    parsed = gates.parse_fields(commercial_fields(pst_amount_generate=fnum(0.0, 0.95)))
    wv, _ = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("explicit 0.0 pst stays 0", wv["pst_amount"] == 0.0, str(wv["pst_amount"]))


def test_billing_period_twins_and_derivation():
    print("\n[gates: billing-period twins -- informational; derived start = end - (days - 1)]")

    # Informational only: written to Dynamics, never critical, never today-defaulted.
    for name in ("billing_period_start_date", "billing_period_end_date", "number_of_days"):
        check(f"{name} in WRITE_FIELDS", name in field_policy.WRITE_FIELDS)
        check(f"{name} not critical (commercial)", name not in field_policy.critical_fields("commercial"))
        check(f"{name} not critical (municipal)", name not in field_policy.critical_fields("municipal"))
    check("billing dates NOT in DATE_FIELDS (never defaulted to today)",
          "billing_period_start_date" not in field_policy.DATE_FIELDS
          and "billing_period_end_date" not in field_policy.DATE_FIELDS)

    # commercial invoice without the twins -> keys present but blank, never defaulted, happy.
    r = ev(commercial_fields())
    check("commercial -> blank billing start", r["writeValues"]["billing_period_start_date"] == "",
          str(r["writeValues"].get("billing_period_start_date")))
    check("commercial -> blank billing end", r["writeValues"]["billing_period_end_date"] == "",
          str(r["writeValues"].get("billing_period_end_date")))
    check("commercial -> blank number_of_days", r["writeValues"]["number_of_days"] == "",
          str(r["writeValues"].get("number_of_days")))
    check("blank billing fields never recorded as defaulted",
          not any(n.startswith("billing_period") or n == "number_of_days" for n in r["defaultedFields"]),
          str(r["defaultedFields"]))
    check("commercial stays happy without billing twins",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])

    # Burnaby/West Van shape: explicit period + DAYS column, confident extracts.
    fields = municipal_fields(
        sub_bill_type=fstr("water", 0.9),
        sub_bill_type_generate=fstr("water", 0.85),
        billing_period_start_date_extract=fdate("2026-01-01", 0.94),
        billing_period_end_date_extract=fdate("2026-03-31", 0.93),
        number_of_days_extract=fint(83, 0.92),
    )
    r = ev(fields)
    check("explicit period -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("start source = extract", r["resolutions"]["billing_period_start_date"]["source"] == "extract",
          str(r["resolutions"].get("billing_period_start_date")))
    check("end source = extract", r["resolutions"]["billing_period_end_date"]["source"] == "extract")
    check("days source = extract", r["resolutions"]["number_of_days"]["source"] == "extract")
    check("start written normalised", r["writeValues"]["billing_period_start_date"] == "2026-01-01",
          str(r["writeValues"].get("billing_period_start_date")))
    check("end written normalised", r["writeValues"]["billing_period_end_date"] == "2026-03-31")
    check("days written as int", r["writeValues"]["number_of_days"] == 83,
          str(r["writeValues"].get("number_of_days")))
    check("no derivation when start is printed",
          not any("billing_period_start_date derived" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # Agreement boost (the vendor_name-style sub-threshold rescue): both twins below
    # the bar but the same calendar day across formats -> passed, source agreement.
    fields = municipal_fields(
        billing_period_start_date_extract=fdate("2026-01-01", 0.50),
        billing_period_start_date_generate=fdate("Jan 1, 2026", 0.60),
    )
    r = ev(fields)
    check("both below but same day -> passed via agreement",
          r["resolutions"]["billing_period_start_date"]["passed"] is True
          and r["resolutions"]["billing_period_start_date"]["source"] == "agreement",
          str(r["resolutions"].get("billing_period_start_date")))
    check("agreement writes the normalised extract value",
          r["writeValues"]["billing_period_start_date"] == "2026-01-01",
          str(r["writeValues"].get("billing_period_start_date")))
    fields = municipal_fields(
        number_of_days_extract=fint(83, 0.50),
        number_of_days_generate=fstr("83 days", 0.60),
        # A period end is required for the count to be written at all (a day count on a
        # bill with no billing period is meaningless -- see the invented-"1" case below).
        billing_period_end_date_extract=fdate("2026-03-31", 0.90),
    )
    r = ev(fields)
    check("days both below but equal -> agreement",
          r["resolutions"]["number_of_days"]["source"] == "agreement",
          str(r["resolutions"].get("number_of_days")))
    check("days agreement writes the int", r["writeValues"]["number_of_days"] == 83,
          str(r["writeValues"].get("number_of_days")))

    # Abbotsford: month-only period -> extract twins null, generate returns the
    # reading date as the end; code derives start = end - (days - 1) = Mar 1.
    fields = municipal_fields(
        sub_bill_type=fstr("water", 0.9),
        sub_bill_type_generate=fstr("water", 0.85),
        billing_period_end_date_generate=fdate("2026-04-30", 0.85),
        number_of_days_extract=fint(61, 0.90),
    )
    r = ev(fields)
    check("end rescued by confident generate (reading date)",
          r["resolutions"]["billing_period_end_date"]["source"] == "generate",
          str(r["resolutions"].get("billing_period_end_date")))
    check("Abbotsford derived start = 2026-03-01",
          r["writeValues"]["billing_period_start_date"] == "2026-03-01",
          str(r["writeValues"].get("billing_period_start_date")))
    check("derived start source = derived",
          r["resolutions"]["billing_period_start_date"]["source"] == "derived"
          and r["resolutions"]["billing_period_start_date"]["passed"] is True,
          str(r["resolutions"].get("billing_period_start_date")))
    check("derived start confidence = min(end, days)",
          r["fields"]["billing_period_start_date"]["confidence"] == 0.85,
          str(r["fields"].get("billing_period_start_date")))
    check("derivation advisory raised",
          any("billing_period_start_date derived from billing_period_end_date" in a
              for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))
    check("derivation never gates routing", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

    # Surrey: no period printed at all -> end = reading date, days = 112,
    # derived start = 2026-01-08.
    fields = municipal_fields(
        billing_period_end_date_generate=fdate("2026-04-29", 0.90),
        number_of_days_extract=fint(112, 0.88),
    )
    r = ev(fields)
    check("Surrey derived start = 2026-01-08",
          r["writeValues"]["billing_period_start_date"] == "2026-01-08",
          str(r["writeValues"].get("billing_period_start_date")))
    check("Surrey derived confidence = min(end, days)",
          r["fields"]["billing_period_start_date"]["confidence"] == 0.88,
          str(r["fields"].get("billing_period_start_date")))

    # A printed start -- even below the bar -- is never overwritten by derivation
    # (meter interval can legitimately differ from the period length: Burnaby 83 vs 90).
    fields = municipal_fields(
        billing_period_start_date_extract=fdate("2026-01-01", 0.40),
        billing_period_end_date_extract=fdate("2026-03-31", 0.90),
        number_of_days_extract=fint(83, 0.90),
    )
    r = ev(fields)
    check("low-conf printed start kept (no derivation)",
          r["writeValues"]["billing_period_start_date"] == "2026-01-01"
          and r["resolutions"]["billing_period_start_date"]["source"] == "extract",
          str(r["resolutions"].get("billing_period_start_date")))
    check("no derivation advisory for a printed start",
          not any("billing_period_start_date derived" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # FortisBC final-bill shape (260605_0006): shared-year range 'May 19 - May 31,
    # 2026' -> start extract null, start generate range-collapses to the period
    # END at low confidence. An unverified generate-only start never blocks the
    # derivation: start = 2026-05-31 - (13 - 1) = 2026-05-19.
    fields = municipal_fields(
        sub_bill_type=fstr("gas", 0.98),
        billing_period_start_date_generate=fdate("2026-05-31", 0.458),
        billing_period_end_date_generate=fdate("2026-05-31", 0.463),
        number_of_days_generate=fint(13, 0.665),
    )
    r = ev(fields)
    check("FortisBC derived start = 2026-05-19 (over failed generate)",
          r["writeValues"]["billing_period_start_date"] == "2026-05-19",
          str(r["writeValues"].get("billing_period_start_date")))
    check("FortisBC derived source/passed",
          r["resolutions"]["billing_period_start_date"]["source"] == "derived"
          and r["resolutions"]["billing_period_start_date"]["passed"] is True,
          str(r["resolutions"].get("billing_period_start_date")))
    check("FortisBC advisory names the replaced generate value",
          any("replacing unverified generate value '2026-05-31'" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # A generate start that PASSED (confident rescue of an absent extract) is
    # trusted -- derivation does not fire over it (it would compute 2026-05-22).
    fields = municipal_fields(
        billing_period_start_date_generate=fdate("2026-05-19", 0.90),
        billing_period_end_date_generate=fdate("2026-05-31", 0.90),
        number_of_days_generate=fint(10, 0.90),
    )
    r = ev(fields)
    check("passed generate start kept (no derivation)",
          r["writeValues"]["billing_period_start_date"] == "2026-05-19"
          and r["resolutions"]["billing_period_start_date"]["source"] == "generate",
          str(r["resolutions"].get("billing_period_start_date")))
    check("no derivation advisory for a passed generate start",
          not any("billing_period_start_date derived" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # Disagreeing low twins: informational -> never reviews; extract kept; advisory raised;
    # start stays blank when days are missing (no derivation possible).
    fields = municipal_fields(
        billing_period_end_date_extract=fdate("2026-03-31", 0.40),
        billing_period_end_date_generate=fdate("2026-04-24", 0.60),
    )
    r = ev(fields)
    check("disagreeing billing twins never gate routing",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("disagree advisory raised",
          any("billing_period_end_date_extract" in a and "disagree" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))
    check("extract kept on disagreement", r["writeValues"]["billing_period_end_date"] == "2026-03-31",
          str(r["writeValues"].get("billing_period_end_date")))
    check("start blank when days missing (no derivation, no default)",
          r["writeValues"]["billing_period_start_date"] == "", str(r["writeValues"].get("billing_period_start_date")))

    # Junk day counts write blank, never a guess.
    r = ev(municipal_fields(number_of_days_extract=fstr("abc", 0.9)))
    check("junk days -> blank", r["writeValues"]["number_of_days"] == "",
          str(r["writeValues"].get("number_of_days")))

    # A day count is kept only for a bill that HAS a billing period. Without one it is
    # meaningless, and the reasoning twin was observed inventing "1" (0.45-0.98) on four
    # corpus bills printing no period at all, where the extract twin correctly returned
    # nothing. Blank beats a substituted count, the same principle as the period dates.
    r = ev(municipal_fields(number_of_days_generate=fstr("30 days", 0.90)))
    check("day count with NO billing period is dropped",
          r["writeValues"]["number_of_days"] == "", str(r["writeValues"].get("number_of_days")))

    # With a period printed, the count is kept however it was read -- including from the
    # generate twin alone, the BC Hydro 'used over 30 days' shape that bug_260609_0031
    # relies on (its extract twin returns nothing on 2 runs in 3).
    period = dict(billing_period_end_date_extract=fdate("2026-05-26", 0.90))
    check("generate-only count IS kept when a period is printed",
          ev(municipal_fields(number_of_days_generate=fstr("19 days", 0.66), **period))
          ["writeValues"]["number_of_days"] == 19)
    check("sub-threshold extract count is kept (span-grounded)",
          ev(municipal_fields(number_of_days_extract=fint(19, 0.50), **period))
          ["writeValues"]["number_of_days"] == 19)
    check("derived start still uses the kept count",
          ev(municipal_fields(number_of_days_generate=fstr("19 days", 0.66), **period))
          ["writeValues"]["billing_period_start_date"] == "2026-05-08")

    # Helpers.
    check("_dates_agree across formats", field_policy._dates_agree("2026-05-07", "May 7, 2026"))
    check("_dates_agree rejects different days", not field_policy._dates_agree("2026-05-07", "2026-05-08"))
    check("_dates_agree rejects unparseable (ambiguous numeric)",
          not field_policy._dates_agree("03/04/2026", "03/04/2026"))
    check("_dates_agree rejects missing", not field_policy._dates_agree(None, "2026-05-07"))
    check("_days_agree int vs prose", field_policy._days_agree(61, "61 days"))
    check("_days_agree rejects different", not field_policy._days_agree(61, 62))
    check("_days_agree rejects missing", not field_policy._days_agree(None, 61))
    derive = field_policy.derive_billing_period_start
    check("derive Abbotsford (Apr 30, 61d) -> Mar 1",
          derive("2026-04-30", 0.85, 61, 0.90) == ("2026-03-01", 0.85))
    check("derive Surrey (Apr 29, 112d) -> Jan 8",
          derive("2026-04-29", 0.90, 112, 0.88) == ("2026-01-08", 0.88))
    check("derive parses month-name end + prose days",
          derive("Apr 30, 2026", 0.80, "61 days", 0.90) == ("2026-03-01", 0.80))
    check("derive without end -> None", derive(None, None, 61, 0.9) is None)
    check("derive without days -> None", derive("2026-04-30", 0.9, None, None) is None)
    check("derive junk days -> None", derive("2026-04-30", 0.9, "n/a", 0.9) is None)


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


def test_invoice_date_twin_and_future_gate():
    print("\n[gates: invoice_date twin -- defaults to today, future date routes to review]")

    # THE BUG: a correct date whose extract confidence lands under the bar used to be
    # replaced by today's date. Two agreeing sub-threshold twins now keep it.
    r = ev(commercial_fields(
        invoice_date_extract=fdate("2026-06-11", 0.60),
        invoice_date_generate=fdate("2026-06-11", 0.60),
    ))
    check("low twins agreeing -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE,
          r["routingDecision"])
    check("agreeing twins keep the printed date", r["writeValues"]["invoice_date"] == "2026-06-11",
          str(r["writeValues"].get("invoice_date")))
    check("invoice_date source = agreement", r["resolutions"]["invoice_date"]["source"] == "agreement",
          str(r["resolutions"].get("invoice_date")))
    check("agreement -> not defaulted", "invoice_date" not in r["defaultedFields"],
          str(r["defaultedFields"]))

    # Agreement is on the calendar day, not the printed format.
    r = ev(commercial_fields(
        invoice_date_extract=fdate("May 1, 2026", 0.50),
        invoice_date_generate=fdate("2026-05-01", 0.55),
    ))
    check("twins agree across date formats", r["writeValues"]["invoice_date"] == "2026-05-01",
          str(r["writeValues"].get("invoice_date")))

    # Extract absent -> the generate twin may NOT carry the field on its own, however
    # confident it is (observed: the reasoning twin answering with a page-footer print
    # timestamp at 0.82 on a bill that prints no issue date). Today is written instead.
    parsed = gates.parse_fields(commercial_fields(invoice_date_extract=fdate(None, None),
                                                  invoice_date_generate=fdate("2026-05-01", 0.90)))
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("ungrounded generate-only date is refused", wv["invoice_date"] == TODAY, wv["invoice_date"])
    check("refused generate -> defaulted", "invoice_date" in defaulted, str(defaulted))
    check("invoice_date is in NO_GENERATE_RESCUE",
          "invoice_date" in field_policy.NO_GENERATE_RESCUE,
          str(field_policy.NO_GENERATE_RESCUE))
    check("the refusal is invoice_date-only (invoice_number still rescues)",
          field_policy.resolve_field(
              "invoice_number",
              gates.parse_fields(municipal_fields(invoice_number_extract=fstr("", None),
                                                  invoice_number_generate=fstr("BL-9", 0.90))),
              THRESHOLD)[2] is True)

    # ... unless the SAME calendar day is printed in the document text. The extract twin
    # intermittently returns nothing on bills that plainly show their date; a grounded
    # generate value is accepted rather than substituting today.
    md = "<td>BILLING DATE:</td> <td>May 26, 2026</td> <td>DUE DATE:</td> <td>Jun 25, 2026</td>"
    r = gates.evaluate(
        cu_result(commercial_fields(invoice_date_extract=fdate(None, None),
                                    invoice_date_generate=fdate("2026-05-26", 0.60)),
                  markdown=md),
        THRESHOLD)
    check("printed date corroborates a generate-only value",
          r["writeValues"]["invoice_date"] == "2026-05-26", str(r["writeValues"].get("invoice_date")))
    check("corroborated source recorded",
          r["resolutions"]["invoice_date"]["source"] == "corroborated",
          str(r["resolutions"].get("invoice_date")))
    check("corroborated date is not a default",
          "invoice_date" not in r["defaultedFields"], str(r["defaultedFields"]))
    check("corroboration advisory raised",
          any("printed in the document text" in a for a in r["advisoryFlags"]), str(r["advisoryFlags"]))

    # The business_license case: the only date-shaped text is a slashed page-footer print
    # timestamp, which must never ground a value -- the date still defaults to today.
    r = gates.evaluate(
        cu_result(commercial_fields(invoice_date_extract=fdate(None, None),
                                    invoice_date_generate=fdate("2026-01-13", 0.82)),
                  markdown="<!-- PageFooter: 1/13/26 10:12AM -->"),
        THRESHOLD)
    check("a slashed footer timestamp does not corroborate",
          r["writeValues"]["invoice_date"] != "2026-01-13", str(r["writeValues"].get("invoice_date")))
    check("uncorroborated generate still defaults",
          "invoice_date" in r["defaultedFields"], str(r["defaultedFields"]))

    # A corroborated date in the future still goes to a human.
    r = gates.evaluate(
        cu_result(commercial_fields(invoice_date_extract=fdate(None, None),
                                    invoice_date_generate=fdate("2099-12-31", 0.60)),
                  markdown="Invoice date: December 31, 2099"),
        THRESHOLD)
    check("corroborated future date still routes to review",
          r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD, r["routingDecision"])

    # helper: unambiguous printed forms ground a date; ambiguous/absent ones do not.
    corr = field_policy.date_corroborated_in_text
    check("month-name form", corr("2026-05-26", "BILLING DATE: May 26, 2026") == "2026-05-26")
    check("abbreviated month", corr("2026-06-17", "<td>Jun 17, 2026</td>") == "2026-06-17")
    check("full month name", corr("2026-06-17", "issued June 17, 2026") == "2026-06-17")
    check("day-first form", corr("2026-05-26", "dated 26 May 2026") == "2026-05-26")
    check("ISO form", corr("2026-05-26", "date 2026-05-26 ok") == "2026-05-26")
    check("date split across an OCR line break",
          corr("2026-05-26", "from May\n26, 2026 to") == "2026-05-26")
    check("slashed short form never corroborates",
          corr("2026-01-13", "<!-- PageFooter: 1/13/26 10:12AM -->") is None)
    check("a date absent from the text does not corroborate",
          corr("2010-03-26", "Payment must be received by December 31, 2025") is None)
    check("empty text does not corroborate", corr("2026-05-26", "") is None)
    check("unparseable value does not corroborate", corr(None, "May 26, 2026") is None)
    check("a different day in the same month does not corroborate",
          corr("2026-05-27", "BILLING DATE: May 26, 2026") is None)

    # A generate that disagrees never overrides a passing extract.
    r = ev(commercial_fields(invoice_date_extract=fdate("2026-05-01", 0.95),
                             invoice_date_generate=fdate("2026-05-31", 0.90)))
    check("passing extract wins over a disagreeing generate",
          r["writeValues"]["invoice_date"] == "2026-05-01", str(r["writeValues"].get("invoice_date")))
    check("disagreement surfaced as an advisory",
          any("invoice_date_extract/invoice_date_generate disagree" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))

    # Both twins below the bar and disagreeing -> today, recorded as defaulted. Still
    # happy path: invoice_date is not a critical field.
    r = ev(commercial_fields(invoice_date_extract=fdate("2026-05-01", 0.40),
                             invoice_date_generate=fdate("2026-05-31", 0.40)))
    check("unresolved twins -> still happy (invoice_date is not critical)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("unresolved twins -> defaulted", "invoice_date" in r["defaultedFields"],
          str(r["defaultedFields"]))

    # Both twins absent -> today (the agreed fallback).
    fields = commercial_fields()
    del fields["invoice_date_extract"]
    parsed = gates.parse_fields(fields)
    wv, defaulted = field_policy.build_write_values(parsed, THRESHOLD, now=FIXED_NOW)
    check("no invoice date at all -> today (PST)", wv["invoice_date"] == TODAY, wv["invoice_date"])
    check("today substitution recorded", "invoice_date" in defaulted, str(defaulted))

    # An invoice dated after today goes to a human, with the read value left intact.
    r = ev(commercial_fields(invoice_date_extract=fdate("2099-12-31", 0.95)))
    check("future invoice_date -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD,
          r["routingDecision"])
    check("review summary names invoice_date",
          r["reviewReasons"] == ["invoice_date needs attention"], str(r["reviewReasons"]))
    check("future date advisory carries the value",
          any("B4 invoice_date 2099-12-31 is after today" in a for a in r["advisoryFlags"]),
          str(r["advisoryFlags"]))
    check("future date is written unchanged for the reviewer",
          r["writeValues"]["invoice_date"] == "2099-12-31", str(r["writeValues"].get("invoice_date")))
    check("future date not marked defaulted", "invoice_date" not in r["defaultedFields"],
          str(r["defaultedFields"]))

    # A future date the twins could not verify defaults to today, so the gate cannot
    # fire on a substituted value.
    r = ev(commercial_fields(invoice_date_extract=fdate("2099-12-31", 0.40)))
    check("unverified future date -> defaulted, not review",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("substituted date is today, never the future value",
          r["writeValues"]["invoice_date"] != "2099-12-31" and "invoice_date" in r["defaultedFields"],
          str(r["writeValues"].get("invoice_date")))

    # helper: the comparison is calendar-day based in the business timezone.
    future = field_policy.invoice_date_in_future
    check("tomorrow is in the future", future("2026-06-30", now=FIXED_NOW))
    check("today is not in the future", not future(TODAY, now=FIXED_NOW))
    check("yesterday is not in the future", not future("2026-06-28", now=FIXED_NOW))
    check("missing value is not in the future", not future(None, now=FIXED_NOW))
    check("unparseable value is not in the future", not future("03/04/2026", now=FIXED_NOW))


def test_sub_bill_type():
    print("\n[field_policy + gates: sub_bill_type -- informational; municipal label bar 0.80, commercial PO prefix]")
    resolve = field_policy.resolve_sub_bill_type
    check("sub_bill_type bar is 0.80 (separate from critical THRESHOLD)",
          field_policy.SUB_BILL_TYPE_THRESHOLD == 0.80)

    # municipal: only the four municipal sub-types pass, at/above the bar.
    for label in field_policy.MUNICIPAL_SUB_TYPES:
        check(f"municipal {label} at 0.80 -> {label}",
              resolve("municipal", label, 0.80, None) == label)
    check("municipal label normalised (case/space)", resolve("municipal", " Water ", 0.9, None) == "water")
    check("municipal below bar -> other", resolve("municipal", "gas", 0.79, None) == "other")
    check("municipal None confidence -> other", resolve("municipal", "gas", None, None) == "other")
    check("municipal repair (cross-bucket) -> other",
          resolve("municipal", "repair", 0.95, "11024580") == "other")
    check("municipal unknown label -> other", resolve("municipal", "property_tax", 0.95, None) == "other")
    check("municipal empty label -> other", resolve("municipal", "", 0.95, None) == "other")

    # commercial: the format-valid resolved PO's prefix decides (330 -> service,
    # 110 -> repair); the classified label and its confidence are ignored.
    check("commercial 330 PO -> service", resolve("commercial", None, None, "33001022") == "service")
    check("commercial 110 PO -> repair", resolve("commercial", None, None, "11024580") == "repair")
    check("commercial label ignored (repair label + 330 PO -> service)",
          resolve("commercial", "repair", 0.95, "33001022") == "service")
    check("commercial label ignored (gas label + 110 PO -> repair)",
          resolve("commercial", "gas", 0.95, "11024580") == "repair")
    check("commercial confidence ignored (below-bar label + 110 PO -> repair)",
          resolve("commercial", "service", 0.50, "11024580") == "repair")
    check("commercial without PO -> other", resolve("commercial", "repair", 0.95, None) == "other")
    check("commercial empty PO -> other", resolve("commercial", "repair", 0.95, "") == "other")
    check("commercial format-violating PO -> other",
          resolve("commercial", "repair", 0.95, "JOB-4471") == "other")
    check("commercial malformed 33-prefix PO -> other",
          resolve("commercial", None, None, "3345") == "other")

    # agreement twin: a matching generate label corroborates a below-bar classify
    # label (estimated confidence is noisy; two independent reads agreeing are not).
    check("below-bar + agreeing generate -> label",
          resolve("municipal", "gas", 0.51, None, "gas") == "gas")
    check("agreement is case/space-insensitive",
          resolve("municipal", "electric", 0.30, None, " Electric ") == "electric")
    check("below-bar + disagreeing generate -> other",
          resolve("municipal", "gas", 0.79, None, "electric") == "other")
    check("below-bar + missing generate -> other",
          resolve("municipal", "gas", 0.79, None, None) == "other")
    check("agreement never overrides bucket rules (municipal repair) -> other",
          resolve("municipal", "repair", 0.50, "11024580", "repair") == "other")
    check("commercial ignores agreement too (agreeing twins, no PO) -> other",
          resolve("commercial", "repair", 0.95, None, "repair") == "other")
    check("empty labels never agree", resolve("municipal", "", 0.30, None, "") == "other")
    check("generate never supplies the label alone",
          resolve("municipal", "", 0.95, None, "gas") == "other")

    # end-to-end: response + writeValues carry the resolved sub-type.
    r = ev(commercial_fields())
    check("commercial fixture (110 PO) -> subBillType repair",
          r["subBillType"] == "repair", str(r.get("subBillType")))
    check("writeValues carries sub_bill_type", r["writeValues"]["sub_bill_type"] == "repair",
          str(r["writeValues"].get("sub_bill_type")))
    check("fields.sub_bill_type carries the raw label + confidence",
          r["fields"]["sub_bill_type"] == {"value": "repair", "confidence": 0.9},
          str(r["fields"].get("sub_bill_type")))

    r = ev(commercial_fields(po_or_job_number_extract=fstr("33001022", 0.91)))
    check("commercial 330 PO -> subBillType service", r["subBillType"] == "service",
          str(r.get("subBillType")))
    check("writeValues carries service", r["writeValues"]["sub_bill_type"] == "service",
          str(r["writeValues"].get("sub_bill_type")))

    r = ev(municipal_fields())
    check("municipal fixture -> business_license", r["subBillType"] == "business_license",
          str(r.get("subBillType")))
    # content-based water demotion (pure helpers): a 'water' label only stands
    # when the document text names a water/sewer/stormwater service.
    check("water text indicates water service",
          field_policy.water_service_indicated("Water consumption 12 m3"))
    check("sewer counts as a water service",
          field_policy.water_service_indicated("Sewer utility levy 5.00"))
    check("stormwater counts as a water service",
          field_policy.water_service_indicated("Stormwater drainage charge"))
    check("fireline + street cleaning names no water service",
          not field_policy.water_service_indicated("Annual Fireline (100mm)\nStreet Cleaning"))
    check("demote 'water' when text names no water service",
          field_policy.demote_non_water_sub_type("water", "Fireline 564\nStreet Cleaning 144") == "other")
    check("keep 'water' when text names water",
          field_policy.demote_non_water_sub_type("water", "Water consumption 12 m3") == "water")
    check("demotion is a no-op for non-water labels",
          field_policy.demote_non_water_sub_type("electric", "no service named here") == "electric")
    check("'water' with empty text -> other (no service named)",
          field_policy.demote_non_water_sub_type("water", "") == "other")

    # end-to-end: a genuine water bill names water in its OCR text -> stays water.
    r = gates.evaluate(cu_result(
        municipal_fields(sub_bill_type=fstr("water", 0.9),
                         sub_bill_type_generate=fstr("water", 0.85)),
        markdown="City Utility Bill\nWater consumption 12 m3 45.00\nSewer 5.00"), THRESHOLD)
    check("municipal water label + water in text -> water",
          r["subBillType"] == "water", str(r.get("subBillType")))

    # a city bill CU labels 'water' whose only charges are a fireline fee and
    # street cleaning names no water service -> demoted to other.
    r = gates.evaluate(cu_result(
        municipal_fields(sub_bill_type=fstr("water", 0.9),
                         sub_bill_type_generate=fstr("water", 0.85)),
        markdown="City Utility Bill\nAnnual Fireline (100mm) 564.00\nStreet Cleaning 144.00"), THRESHOLD)
    check("municipal water label, no water service in text -> other",
          r["subBillType"] == "other", str(r.get("subBillType")))

    # below-bar label whose generate twin disagrees -> other, and never gates routing.
    r = ev(municipal_fields(sub_bill_type=fstr("gas", 0.50)))
    check("below-bar unconfirmed label -> other", r["subBillType"] == "other", str(r.get("subBillType")))
    check("sub_bill_type never gates routing", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

    # below-bar label with an agreeing generate twin -> accepted end-to-end.
    r = ev(municipal_fields(sub_bill_type=fstr("gas", 0.50),
                            sub_bill_type_generate=fstr("gas", 0.40)))
    check("below-bar + agreeing twin -> label accepted", r["subBillType"] == "gas",
          str(r.get("subBillType")))

    # absent classify field: commercial derives from the PO regardless; municipal
    # has no label to trust -> other. Neither gates.
    fields = commercial_fields()
    del fields["sub_bill_type"]
    r = ev(fields)
    check("absent sub_bill_type (commercial) -> PO-derived repair",
          r["subBillType"] == "repair", str(r.get("subBillType")))
    check("absent sub_bill_type does not gate", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    fields = municipal_fields()
    del fields["sub_bill_type"]
    r = ev(fields)
    check("absent sub_bill_type (municipal) -> other", r["subBillType"] == "other",
          str(r.get("subBillType")))

    # commercial bill with no usable PO -> other (the invoice reviews on the PO
    # anyway, but the written sub-type must not claim service or repair).
    r = ev(commercial_fields(po_or_job_number_extract=fstr("", None)))
    check("commercial without PO -> subBillType other", r["subBillType"] == "other",
          str(r.get("subBillType")))

    # the OCR PO rescue drives the sub-type: twins empty, one candidate in markdown.
    r = gates.evaluate(
        cu_result(commercial_fields(po_or_job_number_extract=fstr("", None)),
                  markdown="Job# 11024580"),
        THRESHOLD)
    check("OCR-rescued 110 PO -> repair", r["subBillType"] == "repair", str(r.get("subBillType")))
    check("rescued path stays happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    r = gates.evaluate(
        cu_result(commercial_fields(po_or_job_number_extract=fstr("", None)),
                  markdown="PO# 33001022"),
        THRESHOLD)
    check("OCR-rescued 330 PO -> service", r["subBillType"] == "service", str(r.get("subBillType")))


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
    test_sub_bill_type()
    test_b4_review_summary()
    test_po_ocr_rescue()
    test_billing_period_twins_and_derivation()

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
