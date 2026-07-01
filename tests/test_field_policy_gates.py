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


def cu_result(fields, category="general_invoice", analyzer="generalinvoice"):
    """contents[0] = router result (segment category); contents[1] = child fields."""
    return {
        "contents": [
            {"segments": [{"category": category}]},
            {"analyzerId": analyzer, "category": category, "fields": fields},
        ]
    }


def commercial_fields(**overrides):
    base = {
        "vendor_name_extract": fstr("Bob's Plumbing Ltd.", 0.97),
        "service_address_extract": fstr("123 Main St, Vancouver BC", 0.95),
        "total_invoice_amount_extract": fnum(105.0, 0.96),
        "po_or_job_number_extract": fstr("00471234", 0.91),
        "gst_amount_extract": fnum(5.0, 0.93),
        "invoice_date": fdate("2026-05-01", 0.95),
        "payment_due_date": fdate("2026-05-31", 0.94),
        "invoice_number": fstr("INV-2201", 0.92),
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
        # municipal bills usually carry no PO and no GST
        "invoice_date": fdate("2026-05-10", 0.95),
        "payment_due_date": fdate("2026-06-10", 0.94),
        "bill_type": fstr("municipal", 0.9),
        "is_handwritten": fstr("no", 0.97),
        "invoice_description": fstr("Annual business license renewal.", 0.8),
        "anomaly_flag": fstr("", None),
    }
    base.update(overrides)
    return base


def ev(fields, category="general_invoice"):
    return gates.evaluate(cu_result(fields, category=category), THRESHOLD)


# --- field_policy unit checks ------------------------------------------------


def test_policy_constants_and_buckets():
    print("\n[field_policy: buckets and criticality]")
    check("threshold is 0.73", THRESHOLD == 0.73, str(THRESHOLD))
    check("base critical = vendor/address/total",
          field_policy.BASE_CRITICAL == ("vendor_name", "service_address", "total_invoice_amount"))
    check("commercial delta = po + gst",
          field_policy.COMMERCIAL_DELTA == ("po_or_job_number", "gst_amount"))

    commercial = field_policy.critical_fields("commercial")
    municipal = field_policy.critical_fields("municipal")
    check("commercial critical has 5 fields incl po + gst",
          set(commercial) == {"vendor_name", "service_address", "total_invoice_amount",
                              "po_or_job_number", "gst_amount"}, str(commercial))
    check("municipal critical = base only (no po, no gst)",
          set(municipal) == {"vendor_name", "service_address", "total_invoice_amount"}, str(municipal))

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

    # po_or_job_number format: exactly 8 numeric digits (analyzer prompt + B4 gate)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("12345678", 0.95)))
    check("commercial 8-digit PO -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    r = ev(commercial_fields(po_or_job_number_extract=fstr("00471234", 0.95)))
    check("commercial 8-digit PO with leading zero -> happy", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("JOB-4471", 0.95)))
    check("commercial alphanumeric PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    check("PO format review reason names the field",
          any("po_or_job_number" in x for x in r["reviewReasons"]), str(r["reviewReasons"]))
    r = ev(commercial_fields(po_or_job_number_extract=fstr("1234567", 0.95)))
    check("commercial 7-digit PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)
    r = ev(commercial_fields(po_or_job_number_extract=fstr("123456789", 0.95)))
    check("commercial 9-digit PO -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)


# --- municipal routing -------------------------------------------------------


def test_municipal_routing():
    print("\n[gates: municipal bucket]")
    r = ev(municipal_fields())
    check("municipal happy path (no PO/GST present)", r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("bucket resolved municipal", r["policyBucket"] == "municipal")

    # municipal does NOT require PO or GST -> still happy even absent
    r = ev(municipal_fields(invoice_number=fstr("", None)))
    check("municipal missing invoice_number -> still happy (non-critical)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE)

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

    # DOCUMENTED RESIDUAL: a trade invoice (no PO, no GST) misclassified municipal
    # gets the relaxed bucket and auto-writes. Defended only at the prompt layer
    # (issuer-not-customer tie-breaker) + base+delta, never by this gate.
    trade_as_municipal = {
        "vendor_name_extract": fstr("Bob's Plumbing Ltd.", 0.97),
        "service_address_extract": fstr("123 Main St", 0.95),
        "total_invoice_amount_extract": fnum(105.0, 0.96),
        "bill_type": fstr("municipal", 0.9),  # WRONG label
        "is_handwritten": fstr("no", 0.97),
    }
    r = ev(trade_as_municipal)
    check("RESIDUAL: trade mislabeled municipal auto-writes (known, prompt-defended)",
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
    # decision enum is exactly the agreed reduced set
    allowed = {gates.HAPPY_PATH_CANDIDATE, gates.REVIEW_B4_CRITICAL_FIELD,
               gates.REJECT_B2_OTHER_CATEGORY, gates.REVIEW_NO_CHILD_EXTRACTION}
    check("routingDecision in reduced enum", r["routingDecision"] in allowed)


def test_field_format_rules():
    print("\n[field_policy: per-field format rules]")
    vr = field_policy.format_violation_reason
    check("8-digit po ok", vr("po_or_job_number", "12345678") is None)
    check("leading-zero 8-digit po ok", vr("po_or_job_number", "00471234") is None)
    check("7-digit po violates", vr("po_or_job_number", "1234567") is not None)
    check("9-digit po violates", vr("po_or_job_number", "123456789") is not None)
    check("alphanumeric po violates", vr("po_or_job_number", "JOB-4471") is not None)
    check("empty po is not a format violation", vr("po_or_job_number", "") is None)
    check("None po is not a format violation", vr("po_or_job_number", None) is None)
    check("field without a rule is always ok", vr("vendor_name", "anything") is None)


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
        po_or_job_number_extract=fstr("00471234", 0.60),
        po_or_job_number_generate=fstr("00471234", 0.60),
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
        po_or_job_number_generate=fstr("12345678", 0.95),
    )
    r = ev(fields)
    check("po extract empty + confident generate -> happy (rescue)",
          r["routingDecision"] == gates.HAPPY_PATH_CANDIDATE, r["routingDecision"])
    check("po rescue source = generate", r["resolutions"]["po_or_job_number"]["source"] == "generate")
    check("po rescue writes the generate value", r["writeValues"]["po_or_job_number"] == "12345678",
          str(r["writeValues"].get("po_or_job_number")))

    # po: extract empty, generate present but LOW confidence -> review (nothing corroborates it).
    r = ev(commercial_fields(
        po_or_job_number_extract=fstr("", None),
        po_or_job_number_generate=fstr("12345678", 0.50),
    ))
    check("po extract empty + low generate -> review", r["routingDecision"] == gates.REVIEW_B4_CRITICAL_FIELD)

    # numeric / po agreement helpers.
    check("amounts equal to the cent agree", field_policy._amounts_agree(228.08, 228.084))
    check("amounts differing by a cent do not agree", not field_policy._amounts_agree(228.08, 228.09))
    check("None amount does not agree", not field_policy._amounts_agree(228.08, None))
    check("same digit sequences agree", field_policy._po_values_agree("00471234", "00471234"))
    check("different digits do not agree", not field_policy._po_values_agree("00471234", "00471235"))
    check("empty po does not agree", not field_policy._po_values_agree("", "12345678"))


def main():
    test_policy_constants_and_buckets()
    test_field_format_rules()
    test_date_defaulting_and_derivation()
    test_commercial_routing()
    test_municipal_routing()
    test_default_and_residual()
    test_handwriting_b2_nochild()
    test_response_shape()
    test_vendor_extract_generate_twin()
    test_service_address_extract_generate_twin()
    test_amount_and_po_twins()

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
