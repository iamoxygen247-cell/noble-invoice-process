# Open defects — running list

Defects found but **not fixed**, with the evidence behind each. Deferred deliberately so the
work in flight stays reviewable; this file is the backlog they were deferred into.

> **Scope (user, 2026-08-19):** anything whose only impact is on the written Dynamics/Dataverse
> record — mapping, column shape, normalisation — is a **TODO in `docs/ai/dataverse-todo.md`**,
> not a defect here. A finding that is both gets its extraction half logged here and its
> write half there. Items 14 and 15 moved out under this rule; the *Resolved* history and the
> Dataverse impact index below are kept as written.

**How to use this file.** Add a row when you find a defect you are not fixing now. Record the
measured rate, not an impression — "5/10 runs" is actionable, "sometimes" is not. When a defect
is fixed, move it to *Resolved* with the commit sha rather than deleting it, so a future
regression on the same document is recognisable.

**Conventions.** Corpus documents are named by their `tests/pre-commit-test/<stem>` sidecar. A
measured rate like `5/10` means 5 of 10 replicate CU calls on the same document and analyzer.
Rates in this file were measured on analyzer hash `cd1e585c2f1f` (2026-08-17) unless stated.

Last updated: 2026-09-10 — **`commercial-narrative-v22`**: the day count no longer turns an
invented `1` into the period length. See *Resolved* → **A2 + B3 follow-on (2)**: 45 of 3,994
cached decisions changed, `number_of_days` only, no routing change. It was found by the live
re-roll of 2026-09-10 (`regress.py --force`, 159 CU calls: 1144 / 1146). That run's other
failure is not a defect: `260901_rogers.vendor_name` read the page-3 issuer `Rogers Communications
Canada Inc.` instead of the payable-to `Shaw Cablesystems` on 3 of 3 reads, with the same analyzer
definition that read Shaw on 12 of 12 the day before. The user accepted both names, and
`regress.py` now reads a sidecar value `A || B` as either. Corpus after both changes: **1146 /
1146 across 53 documents** (53 with `property_richmond2`, added the same day with
`commercial-narrative-v21`). Code-side only: a function deploy, no analyzer push.

Previous watermark: 2026-09-09 (later) — **`commercial-narrative-v20`**: the analyzer gained
`folio_number_extract` / `folio_number_generate` (critical on property tax notices ONLY, via
`field_policy.PROPERTYTAX_DELTA`), and the `total_invoice_amount` prompt gained the property-tax
paragraph that **closes A11 and A14 together** (no-grant column + arrears are payable). Code-side:
`account_number` is taken from the folio on a property tax notice that prints both (user
requirement — 11 reads, 4 documents), the A13e vendor rescue was widened to the above-threshold
regime it was missing (**A13e part 2** — that one auto-wrote a wrong vendor, D1), and
`folio_number` is blanked off property tax notices (**A13g**). **A15 is fixed** in the same change
(below). Corpus: **1124 OK / 0 not OK across 52 documents, 1124 assertions**, green on BOTH n=3
samples and on all six reads per document. **`warranty_260120_0062.account_number`, the
long-standing blocker, now reads correctly — see the caveat in A13.** **This change needs BOTH a
prod analyzer push and a function deploy, analyzer FIRST.**

> **The corpus was re-rolled LIVE against this definition (156 CU calls, 0 cache hits) and the
> result is the strongest "lucky draw" evidence this project has.** The frozen sample and the live
> sample are both n=3 of the *same* definition `b5b984bc1a88`, and **each was green on the other's
> failures**: the frozen roll failed only `bug_260629_0012.routingDecision`, while the live roll
> failed `property_delta.routingDecision`, `property_west_vancouver.routingDecision` and
> `property_surrey.billing_period_start_date` — and passed `bug_260629_0012` 3/3. Scored together at
> **n=6**, four keys were unstable, none consistently wrong (three at 5/6, one at 4/6). Two of those
> were real code defects, now fixed; one was withdrawn by the user; one (A15) is fixed. **Never
> treat a green n=3 as a stable corpus.**

The live roll also exposed **a defect in the folio→`account_number` requirement as first
implemented**: `build_write_values` set `write[account_number]` from the folio but never updated the
`resolutions` map, so `evaluate_b4` still judged the *account twins the folio had replaced*. When CU
returned no `account_number` at all (a blank field entry — confidence, no value) while
`folio_number` came back at 0.875–0.988 **from the same span on the same read**, the notice routed
`REVIEW_B4_CRITICAL_FIELD` even though the record it was about to write was complete and correct.
Measured **1 read in 6** on `property_delta` and `property_west_vancouver`. Fixed in `gates.py` by
pointing the account resolution at the folio, after the PO-echo discard (the last rule that can
change `account_number`) and before B4. **Not a relaxation** — `propertytax_account_from_folio`
still returns `None` unless the folio passed its own bar, so a missing or weak folio reviews exactly
as before. Blast radius over 312 reads: applied on **120 reads, 0 outside `municipal` +
`propertytax`**, and changed the outcome on exactly the **2** reads above.

**Authorised sidecar removals** (user, 2026-09-09), each recorded in the affected note:
`260901_rogers.invoice_description`; `260825_telus.payment_due_date`; `number_of_days` from all 20
`property_*` sidecars; and **`billing_period_start_date` + `billing_period_end_date` from all 20
`propertytax` sidecars** — the Power Automate flow does not consume the billing period for property
tax. That last removal takes 40 assertions and, stated plainly, hides the A13b failure chain
(B3's invented `number_of_days=1` derives `start = end − 1 day`, collapsing the period and blanking a
correct `2026-01-01`, measured 4 of 6 reads correct) from the corpus on every tax notice. The chain
is unfixed. `property_white_rock.routingDecision` was **restored** (user request; green 3/3 on the
frozen sample and 3/3 live).
Previous watermark: 2026-09-09 — **the corpus grew from 38 documents to 52**: 14 Greater Vancouver property
tax notices were added on request, taking `propertytax` coverage from 6 documents to 20 and the
expectation count from 815 to 1137. All 14 verified against their printed pages and green. Three more
A13 rows closed in code (`commercial-narrative-v19`): **A13d** `diag_260414_0028.vendor_name`
(commercial printed-name rule, 30 reads changed), **A13e** `property_vancouver.vendor_name` →
routing (property-tax vendor rescue, 1 read), and **A13f** `property_ubc2.vendor_name` (a leading
"the" in `_normalize_vendor`, 6 reads). **No sidecar was edited and `analyzers/` is untouched**, so no
prod analyzer push is needed. **Only one A13 row is still open** —
`warranty_260120_0062.account_number` (8/9) — and it is now the *only* failing key in the whole
corpus. New item **A14**: `total_invoice_amount_extract` sweeps in a *small* arrears line on
`property_white_rock` (5 of 12), which routes to review rather than writing wrongly. **A11 re-measured
on all 20 tax notices** — every one picked column A, but by coincidence rather than by construction;
documentation only, per the user's decision.
Previous watermark: 2026-09-08 (later still) — **A13 partially resolved**: the two `abbotsford_water`
billing-period keys are fixed in code (`commercial-narrative-v18`,
`field_policy.billing_period_extracts_collapsed`), and the `property_surrey`
`billing_period_start_date` row was **withdrawn — it was never a defect**, the notice does print
the period and the sidecar has been re-baselined. Three factual errors in the original A13 entry
are corrected below. A three-arm prompt experiment (n=12 each, same day) was run first and found
the prompt is **not** the lever, so `analyzers/` is untouched and no prod analyzer push is needed.
`property_surrey.service_address` was also **accepted** as the new truth the same day (A13c): the
shorter `2790 167 ST` is what the notice prints under its own PROPERTY ADDRESS label, and the old
assertion had spliced in the mailing block's city and postal code. **Three A13 rows remain open.**
Previous watermark: 2026-09-08 (later) — new item **A13**: the CU
deployment was upgraded from GPT-5.2 to GPT-5.5 and extraction quality dropped on six keys across
five documents (rates 1/9 to 9/9, all measured post-upgrade). The analyzer was **not** changed —
same definition hash either side of the upgrade. A seventh symptom from the same investigation, CU
returning the literal string `"null"` and defeating the municipal invoice-number fallback, had one
root cause and **was** fixed (`commercial-narrative-v17`, `gates._denull`). Previous watermark:
2026-09-08 — new item **A12**: a bill addressed only to Noble's own office loses
`service_address` and always routes to review (measured 5/5 on the new `260825_telus` anchor).
Found while shipping the vendor-driven `bill_type` rule (`commercial-narrative-v16`), which is
code-side and needed no analyzer push; the record half of that change is `dataverse-todo.md` →
DV-10. Previous watermark: 2026-08-27 (later) — **A8 closed as accepted behaviour, not a defect**: a blank
`invoice_date` always becomes today's date, in **both** buckets (user). Two attempts to change that
— blank everywhere (`v12`, committed as 11a530c), then blank on municipal only (`v13`, never
committed) — were **reverted**; neither reached production. The shipped version is `v14`. The measurement is kept because it is useful:
`business_license` writes today's date on 121/121 cached reads and three of the six property tax
notices print no issue date at all. **C6 closed**
without change — six property tax notices classify correctly on the live router, so the `other`
wording is not the defect. The **B8 backfill of 25 historical rejects is closed unstarted**: the
PDFs are only in SharePoint (Graph returns 403 on files), the review queue is live so they were
already re-keyed by hand, and the ledger cannot say which reached Dynamics — so reprocessing risked
duplicate records for no certain gain. `out/b2-backfill-list.csv` retains the work list if it is
ever revived. Previous watermark: **B8** (router `other` was a terminal reject; 26 of 30 such rejects were real payables) fixed code-side with the B2 rescue, no analyzer push. Its prompt-wording half is deferred as new item **C6**. Previous watermark: 2026-08-26 (sha 23c36a9: the `po_or_job_number` prefix rule relaxed from `110`/`330` to `11`/`33`; analyzer and function app both verified live at HEAD. New item C5 records that no real document exercises the widened range. Previous watermark: 2026-08-19, sha 9406364 — stage D2, see docs/ai/warranty-prompt-retry-plan.md).

---

## Status: Dataverse is not in use, so 11 of the 16 are deferred

**Decision (user, 2026-08-17): the Dataverse data is not being used yet, so the defects that
only corrupt a Dataverse record are acceptable for now.** They are not closed and not
forgotten — they are **Phase 4 blockers**, listed in P2 below and to be fixed before the
Dataverse write ships. There is no backfill, so anything extracted wrongly before then stays
wrong.

Of the 16 defects: **11 affect Dataverse only** (7 write a silently wrong value, 3 write an
inconsistent one, 1 could fail the row write) and **5 have impact today** regardless of
Dataverse. Work the P1 list.

**2026-08-18: B1, B6b and B6c are fixed** (code-side, no analyzer push — see *Resolved*).
That closes 2 of the 5 non-Dataverse defects and the address half of the D2 normalisation
gap, and it let the corpus re-assert `service_address` on all 29 documents.

**2026-08-18, second pass: A3 and A4 are fixed** — also code-side. And the premise above is
now known to be **wrong**: the user confirmed a live Power Automate flow that consumes
`writeValues` and updates Dynamics today. The D1 defects are therefore **not latent** —
they are writing wrong values into real records right now. A4 and A5 were doing so on every
run. The remaining D1 work (A1, A2, A5, B3, B5) should be re-prioritised as live data
corruption, not Phase 4 preparation, and this file's "Where the data actually goes" section
below is stale wherever it says the values stop at the response.

---

## P1 — Impact today, independent of Dataverse

Ordered by what it costs you now.

| # | ID | Defect | Cost today | Fix shape |
|---|---|---|---|---|

## P2 — Deferred until Phase 4 (Dataverse only)

Fix **before** the Dataverse write goes live, not before that.

| # | ID | Class | Defect |
|---|---|---|---|
| 14 | B2 + B6a + B7 | D2 | **Moved to `docs/ai/dataverse-todo.md` → DV-4** (2026-08-19). Vendor-name inconsistency — nothing normalises the *name* (legal suffix, case). The address half (B6b) was fixed on 2026-08-18: `build_write_values` now collapses whitespace in `service_address` as it already did for `vendor_name` |

## P3 — Already mitigated

| # | ID | Class | Status |
|---|---|---|---|
| 15 | C4 | D3 | **Moved to `docs/ai/dataverse-todo.md` → DV-5** (2026-08-19). Narrative limits are prompt-only. The overshoot behind it is gone: the D2 warranty edit cut the longest value from 435 to 255 against a 400 limit, which also supersedes stage D1 of the retry plan |

---

## Where the data actually goes (read this before judging severity)

Nothing persists the extracted field values to a record store **today**:

| Store | What it holds |
|---|---|
| Azure Table `InvoiceExtractProcessLog` (`ledger.py`) | Processing state only — Status, RoutingDecision, BillType, PolicyVersion, DefaultedFields, blob pointers, timings. **No invoice field values.** |
| Blob container `invoice-diagnostics` | The raw CU JSON and the decision JSON (which *does* contain `writeValues`), as evidence. 90-day lifecycle rule. Not queryable as a record. |
| Dataverse invoice table | **Does not exist yet — Phase 4 is unbuilt.** `function_app.py`: "It does NOT write to Dataverse." The ledger's `DynamicsRecordId` column is reserved for a 201 that never comes today. |

So a wrong `writeValues` entry currently lands in an HTTP response and a diagnostic blob, and
then stops. **The Severity A defects are latent, not actively corrupting anything.** They become
live the moment the Phase 4 Dataverse write ships, and there is no backfill for invoices
processed before then — which makes them **Phase 4 blockers**, not production incidents.

---

## Dataverse impact index

Which defects corrupt a **stored Dataverse record** once Phase 4 ships, and which do not. Two
things decide it: whether the field is in `field_policy.WRITE_FIELDS` (so it gets a Dataverse
column at all), and whether it is a **critical field** for that bill's bucket (so a bad value
trips gate B4 and a human sees it instead of it being auto-written).

Critical fields: `vendor_name`, `service_address`, `total_invoice_amount` always; plus
`po_or_job_number` + `gst_amount` on commercial, `account_number` + `invoice_number` on
municipal. Everything else is auto-written without review.

### D1 — Dataverse, silently wrong (fix before Phase 4)

The value is written to a column, it is wrong, and nothing routes it to review. This is the set
that actually corrupts records.

| ID | Dataverse column | Why it is not caught |
|---|---|---|

### D2 — Dataverse, inconsistent rather than wrong (data quality)

The stored value is defensible on every run, but the *same real-world entity* is stored under
different strings across invoices, which breaks grouping, dedupe and vendor reporting.

| ID | Dataverse column | Variation |
|---|---|---|
| B2 | Vendor Name | `FortisBC Energy Inc.` vs `FortisBC - Natural gas` |
| B6a | Vendor Name | `DISTRICT OF WEST VANCOUVER` vs `District of West Vancouver` |
| B6b | Service Address | same address, line-broken differently |
| B7 | Vendor Name | `CAMBIE ROOFING CONTRACTORS LTD.` vs `CAMBIE ROOFING CONTRACTORS` — legal suffix dropped |

All four are the same underlying gap: **nothing normalises the written vendor name or address.**
`build_write_values` collapses whitespace in `vendor_name` and that is all. A single
normalisation step (case-fold for comparison, strip legal suffixes, collapse address line
breaks) would fix B2, B6a, B6b and B7 together *and* let the corpus assert those keys again —
they are currently unasserted only because exact string comparison cannot tolerate the variance.

### D3 — Dataverse, the write itself could fail (loud, not silent)

| ID | Dataverse column | Effect |
|---|---|---|
| C4 | Diagnosis / Recommendation / Warranty | an over-length value is rejected by the column and **the whole row write fails**, losing the invoice. Mitigated by sizing the columns at 1000/1000/500 against 800/800/400 targets |

### D4 — Not Dataverse-related

| ID | Why not |
|---|---|
| B1 | *(fixed 2026-08-18)* `service_address` is critical, so an intermittent null routed to `REVIEW_B4_CRITICAL_FIELD` — a human saw it and no row was written. Cost review churn, not data integrity |
| B4 | `total_invoice_amount_generate` is a raw twin, not in `WRITE_FIELDS`; the resolved total stays correct |
| B6c | *(fixed 2026-08-18)* `business_license` routing flip — affected which queue the invoice entered, not any stored value |
| A12 | `service_address` is critical, so discarding Noble's own office routes to `REVIEW_B4_CRITICAL_FIELD` — a human sees it and no row is auto-written. Same shape as B1: review churn, not data integrity |
| A13 (part) | `warranty_260120_0062.account_number` is critical, so the GPT-5.5 degradation routes to review rather than writing. **The billing-period half of A13 was D1, not D4** — those keys are informational and auto-write, so a wrong period reached Dynamics unseen. **Fixed 2026-09-08** (`commercial-narrative-v18`), closing the D1 exposure; `property_surrey.service_address` was **accepted** the same day (A13c). The three `vendor_name` rows were **fixed in code 2026-09-09** (`commercial-narrative-v19`, A13d/A13e/A13f), leaving this one D4 row |
| A14 | `property_white_rock.total_invoice_amount_extract` sweeps in a small arrears line on 5 of 12 reads. The twins then disagree and the notice routes `REVIEW_B4_CRITICAL_FIELD`, so a human sees it and nothing wrong is written — D4, not D1. The generate twin is correct 12/12 |
| A15 | *(fixed 2026-09-09)* `bug_260629_0012` — a below-threshold `service_address` blocked the Bill-To fallback, so the doc reviewed on 1 read in 103. The value written was the correct one on every read; only the routing differed. Same shape as B1 and A12: review churn, not data integrity |
| C1, C2, C3 | Corpus and tooling health; no runtime effect |

**Count:** D1 = 7, D2 = 3, D3 = 1 (**11 Dataverse-affecting**) and D4 = 6 (**impact today**).
One caveat on the arithmetic: section `B6` covers three variants and is tagged `D2` because two
of them are, but its third variant (`business_license` routing) is `D4` — so the honest split is
11 Dataverse concerns and 7 non-Dataverse ones across 17 sections.

**Sequencing:** with Dataverse unused, D1/D2/D3 are all deferred (P2/P3 above). D4 is the work
that pays off now (P1). Within D1, A3 is the cheapest fix — the municipal sectioned-GST rescue
is already arithmetic-guarded, which is what makes it safe to widen to commercial. Within D2,
all four are one normalisation step.

---

## Severity A — will write wrong data unreviewed once Phase 4 ships

These produce an incorrect value **and** route happy, so no human would see them. Latent today
(no Dataverse write exists); fix before the Phase 4 write goes live.

### A1. `invoice_date` silently defaults to today when both date twins fail

**`D1` silently wrong → Invoice Date**

| | |
|---|---|
| Documents | `bug_260528_0016` (**5/10**), `bug_260624_0015` (**1/10**) |
| Observed | `bug_260528_0016`: printed `2026-05-25` on 5 runs, `2026-08-17` (today) on 5. `bug_260624_0015`: printed `2026-06-17` on 9, today on 1 |
| Why it escapes | `invoice_date` is not a critical field, so a defaulted value does not trigger review. `build_write_values` substitutes today when the resolution is unreliable |
| Impact | Once Phase 4 writes to Dataverse, the invoice is filed under the wrong date; on `bug_260528_0016` that is a coin flip. Today the value stops at the HTTP response and the diagnostics blob |
| Related | Same family as the `payment_due_date` +30 default fixed in `9c8b283`, and the `NO_GENERATE_RESCUE` work for `invoice_date` |
| Note | The corroboration rescue (`date_corroborated_in_text`) already exists for exactly this shape but only fires when the generate twin produced a value. Here **both** twins fail together |

### A2. `number_of_days` returns two different counts for the same bill

**`D1` silently wrong → Number of Days**

| | |
|---|---|
| Document | `bug_260624_0015` |
| Observed | `13` on **6/10** runs, `28` on **4/10** |
| Impact | Feeds the tenant utility-sharing calculation, so a wrong count misallocates real money. Whichever value is correct, the extraction is not reliable |
| Next step | Read the PDF and establish the correct count first — that decides whether this is an extract bug, a generate bug, or an ambiguous document |

### A3. `gst_amount` reads one section's GST on a sectioned **commercial** invoice

**`D1` silently wrong → GST Amount + Amount Excluding GST**

| | |
|---|---|
| Document | `recommend_260120_0036` (CentiMark) |
| Observed | `27.50` stable 10/10; the correct value is **32.62** (`27.50 + 2.97 + 2.15`, = 5% of the 652.30 subtotal, and `652.30 + 32.62` = the printed total 684.92) |
| Cause | The sectioned-GST summation rescue in `gates.evaluate` is gated to `bucket == MUNICIPAL`. This invoice prints a GST row per line item with no TAX SUMMARY recap, and is commercial |
| Impact | `gst_amount` and the derived `amount_excluding_gst` are both wrong. Currently routes to review for an unrelated reason, so it is not silently written *today* |
| Fix sketch | Extend the existing municipal rescue to the commercial bucket. It is already arithmetic-guarded (`GST ≈ 5% of total − gst − pst`), which is what makes it safe to widen |
| Related | `c24dad1`, `478b804` — the municipal version of this same bug |

### A4. `billing_period_start_date` is 20 years off

**`D1` silently wrong → Billing Period Start Date**

| | |
|---|---|
| Document | `bug_260605_0017` |
| Observed | `2006-01-26`, stable **10/10** |
| Impact | Feeds the utility-sharing calculation. Stable, so it would pass any replicate-based stability check — it was found only by reading the value |
| Note | A good example of why the audit did not auto-assert "stable" values |

### A5. `account_number` returns the PO number

**`D1` silently wrong → Account Number**

| | |
|---|---|
| Document | `260629_0024` |
| Observed | `11022266`, stable 10/10 — which is the PO/job number printed as `# 11022266`, not an account number. The invoice prints no customer account number |
| Impact | Low today (commercial bills do not require `account_number`), but it is a wrong value in a real column |

---

## Severity B — unstable extraction, visible as review churn

Wrong or missing values that *do* route to review, or that flip without changing the outcome.

### B1. `service_address` intermittently returns nothing, flipping routing — **FIXED 2026-08-18**

**`D4` not Dataverse — routed to review, a human saw it**

Re-scoring all 1,633 cached `service_address` resolutions (24 docs × 12 analyzer versions)
against the live decision path found **41 failures in two distinct shapes**, not one:

| Shape | n | Mechanism |
|---|---|---|
| generate-only, below bar | 28 | `service_address_extract` returns a **confident null** (0.782/0.837 — a confidence in the *absence*, not a shaky read) because the address sits outside every labelled block the prompt lists. The generate twin reads it correctly but reports the weak label evidence as 0.41–0.87, straddling the 0.73 bar, so the identical correct address passed or failed by coin flip — and the losing runs still **wrote** it, then sent the doc to review |
| both twins null | 13 | Nothing was read at all. Correct review, not a defect — except on `business_license`, see B6c |

Five documents, not the two originally logged: `260629_0010`, `bug_260605_0017`,
`bug_260615_0006`, `business_license`, `recommend_241105_1061`. The "1 run in 3 on
`recommend_241105_1061`" figure in the old P1 row was already stale when written: that
document's mechanism is different (CU assigns the *same* extraction — identical spans and
confidences — to either `service_address` or `bill_to_address`, never both), and the Bill To
fallback shipped 2026-07-24 had already absorbed it, 10/10 happy.

**Fix:** `gates.evaluate` accepts a below-bar, generate-only address when CU's own spans for
that value quote text naming the same place (`field_policy.address_corroborated_by_span`).
Stronger than the `date_corroborated_in_text` precedent, which can only ask whether the value
appears *somewhere* on the page — a span says where it was read, so a letterhead address or an
invented one cannot be laundered by a page-wide hit. Narrow: only when the extract found
nothing, never for Noble's own office, and a disagreeing twin pair keeps its review. All 28
cases were span-grounded; the 13 empty ones are untouched.

### B2. FortisBC letterhead vendor coin flip

**`D2` inconsistent → Vendor Name**

| | |
|---|---|
| Documents | `fortisbc`, `bug_260528_0016` |
| Observed | `vendor_name_extract` alternates between `FortisBC Energy Inc.` and `FortisBC - Natural gas` (roughly 7/12 and 9/10 respectively) |
| Impact | Cosmetic in the resolved value, but it is a real disagreement between two readings of the same letterhead |

### B3. `number_of_days` invents a 1-day count

**`D1` silently wrong → Number of Days**

| | |
|---|---|
| Document | `bug_260605_0017` (**1/10**) |
| Observed | `""` on 9 runs, `1` on 1 |
| Note | `NO_GENERATE_RESCUE` + the reliability gate suppress this most of the time but not always. The residual rate is low |

### B4. `total_invoice_amount_generate` disagrees on one run in ten

**`D4` not Dataverse — raw twin, resolved value stays correct**

| | |
|---|---|
| Document | `abbotsford_water` |
| Observed | `1855.11` x9, `1952.75` x1. The resolved `total_invoice_amount` stays correct |

### B5. `invoice_number` swallows the adjacent date — **FIXED 2026-08-18**

**`D1` silently wrong → Invoice Number**

| | |
|---|---|
| Document | `recommend_260120_0036` |
| Observed | `8001214179` x13, `8001214179 - 01/14/2026` x1 at n=14. The invoice prints `Invoice # / Date: 8001214179 - 01/14/2026` on one line |
| Why it was fixed now | Fixing **A3** removed the review that had been containing it. That document routed `REVIEW_B4_CRITICAL_FIELD` on every run *because of* the wrong GST; with the GST resolved it routes happy 13/14, so the malformed identifier would have auto-written to Dynamics roughly 1 invoice in 14 |
| Fix | `field_policy.strip_trailing_date` trims a trailing date off the written identifier. It requires the date's own punctuation, so `26-158696`, `2353671-0602-0` and `7300-0002803156` are untouched, and a value that is *entirely* a date keeps its text rather than emptying. One value changed across 1,749 cached decisions |

### B6. Cosmetic instability that defeats exact-string assertions

**`D2` inconsistent → Vendor Name / Service Address; the routing flip is `D4`**

| | |
|---|---|
| Documents | `west_van_water` (`DISTRICT OF WEST VANCOUVER` vs `District of West Vancouver`) — B6a, still open |
| Note | Not wrong data. B6b (address line-breaking on `diag_260106_0008`, `bug_260605_0017`, `warranty_260120_0062`) and B6c (`business_license` routing) were both fixed on 2026-08-18 — see *Resolved*. B6a remains: nothing normalises vendor-name case |

---

## Severity C — process and tooling

### B7. Vendor legal suffix intermittently dropped

**`D2` inconsistent → Vendor Name**

| | |
|---|---|
| Document | `recommend_240124_0001` |
| Observed | `CAMBIE ROOFING CONTRACTORS LTD.` on 12 of 13 observations, `CAMBIE ROOFING CONTRACTORS` on 1 |
| Note | Stable across the first n=10 audit and surfaced only on the forced re-roll — the signature of a ~5% flip, and a reminder that n=10 certifies roughly 95%-stable assertions, not 99% ones |

### C1. The golden corpus only passes because the cache freezes one roll

**`D4` not Dataverse — corpus health**

The pre-commit hook runs `regress.py` **without** `--force`, so it re-scores cached CU results.
Two independent 3-replicate rolls produced **disjoint** failure sets (5 rows, then 7, zero
overlap) — roughly 1% of assertions failed per run. The n=10 audit on 2026-08-17 found the real
figure: **573 of 580 assertions stable, 7 not**, and those 7 are now unasserted or logged above.

Open question: nothing schedules a periodic `--force` run, so the next crop of marginal
assertions will again surface only when someone edits the analyzer and busts the cache.
Consider a periodic forced re-roll, or raising the pre-commit replicate count.

### C2. 73 stable-but-unverified keys are unasserted — **DONE 2026-08-18**

**`D4` not Dataverse — corpus health**

Resolved to 20 verified additions; see *Resolved*. Two traps found while doing it, both worth
remembering:

**A defaulted date looks perfectly stable.** Seven `payment_due_date` candidates read
`2026-09-17` and one `invoice_date` read `2026-08-18` — today+30 and today. They are unanimous
across every replicate and would have turned the corpus red the next morning. Any candidate
whose field appears in `defaultedFields` must be excluded, whatever its stability.

**Verifying is still how defects surface.** `260629_0024.invoice_date` is stable 31/31 at
`2025-08-06` — which is `8/6/25` from a `Date Order Taken and Completed` block whose other value
is `4/27/26`. The document prints no issue date at all, so the pipeline is filing the invoice
under the date the order was *taken*, eight months before completion, and the slashed form is
ambiguous besides (Aug 6 or Jun 8). It is not defaulting and not flagged, because CU returns it
as a resolved `valueDate` which bypasses the slashed-date rejection that used to catch it.
**Left unasserted by decision (user, 2026-08-18); the underlying wrong write is open.**

### C3. `scripts/scorecard.py` `invoice_description_gate` is stale

**`D4` not Dataverse — tooling**

Enforces a 15-**word** warning that was superseded by the 44-**character** rule in `b20104f` /
`9a056b8`. Emitted into the scorecard as `invoice_description.length_gate`, so it reports on a
rule that no longer exists. Nothing checks 44 characters programmatically anywhere.

### C4. Narrative character limits are prompt-only

**`D3` the row write itself fails → Diagnosis / Recommendation / Warranty**

`diagnosis_solution` / `recommendation` / their `_zh_hant` twins and `warranty` are enforced in
the analyzer prompt only — by decision, no code truncates.

**Current headroom (measured across every cached replicate, n=14):** longest
`diagnosis_solution` 399, `recommendation` 393, `warranty` **387**, `diagnosis_solution_zh_hant`
245, `recommendation_zh_hant` 174. Nothing exceeds its limit today, and the 403-character
`warranty` in the original report did not reproduce. `warranty` at 387 against 400 is the tight
one — about one wordy clause of margin.

**2026-08-18: setting all five limits to 500 was attempted, MEASURED HARMFUL, and reverted.**
Editing only the five narrative `description` strings destabilised two fields that were not
touched, on the first live 3-replicate roll of the new analyzer:

| Assertion | 14 previous analyzer versions | new analyzer, n=3 |
|---|---|---|
| `delta_water.vendor_name` | `City of Delta` **68/68** | `Delta` 2, `City of Delta` 1 |
| `recommend_241105_1061.account_number` | `null` **14/14** | `#11020375` (the PO number) on 1 |

Neither had ever flipped. This is the cross-field coupling already documented for this analyzer —
a prompt edit reaching fields it has nothing to do with — and it is why code-side fixes are
preferred here. The measurement is n=3, enough to show something moved but not to size it; the
change was dropped rather than measured further because the limit is a nice-to-have (user,
2026-08-18) and a prompt instruction cannot *guarantee* a length anyway.

**Still open, and the real mitigation is not in this repo:** nothing truncates in code, so the
limit is an instruction, not a bound. What actually prevents a lost invoice is the Dataverse
column size — size the three columns comfortably above the longest plausible value and an
overshoot is stored rather than rejected. A code-side cap would also give a hard guarantee if
the appetite for truncation ever changes.

### C5. The relaxed `11`/`33` PO prefix has no real-document coverage

**No Dataverse impact — a test-coverage gap, not a wrong value.**

The `po_or_job_number` prefix rule was relaxed from `110`/`330` to `11`/`33` (third digit no
longer constrained; length still exactly 8). Nothing in the repo exercises the newly-permitted
range: every PO in `tests/pre-commit-test/*.expected.json`, in the test literals, and in the
`out/regress-cache/` OCR text begins `110` or `330`, or is blank — `distinct 3-digit prefixes:
['110', '330']`, 11 non-empty values.

So the corpus and the suite prove **no regression** (the new rule is a strict superset, so a
relaxation cannot invalidate anything previously valid), and the unit tests prove the *code*
accepts the widened range — but they do so with **synthetic values** (`11524580`, `33512345`).
Nothing proves a real new-prefix PO is extracted end-to-end, and the CU prompt half in
particular is unverified against a real document.

This is inherent to relaxing a constraint before the newly-permitted values exist, not an
oversight. **Close it when the first invoice with a third-digit-≠0 PO arrives** by adding it as a
permanent corpus anchor:

```powershell
.\.venv\Scripts\python.exe scripts\regress.py --add ".\samples\<new-prefix>.pdf" --load-local-settings
```

Watch for one specific failure if it does not extract: both worked examples left in the two PO
prompts (`11024580`, `33001022`) still have a third digit of `0`, so CU may generalise from the
examples rather than the stated rule. The fix would be to add one third-digit-≠0 example — held
back deliberately to keep the prompt diff minimal (see C4 on cross-field coupling).

### A6. `invoice_date` silently defaults to today on an "Order Date" page — **FIXED 2026-08-27**

**D1 — wrote a silently wrong value.** Same failure mode as A1, on a document shape the A1
fix did not reach.

`bug_260827` (Trail Appliances) prints its issue date as **`Order Date: 08/27/2026`** and no
other issue-date label. Measured on the live analyzer, 2026-08-27:

| Run | `invoice_date_extract` | `invoice_date_generate` | Outcome |
|---|---|---|---|
| direct re-analysis | `2026-08-27` (0.417) | `2026-08-27` (0.358) | passed by twin **agreement**, not defaulted |
| local end-to-end | **`null`** (—) | `2026-08-27` (0.316) | resolution **failed** → **defaulted to today** |

Both twins sit far below the 0.73 bar, so the field passes only when they happen to agree —
a coin flip between the printed date and `date.today()`.

**`field_policy.find_invoice_date_in_text` cannot rescue it, for two independent reasons:**
`_INVOICE_DATE_LABEL` accepts only `invoice|billing|bill|statement|notice|issue` + "date", so
**"Order Date" is not a recognised label**; and the printed form is **slashed** (`08/27/2026`),
which the pattern deliberately refuses as ambiguous and print-timestamp-shaped. Adding `order`
to the label list alone therefore fixes nothing — the slashed-date exclusion would still
decline. Any fix has to decide whether a slashed date next to an *unambiguous* label is
trustworthy, which is exactly the judgement that exclusion was added to avoid.

**Why it was nearly invisible:** the document was processed on its own order date, so the
defaulted value and the printed value were the same string. It is only detectable via
`defaultedFields`, never by comparing the written date to the page. Every such document
processed on any later day writes the processing date instead.

**Corpus caution (resolved by the fix):** `regress.py --add` proposed `invoice_date:
"2026-08-27"` as stable because all three replicates produced that string — some by reading it,
some by defaulting to the same day. That was a **latent false-green assertion** that would have
flipped red from 2026-08-28. The fix makes it genuinely stable (`source=printed_label`), so the
assertion is now safe to keep. See troubleshooting.md → "A cached-green corpus hid three
coin-flip assertions for weeks".

**Fix.** `find_invoice_date_in_text` gained a **second tier** for `Order Date`, consulted only
when no `invoice|billing|bill|statement|notice|issue` label matched anywhere — a separate tier,
not another alternative, so it can never contribute a second candidate that makes tier one
decline on an invoice printing both. And `_normalize_unambiguous_slashed` accepts a slashed date
**only** where the day/month order is forced by the values (exactly one component > 12) and the
year is four digits, which keeps the `1/13/26 10:12AM` print-timestamp form and every genuinely
ambiguous form (`03/04/2026`) refused. `_normalize_date` is untouched, so nothing else in the
pipeline changes.

**Measured blast radius:** HEAD vs working tree over **3,018 stored CU responses** (regression
cache + diagnostics + the B2 audit), on 856 of which the fallback returns a date — **5 answers
changed, all 5 the Trail document**, every one `None → 2026-08-27`. On the reproduced failure
condition (extract null, generate 0.316) HEAD gives `defaulted=YES` and the working tree gives
`source=printed_label, defaulted=NO`.

### A7. `total_invoice_amount_extract` intermittently returns no value at all — CU-side

**No Dataverse impact — the written value is correct on every observed run.** Logged because the
rate is now measured and the assertion that used to cover it has been dropped.

On `recommend_240124_0001` (Cambie Roofing, totals printed only on page 2) the **extract** twin
sometimes comes back with the field object present but **no `valueNumber` and no `spans`**:

```
NULL  "total_invoice_amount_extract": { "type": …, "confidence": 0.975 }
GOOD  "total_invoice_amount_extract": { …, "valueNumber": 903,
        "spans": [{"offset": 3503, "length": 7}], "source": "D(2,…)" }   <- page 2
```

Not an OCR failure: the markdown is **byte-identical** between null and good runs (`md_len` 3852,
`903` present, 7 page markers), every other field extracts normally on the same run, and the
generate twin returns 903. The `confidence: 0.975` on a null is the placeholder every absent field
carries that run, not a field confidence.

**Measured with a concurrent control, 2026-08-27** (n = 12 per arm, one scratch analyzer, same
session, both arms' pushes verified by fetching the enum back):

| arm | definition | null rate |
|---|---|---|
| 0 | HEAD — **no** `propertytax` (= the definition in production) | **2/12** |
| 1 | working tree — **with** `propertytax` | **3/12** |

Indistinguishable, so the `propertytax` edit is **exonerated**; pooled rate **5/24 ≈ 21 %**.

**This is a CU regime shift, not a hidden coin flip.** The cache holds 62 clean reads of 63 across
8 analyzer versions — at 21 % that run has probability ~10⁻⁶, so the old regime genuinely existed
and the assertion was valid when written. Same shape as the municipal `vendor_name` step change of
2026-08-18/19 (troubleshooting.md → "Analyzer version is confounded with wall-clock time"), and it
surfaced only because changing the analyzer hash forced a fresh roll.

**Why nothing was fixed in code:** twin redundancy did its job — extract dropped out, generate held,
the resolution wrote 903. There is no defect on our side to repair.

**Assertion dropped** (user-approved): `recommend_240124_0001.fields.total_invoice_amount_extract`.
At 21 % it reddens ~50 % of 3-replicate runs, blocking commits through the pre-commit hook, while
protecting nothing that `writeValues.total_invoice_amount` (63/63), `amount_excluding_gst`, and
`total_invoice_amount_generate` (0 null in 63) do not already cover.

**Latent risk to watch:** `total_invoice_amount` is base-critical. Both twins failing on one run
would route the invoice to review. Never observed in 63 reads, and invisible on this document
(already `REVIEW_B4_CRITICAL_FIELD` for its missing PO) — but on a happy-path document it would
flip routing.

> ### ⚠ The latent risk materialised the same day, in production
>
> The v11 post-deploy smoke test on **`property_burnaby`** — a *different* document, and one the
> corpus expects to reach `HAPPY_PATH_CANDIDATE` — hit exactly this:
>
> ```
> total_invoice_amount_extract  : null      (confidence 0.975, the absent-field placeholder)
> total_invoice_amount_generate : 1858.69   at confidence 0.318
> -> resolution FAILED -> B4 -> REVIEW_B4_CRITICAL_FIELD
> ```
>
> So A7 is **not specific to `recommend_240124_0001`**, and it **does** flip routing on a document
> that otherwise auto-writes. It degrades safely — review, never a wrong auto-write — but the
> corpus asserts `HAPPY_PATH_CANDIDATE` for this document, so expect intermittent red here too.
>
> It also produced a **different total**: 1858.69 instead of the asserted 2428.69. That is the
> grant-column effect below, not a second defect — with `extract` gone, the surviving generate
> twin answered at 0.318, and a low-confidence answer on an ambiguous page picked a different
> column.

### A7b. Property tax notices print several equally-labelled "Amount due" figures

**Decision recorded, no code written.** A BC property tax notice prints one amount per home-owner
grant tier, all under an `Amount due` label:

```
Grant amount   A No Grant  |  B Grant: 570.00  |  C Grant: 845.00
Amount due     $2,428.69   |  $1,858.69        |  $1,583.69
```

**3 of the 6 sampled notices are ambiguous this way** (`property_burnaby` 3 figures,
`property_vancouver` and `property_Vancouver2` 2 each). The other three print grants of `0.00`
(`property_abbotsford`, `property_surrey`) or a single figure (`property_richmond`).

**Rule (user, 2026-08-27): write the NO-GRANT amount, column A.** The grant requires the property
to be the owner's principal residence, so managed rentals are not eligible. All six sidecars
already assert column A, so no corpus change was needed.

**Why no code repair was written.** Simulated a candidate repair — "on a `propertytax` bill whose
page shows grant tiers, if the written total is not the largest amount-due figure, correct it" —
across **3,094 cached reads**:

| | |
|---|---|
| propertytax reads already writing column A | **18 / 18** |
| reads the repair would fire on | **0 / 18** |
| false positives on non-propertytax reads | **0 / 3076** |

CU already picks column A whenever it answers normally. A repair that never fires is speculative
code for a scenario the evidence says does not occur. **Revisit only if a column-B write is
observed on a run where `total_invoice_amount_extract` returned a value** — the single observed
case was an A7 dropout, above, not a considered column choice.

### A8. `invoice_date` is written as TODAY on documents that print no issue date — **CLOSED, accepted behaviour**

**Not a defect — recorded because the measurement is useful.** The residual half of A1: that fix
reads the date off a printed *label*, so it can do nothing for a page that prints no issue date at
all. Those documents fall back to today, which is the agreed behaviour.

Measured over 3,094 cached reads, documents that wrote `date.today()` on **every** read:

| document | reads | what the page prints |
|---|---|---|
| `business_license` | **121 / 121** | due date + penalty dates only |
| `property_abbotsford` | 3 / 3 | `DUE DATE`, `PENALTY DATE` — no issue date |
| `property_richmond` | 3 / 3 | due date only |
| `property_surrey` | 3 / 3 | due date only |

Four more defaulted occasionally (1–15 of 60–161 reads) through twin dropout. `business_license`
had been doing this since long before the property tax notices existed; it was invisible because
`invoice_date` is not critical, so it auto-wrote, and today's date looks plausible whenever the run
happens to fall on a believable day.

> ### ⚠ CLOSED — NOT A DEFECT (user, 2026-08-27)
>
> **The today-substitution is the intended behaviour, for both buckets.** Two changes were
> written and both reverted before shipping: first blanking `invoice_date` everywhere
> (`commercial-narrative-v12`, committed as 11a530c), then blanking it on the municipal bucket
> only while keeping today on commercial (`v13`, never committed). The user's decision on seeing both: *"when invoice date is
> blank, it should always be today's date for both commercial bill type and municipal bill
> type."*
>
> The measurement below stands and is worth keeping — `business_license` really does write
> today's date on 121 of 121 cached reads, and three of the six property tax notices print no
> issue date at all — but that is now a **known and accepted** property of the pipeline, not a
> defect. `defaultedFields` is the signal: it names `invoice_date` on every such run, so a
> reviewer can always tell a substituted date from a read one.
>
> **Do not "fix" this again without a fresh decision.** Neither reverted version ever reached
> production. `v12` is in git history (11a530c, reverted); `v13` was never committed. The version
> numbers are not reused — the revert ships as `v14` so one `POLICY_VERSION` never means two
> different behaviours in the ledger.

**Measured blast radius** (HEAD vs working tree over all 3,094 cached reads): **8 documents, 151
reads, `invoice_date` the ONLY field that changed, routing unchanged on every single read.**

### A9. CU intermittently returns a *set* of fields with no value — one cause, three symptoms

**Not a defect in our code.** Three separate instabilities chased this session turned out to be
the same CU behaviour, which is worth recording once rather than three times.

CU sometimes returns a field object carrying only `type` and `confidence` — **no value, no
spans** — and the affected fields vary run to run. On `260629_0010` the empty set grew and shrank
across three consecutive reads of the same bytes:

| read | fields with no value | `bill_type` | empty-field confidences |
|---|---|---|---|
| r0 | 19 / 39 | **NULL** | `{0.876: 6, 0.975: 2, 0.837: 11}` |
| r1 | 17 / 39 | **NULL** | `{0.876: 6, 0.975: 2, 0.837: 9}` |
| r2 | 14 / 39 | ok | `{0.876: 6, 0.975: 2, 0.837: 6}` |

The empty fields share a handful of placeholder confidences (they are **not** per-field scores),
and the `0.837` group is the one that varies. `bill_type` carries 0.837, so it was swept into a
widening set CU declined to populate. Empty fields are normal — median 15 of 39 across 108 reads,
most legitimately absent — so the signal is the *variance*, not the count.

**The three symptoms, all measured with concurrent controls:**

| symptom | rate | consequence |
|---|---|---|
| `total_invoice_amount_extract` null (**A7**) | 5/24 pooled | generate twin covers it; routing flips only if generate is also sub-bar |
| `bchydro` both `invoice_date` twins null | 1/12 | today substituted (A8); assertion dropped |
| `260629_0010.bill_type` null | 2 / 3,202 (0.1%) | bucket falls back to commercial — **correct on every read** |

**Why `bill_type` has no second read.** It is the **only classify field in the schema without a
generate twin** — `sub_bill_type` has `sub_bill_type_generate`, `is_handwritten` has
`is_handwritten_generate`, `bill_type` has nothing. The field that selects the policy bucket is the
one field with no second read to fall back on. `resolve_bucket(None)` → `commercial` is the
fail-safe that contains it, and it held on all three reads above.

**The latent risk, never observed:** a null on a genuinely *municipal* bill would put it in the
commercial bucket — stricter critical fields (so almost certainly review, the safe direction) but
also the narrative fields would **not** be blanked, writing diagnosis/recommendation text onto a
utility bill. Zero occurrences in 3,202 reads; every observed null was on a commercial document.

**FIXED 2026-08-28 (the write value), code-side.** `field_policy.default_bill_type` writes
`commercial` whenever CU returns no label, matching `resolve_bucket`'s fail-safe exactly, and
`gates.evaluate` records it in `defaultedFields` with an advisory. Requirement (user, 2026-08-28):
*if CU cannot classify `bill_type` and returns none or empty string, the default is `commercial`.*
Measured over 3,232 cached reads replayed against HEAD as a separate process: **1 write value
changes, 0 routing decisions change, 0 other fields move.** It cannot change routing because the
bucket is derived from the parsed response and never re-read from the write values — the default
records the decision rather than making one.

A narrower first cut, recovering only when `sub_bill_type_generate` named a commercial sub-type,
was **rejected as under-specified**: it left the label null when that twin was also empty (1 of the
3 observed dropouts), when it said `other`, or when it named a municipal sub-type. The accepted
trade of the simpler rule is that a *municipal* bill whose label drops out is written `commercial`
— which is what the pipeline actually applied — and the commercial bucket then makes
`po_or_job_number` critical, so that bill routes to review rather than auto-writing.

**A `bill_type_generate` twin was REJECTED as the fix.** Modelled on `sub_bill_type_generate` it is
validation-only ("never supplies the label itself") and so cannot help when the label is *absent*;
made authoritative it must be able to answer `municipal`, which creates the first path into the
relaxed bucket. Adding a 40th field also carries the measured cross-field coupling risk.

**Still open: escalation.** A dropout that recovers nothing keeps the null label and is contained
only by the commercial fail-safe. Making `bill_type` critical was measured and **rejected for now**:
519 of 3,231 populated reads (16%, min confidence 0.346) sit below the 0.73 bar, so an ordinary
critical-field check would route ~16% of ALL documents to review. It would need a presence-only
critical variant, measured separately.

### A10. `payment_due_date` ignores a printed `NET<n>` payment term — **OPEN**

**D2 — writes an approximate value where an exact one is derivable.** When a document prints no
calendar due date, `payment_due_date` falls back to **today + 30** (`build_write_values`). That is
today + 30, *not* `invoice_date` + 30 — the two coincide only when a document happens to be
processed on its own issue date.

`bug_260827` (Trail Appliances) prints `Order Date: 08/27/2026` and `Payment Terms: NET30`, and no
due date. The business-correct answer is derivable and exact — 2026-09-26 — but the pipeline writes
the run date + 30, which drifts one day further from correct for every day the document sits
unprocessed. Same class as the `payment_due_date` corroboration fix (9c8b283), which found the +30
default writing ~8-day-late dates unseen.

**Rate:** unmeasured across the corpus; 1 of 36 documents prints a `NET<n>` term with no due date.
Measure before building — the fix is only worth it if the pattern is common in production.

**Fix shape:** read `Payment Terms: NET<n>` off the OCR text and derive
`payment_due_date = invoice_date + n` when no due date is printed and `invoice_date` itself was not
defaulted. Code-side, no analyzer change; the guard against compounding two defaults matters.

**How it surfaced:** the `bug_260827` sidecar asserted the defaulted 2026-09-26 with a note claiming
it was `invoice_date + 30` and therefore stable. That claim was wrong about the code, the assertion
tracked the run date, and the corpus went red on 2026-08-28. Assertion dropped; see that sidecar.

### A11. `total_invoice_amount` has no rule for home-owner-grant columns — **FIXED 2026-09-09** (`commercial-narrative-v20`)

**D3 — costs a false review, does not corrupt a value.** Every BC property tax notice prints the
same three-column grant layout: **no grant / regular grant / senior-additional grant**, each with
its own total. The business answer is always **column A (no grant)** — the user's decision when the
six notices were added. The analyzer prompt never says so. Both `total_invoice_amount` twins cover
carried-forward balances, deposits, GST reconciliation and handwritten dollars/cents sub-columns;
every "column" mention is about the sub-column case. **Grants are not mentioned at all.**

On five of the six notices this is harmless — they print no `Amount due` row (the extract twin
anchors on `TOTAL TAXES PAYABLE` / `TOTAL TAXES DUE`), or they print one whose three columns are
the same number (`property_surrey`, extract confidence 0.882 flat).

`property_burnaby` is the exception and shows the cost:

| document | prints an `Amount due` row | figures in it | extract-twin dropouts |
|---|---|---|---|
| abbotsford / north_van / richmond / vancouver | no | — | 0/6 each |
| surrey | yes | 3, all identical | 0/6 |
| **burnaby** | **yes** | **3, all different** | **3/6** |

`Amount due` is the strongest cue for the field, and on burnaby it points at
`$2,428.69 / $1,858.69 / $1,583.69`. The *extract* twin must emit a span naming one figure; with
three equally-labelled candidates and no rule to choose, it anchors weakly (0.415–0.609, the lowest
of the six) or returns the A9 empty shape outright.

**Why that matters more than it looks.** None of these six notices reliably clears the 0.73 bar on
confidence alone — `property_richmond` passes at extract 0.415 / generate 0.341, carried purely by
the **agreement boost**. Losing the extract twin removes that boost, leaving a lone generate twin
that swings 0.316–0.746. One read in six lands below the bar and routes to review.

The written value is correct (2428.69) on **every** observed read, so this costs a false review,
never a wrong write. `property_burnaby.routingDecision` is no longer asserted for this reason.

**Fix shape:** name the grant columns in both `total_invoice_amount` twins — on a property tax
notice showing no-grant / regular-grant / senior columns, return the **no-grant** figure. Analyzer
prompt change, so it needs the full protocol: scratch analyzer, n ≥ 12 per arm with a concurrent
control, all six notices plus non-tax controls. Restore the burnaby `routingDecision` assertion if
the extract twin then anchors reliably. **Do not bundle with an unrelated prompt edit.**

#### Re-measured 2026-09-09, after 14 more notices were added — documentation only, no code

The user's decision on 2026-09-08 was **"for all `bill_sub_type = propertytax`, always pick A (no
grant)" — documented, not coded.** This section records what the corpus now shows.

The corpus went from **6 property tax notices to 20** on 2026-09-09. Every one of the 14 new notices
resolved `total_invoice_amount` to its **column A** figure, verified individually against the printed
page — including nine that print all three grant columns with three *different* numbers, which is the
`property_burnaby` shape this defect was raised on:

| notice | A (no grant) | B | C | stable? |
|---|---|---|---|---|
| `property_ubc` | **3,138.43** | 2,568.43 | 2,293.43 | 3/3 |
| `property_north_van_district` | **875.60** | 305.60 | 30.60 | 3/3 |
| `property_north_van_city` | **7,602.11** | 7,132.11 | 6,857.11 | 12/12 |
| `property_north_van_city2` | **7,192.89** | 6,622.89 | 6,347.89 | 3/3 |
| `property_delta` | **45,691.50** | 45,121.50 | 44,846.50 | 12/12 |
| `property_port_moody` | **3,046.79** | 2,476.79 | 2,201.79 | 3/3 |
| `property_new_westminster` | **2,233.58** | 1,663.58 | 1,388.58 | 3/3 |
| `property_port_coquitlam` | **2,990.13** | 2,420.13 | 2,145.13 | 3/3 |
| `property_coquitlam` | **2,110.90** | 1,540.90 | 1,265.90 | 3/3 |
| `property_white_rock` | **6,032.62** | 5,462.62 | 5,187.62 | see A14 |
| `property_pitt_meadows` / `property_west_vancouver` | all three columns equal (grant 0.00) | | | 3/3 |
| `property_ubc2` / `property_vancouver_advance` | no grant columns printed | | | 3/3 |

**State this precisely: the rule holds by coincidence, not by construction.** The prompt still says
nothing about grants, so nothing *makes* CU prefer column A — it simply has, on every read measured
so far. These notices auto-write (`HAPPY_PATH_CANDIDATE` on 19 of 20), so a read that picked column B
would be a **silent D1**, not a review. The 14 new notices raise confidence in the observation and
raise the exposure at the same time.

The `property_burnaby` extract-twin dropout this section documents (3/6) did **not** reproduce on any
of the 14 new notices: 0 dropouts in 60 reads.

#### Closed 2026-09-09 — both halves shipped in one analyzer version

A11 and A14 were **bundled deliberately** (user decision): both are edits to the same
`total_invoice_amount` prompt, so two separate versions could not have been attributed apart
anyway, and bundling cost one cache re-roll instead of two.

**What the prompt now says.** Both twins gained a property-tax paragraph: *(a)* on a notice with
grant columns, return the **no-grant** figure, read from the notice's own total row — and never
from a sentence about next year's estimated instalments, which is the trap `property_white_rock`
sets by printing *"would be $621.00 No Grant Available"*; *(b)* on a property tax notice, unpaid
**arrears are payable and count toward the total**, reversing the general carried-forward rule for
this document family only. Keying on the words *"No Grant"* rather than *"Column A"* was the
user's call and the corpus supports it: all 18 notices with grant columns print some form of
*No Grant*, while the column is **not** always labelled A — `property_ubc` prints `No Grant A`
reversed, and five others print a bare `NO GRANT`.

**Measured over the full 52-document re-roll — the three arrears notices moved, and nothing else
did:**

| notice | before | after | note |
|---|---|---|---|
| `property_delta` | 45,691.50 ×12 | **48,009.77 ×3** | arrears 2,318.27 now included |
| `property_north_van_city` | 7,602.11 ×12 | **9,245.77 ×3** | arrears 1,643.66 now included |
| `property_white_rock` | 6,158.28 ×5 / 6,032.62 ×7 | **6,158.28 ×3, stable** | the A14 coin flip is gone |
| the other 17 tax notices | — | **unchanged** | |

`property_white_rock` is the direct A14 close: the extract twin no longer flips, so the twins agree
and the notice stops routing to review on an ambiguous total.

**Caveat, stated rather than buried.** These totals are still only measured at **n = 3** on the new
definition, and the reads sit on one day. The A11 observation that CU picks column A remains an
*observation*: the prompt now asks for it explicitly, which is far stronger than before, but these
notices auto-write, so a column-B read would still be a silent bad write rather than a review.

### A14. `total_invoice_amount_extract` ignores a *small* arrears line — **FIXED 2026-09-09** (`commercial-narrative-v20`), found the same day

**D4 — routes to review, never writes the wrong figure.** Found while adding the 14 property tax
notices. Anchor: **`property_white_rock`**, measured at **n = 12**.

The analyzer prompt is explicit that an aged-arrears line is carried forward and must be excluded
(`total_invoice_amount` description, step 4). White Rock prints:

```
2026 TOTAL TAXES AND OTHER CHARGES        6,032.62      <- this bill's own charges (column A)
Unpaid Arrears Taxes                        125.66      <- carried forward
TOTAL OUTSTANDING TAXES DUE JULY 2, 2026  6,158.28
```

so **6,032.62** is correct. Measured over 12 replicates:

| twin | reads | verdict |
|---|---|---|
| `total_invoice_amount_generate` | **6,032.62 × 12** | correct every time |
| `total_invoice_amount_extract` | 6,032.62 × 7, **6,158.28 × 5** | flips |

On the 5 reads where the extract sweeps in the arrears the twins disagree, the resolution fails, and
the notice routes `REVIEW_B4_CRITICAL_FIELD` instead of auto-writing. **That is the safe direction**
— which is why this is D4 and not D1 — but `routingDecision` and `total_invoice_amount` are both
unstable, so `property_white_rock.expected.json` asserts neither. It asserts
`total_invoice_amount_generate = 6032.62`, which is stable and correct.

**Not a general failure of the carried-forward rule.** The two other arrears-bearing notices are
stable 12/12 on both twins:

| notice | own charges | arrears | total due | extract twin |
|---|---|---|---|---|
| `property_delta` | 45,691.50 | 2,318.27 | 48,009.77 | 45,691.50 × 12 |
| `property_north_van_city` | 7,602.11 | 1,643.66 | 9,245.77 | 7,602.11 × 12 |
| **`property_white_rock`** | **6,032.62** | **125.66** | **6,158.28** | **flips 7/5** |

The distinguishing feature is the **size** of the arrears: White Rock's 125.66 is 2 % of the total,
against 5 % and 18 % on the two stable notices. A hypothesis worth testing, not a conclusion — n = 1
document at each size.

**Fix shape:** unclear, and deliberately not attempted. A prompt edit would need the full protocol
(scratch analyzer, n ≥ 12 per arm, concurrent control) and would touch the same
`total_invoice_amount` description that A11 wants to change — **bundle them or neither**, since two
edits to one field's prompt cannot be attributed separately. A code-side option exists (prefer the
generate twin when the extract exceeds it by exactly a printed arrears figure) but it is speculative
against one document.

### A12. A bill addressed only to Noble's own office loses `service_address` and always reviews — **OPEN**

**D4 — costs a review on every occurrence, never a wrong write.** Found 2026-09-08 while adding the
two telecom corpus anchors. Measured **5/5 replicates** on `260825_telus`.

Some vendors bill Noble's head office directly, with no serviced property anywhere on the page. The
TELUS statement is the clean case: every address it prints — the letterhead customer block, the
remittance slip, the page-3 and page-4 charge blocks — is `155 13988 MAYCREST, RICHMOND BC V6V 3C3`,
which is Noble's own office. `is_noble_office_address` correctly matches all five spellings.

`gates.evaluate` then does the right thing and refuses to write it: the Bill To fallback is tried
first, that address is *also* the office, so `service_address` is discarded
(`resolutions.service_address.source = "noble_office_rejected"`) rather than sending the paying
party's address to Dynamics as the serviced property. But `service_address` is **base**-critical, so
the bill fails B4 in **either** bucket:

```
routingDecision   REVIEW_B4_CRITICAL_FIELD   5/5
reviewReasons     ("service_address needs attention",)   5/5
```

This is orthogonal to `bill_type`: the vendor-driven municipal rule shipped in
`commercial-narrative-v16` fixes the bucket for this bill (`bill_type` is now written `municipal`,
and the record and the reviewer both see it), but it cannot fix the routing, because the municipal
delta relaxes `po_or_job_number`/`gst_amount`, never a base field. The Rogers anchor
(`260901_rogers`) prints a real `SERVICE ADDRESS` block and reaches `HAPPY_PATH_CANDIDATE` on 5/5,
which is what isolates this to the address, not the bill type.

**Deliberately not fixed** (user decision, 2026-09-08): logged rather than fixed so the `bill_type`
change stayed surgical. `260825_telus.expected.json` asserts `service_address: null` and
`routingDecision: REVIEW_B4_CRITICAL_FIELD`, so fixing this later forces a conscious sidecar update
rather than a silent corpus flip.

**Fix shape — needs a product decision first, not a prompt edit.** The open question is what a bill
with no serviced property *should* write. Three candidates, in increasing risk:

1. Write Noble's office and accept it, for vendors that genuinely bill the office (telecom, SaaS).
   Cheapest, but it puts the paying party in the service column — exactly what
   `is_noble_office_address` exists to prevent, and it would need to be scoped to a vendor class.
2. Write `""` and let the bill auto-route, treating "no serviced property" as a valid answer rather
   than a missing one. Needs `service_address` to stop being base-critical for that class, which is
   a real weakening of the B4 net.
3. Leave it reviewing. Correct today, and cheap while telecom volume is two invoices a month.

Code-side either way — no analyzer change, so no prod analyzer push.

### A15. A *correct but unconfident* `service_address` suppresses the Bill-To fallback that would have rescued it — **FIXED 2026-09-09**

**D4 — cost a review, never a wrong write.** Found 2026-09-09 by diagnosing the corpus's last
failing key. Measured **1 read in 103**.

> **Fix (user-approved, with the constraint they set):** the Bill-To fallback now *also* fires when
> the `service_address` resolution **failed**, the Bill-To **passed**, and the two values **name the
> same place** (`field_policy.address_values_agree`, the same token-overlap test that resolves the
> Bill-To twins, so the two paths cannot disagree about "same address"). The user's condition —
> *"as long as the address is not the noble office address"* — is enforced by the pre-existing
> `not is_noble_office_address(bt_val)` guard, left untouched: an office address is still discarded,
> still reviewed, and never written. A **different** low-confidence address is still never
> overwritten and still reviews. Both are pinned by
> `test_a15_unconfident_duplicate_of_the_bill_to_is_promoted`.
>
> Verified on the triggering read itself (`bug_260629_0012` frozen r2): `0.399` → promoted to
> `0.897`, `REVIEW_B4_CRITICAL_FIELD` → `HAPPY_PATH_CANDIDATE`. Blast radius over 312 reads: **3
> reads on 2 documents**, both the same "no service block" shape. The second is
> `diag_260414_0028` r0, where the extract twin returned the same address at 0.413 — there the
> written value and the routing are **unchanged** (it reviews for `po_or_job_number`, which its
> sidecar asserts); the rescue only made r0 reach the address the same way r1/r2 already did.

`bug_260629_0012` (JMEC Electric) prints no SHIP TO / Service Address block at all, so
`service_address` is normally supplied by the Bill-To fallback at `gates.py:1114`. That fallback is
gated on the field being **empty or Noble's own office**:

```python
sa_val = resolutions[field_policy.SERVICE_ADDRESS_FINAL][0]
sa_is_office = field_policy.is_noble_office_address(sa_val)
if is_empty_value(sa_val) or sa_is_office:
```

The comment above it states the intent plainly — *"A present, non-office service_address is never
overwritten, even below threshold: that read found a real address and still routes to review."*
On this document that intent inverts: the extract twin's "real address" **is** the Bill To block, so
a low-confidence hit on it displaces a high-confidence copy of the identical string.

The three cached reads under `b5b984bc1a88`, same PDF, same definition:

| read | `service_address_extract` | `bill_to_address` | resolved source | conf | routing |
|---|---|---|---|---|---|
| r0 | *(no value)* @0.836 | `#307-7480 Gilbert Road…` @0.875 | `bill_to_fallback` | **0.875** | HAPPY_PATH_CANDIDATE |
| r1 | *(no value)* @0.836 | `#307-7480 Gilbert Road…` @0.897 | `bill_to_fallback` | **0.897** | HAPPY_PATH_CANDIDATE |
| r2 | `#307-7480 Gilbert Road…` **@0.399** | `#307-7480 Gilbert Road…` @0.897 | `extract` | **0.399** | REVIEW_B4_CRITICAL_FIELD |

`service_address` is the **only** failing field on r2 (`reviewReasons == ['service_address needs
attention']`); all four other criticals clear comfortably. The **written value is identical on all
three reads**, which is why `writeValues.service_address` is green and only `routingDecision` is red.

**This is not A9.** The first record of this key called it "an A9-shaped CU dropout, 7 fields
`None`". Re-measured against the raw cache, that is wrong in both magnitude and direction: 18 of the
41 raw fields carry no value on r0 and r1, and **17** on r2 — r2 is the read where CU returned *one
more* value, not fewer. (The original figure came from passing the whole CU result where
`find_child_content` expects a `contents` list, so it read an empty field set.)

**Rate, across every cached version of this PDF** — 19 analyzer definitions, 106 reads:
`service_address_extract` abstained on **105 of 106**, and the single read where it answered is the
single review. Excluding `29259b9fa9dd` (a 2026-07-25 definition of 32 fields, predating
`bill_to_address` entirely — its 3 reviews are the feature's *absence*, `source='none'`), the rate is
**1 review in 103 fallback-eligible reads**. That base rate spans definitions from 2026-07 to
2026-09, so the `commercial-narrative-v20` edit is not implicated; per the standing rule this is
still not a *concurrent* control, so treat it as a strong prior rather than an attribution.

**Fix shape (not applied — needs a decision).** The narrow correction is to let the fallback also
fire when `service_address` **failed its resolution** and `bill_to_address` **passed** and the two
values are the same address, so the confident copy wins over the unconfident one. That is strictly
narrower than "overwrite any below-threshold address": it changes nothing when the twins disagree
with the Bill To block, which is the case the current guard exists to protect. Cost of leaving it:
one unnecessary review per ~100 reads on invoices with no service block. Code-side — no analyzer
change, so no prod analyzer push.

### A13. Extraction quality dropped after the GPT-5.2 → GPT-5.5 upgrade — **PARTIALLY RESOLVED 2026-09-08**

> **Status.** Of the seven rows originally listed: the two `abbotsford_water` billing-period keys
> are **fixed** in code; `property_surrey.billing_period_start_date` is **withdrawn — it was never a
> defect**; `property_surrey.service_address` is **accepted** as the new truth (user, 2026-09-08);
> **three remain OPEN**. Three factual errors in the original write-up are corrected inline below.
> Details in *A13a*, *A13b* and *A13c* after the table.

**D1/D4 mixed — two of these corrupt a written value, four cost review churn.** Found 2026-09-08
by running the corpus live against the upgraded model. **The analyzer was not touched:**
`analyzers/` has no diff vs HEAD, and the pre- and post-upgrade draws share the cache folder
`db29961e7034` — that folder name *is* the content hash of the analyzer definition, so the
definition was provably constant across both roll dates. Only the model changed.

Rates are out of **9 replicates**, all on GPT-5.5 (a `--force` re-roll replaced the three
pre-upgrade draws). The pre-upgrade column was measured on the 3 draws taken 2026-08-27 and
survives only in a local cache backup; it is not reproducible from the live cache.

| Document | Field | Pre-upgrade (3) | GPT-5.5 (9) | Rate | Status |
|---|---|---|---|---|---|
| ~~`property_surrey`~~ | ~~`service_address`~~ | `2790 167 ST SURREY BC V3Z 0A9` 3/3 | `2790 167 ST` 9/9 | — | **ACCEPTED — A13c** |
| `warranty_260120_0062` | `account_number` | `Account No: 6042641001` 3/3 | `""` 8/9 | **8/9** | OPEN |
| `diag_260414_0028` | `vendor_name` | full legal name 3/3 | `Drips & Drains` 7/9 | **7/9** | **FIXED — A13d** |
| `property_vancouver` | `vendor_name` → routing | city name 3/3 | `Property Tax Office` 1/9 | 1/9 | **FIXED — A13e** |
| `abbotsford_water` | `billing_period_start_date` | `2026-03-01` 3/3 | `2026-01-31` 8/9 | **8/9** | **FIXED — A13a** |
| `abbotsford_water` | `billing_period_end_date` | `2026-04-30` 3/3 | `2026-04-01` 8/9 | **8/9** | **FIXED — A13a** |
| ~~`property_surrey`~~ | ~~`billing_period_start_date`~~ | `""` 3/3 | `2026-01-01` 9/9 | — | **WITHDRAWN — A13b** |

**Severity split.**

* `warranty_260120_0062.account_number` degrades a **base-critical** field — a lost account number.
  It routes to review rather than writing silently, so it costs churn, not correctness (**D4**).
  `property_surrey.service_address` was listed here on the same grounds; it has since been
  **accepted** rather than fixed — see A13c.
* `abbotsford_water`'s billing period was informational, never critical, and **auto-wrote** — a
  wrong period reached Dynamics unseen (**D1**). That exposure is now closed; see A13a.
* `property_vancouver` returns a wrong entity (`Property Tax Office`) on 1 read in 9; the other 8
  are correct. The `City of Vancouver` / `CITY OF VANCOUVER` casing difference in the same column
  is **not** a failure — `regress._values_equal` case-folds `vendor_name` (DV-4 owns the
  normalisation).

**The last row is provisionally green, and should not be closed yet.**
`warranty_260120_0062.account_number` read `""` on 8 of 9 reads under the old analyzer and reads
`Account No: 6042641001` on **3 of 3** under `commercial-narrative-v20`. Three clean reads against
an 8-in-9 defect is unlikely by chance (~0.1 %), so something genuinely changed — but **n = 3 is
thin evidence for declaring it fixed**, and it carries the same attribution confound as every other
v20 movement: the old reads were taken on earlier days, so "which definition" and "which day" are
the same variable. Re-roll it to n >= 12 beside a same-day control before closing this row.

The original entry, kept until then: `warranty_260120_0062.account_number` (8/9). It would need analyzer prompt
work, which per `CLAUDE.md` requires a scratch analyzer, n ≥ 12 per arm with a **concurrent** control,
and a prod analyzer push. Its sidecar is deliberately left asserting the pre-upgrade value (user
decision, 2026-09-08), so the corpus stays red on exactly that key until it is triaged. Unasserting
it to get green would remove coverage on a base-critical field for a model change that may yet be
tuned. **It is the only failing key in the corpus and the only thing blocking the pre-commit hook.**

#### A13d — `diag_260414_0028.vendor_name` — **FIXED 2026-09-09** (`commercial-narrative-v19`)

Both twins were right; they disagreed about **form**. The extract read the printed
`Drips & Drains Plumbing and Heating Ltd.` on 9 of 9, and the generate returned `Drips & Drains`,
exactly what its prompt asks for. They agree by containment, so `_prefer_vendor_generate` fell
through to its confidence tiebreak — and GPT-5.5 dropped the extract to 0.656 against a 0.764
generate, so the truncated form won 7 reads in 9. *"Trust the more confident twin"* was already the
rule, and here the shorter answer was the more confident one.

Fixed with `field_policy.commercial_printed_vendor_name`, applied in `gates.evaluate`: on a
**commercial** bill, when the generate twin is a strict leading prefix of the extract that dropped
**two or more** words, keep the printed extract. Measured over all 3,601 cached corpus reads: **30
changed, all commercial, none harmful** — `diag_260414_0028` (7, the fix), `bug_260605_0017` (22,
collapsing two casings into one full name) and `260629_0024` (1, `PRIORITY` alone being a fragment).
`diag_260414_0028` is now stable on the value its sidecar already asserted; **no sidecar was edited.**

**The two gates are both load-bearing, and both were found by measurement:**

* **Commercial-only.** Unscoped, the same rule rewrites 26 `fortisbc` reads (a municipal gas bill,
  where the short registry spelling is wanted) and on one of them writes the prose sentence
  `FortisBC Energy Inc. does business as FortisBC.`
* **Two-or-more words.** At a delta of one this **breaks `test_vendor_extract_generate_twin`**, which
  asserts on a *commercial* payload that `FortisBC Energy Inc.` @0.40 + `FortisBC` @0.90 writes
  `FortisBC`. That case is structurally identical to `diag` — commercial, leading prefix, extract
  below the bar, generate above — so no confidence-based narrowing can separate them. The corpus
  could not catch it, because real FortisBC bills bucket municipal. At a delta of 2 every one of the
  30 corrections survives and the suite stays green; at 3 the `WASTE MANAGEMENT` and `PRIORITY`
  repairs are lost.

**Three candidates were measured and rejected — do not re-try them:**

| candidate | reads changed | why it fails |
|---|---|---|
| leading subset → keep the long extract, **unscoped** | 62 | regresses `fortisbc` (26) and `Waste Management` (22) |
| leading subset → keep the **short** generate | 557 | `Shaw Cablesystems`→`Shaw`, `TELUS Communications Inc.`→`TELUS`, `SIMON SIK FAI KAN`→`SIMON` |
| both twins above threshold → use the extract | 31 | **never fixes diag** — its extract is 0.656, below the bar, on 7 of 9 reads |

*Highest confidence wins, ties → extract* changes **0** reads: `field_policy.py:890` already **is**
`return (g_conf or 0.0) >= (e_conf or 0.0)`, and there are no exact ties in the 774 reads that reach
the tiebreak.

#### A13e — `property_vancouver.vendor_name` → routing — **FIXED 2026-09-09** (`commercial-narrative-v19`)

The notice prints its issuer only as a logo (`![CITY OF VANCOUVER](figures/1.1)`), so the extract
twin took the plain text it *could* read — `Property Tax Office`, the return-address header at
offset 72 — at 0.666. The generate twin read the logo correctly at 0.778. The twins disagree and the
extract is below the bar, so `resolve_twin`'s `elif e_present` tail wrote the **wrong** one: the only
branch where a failing extract still beats a passing generate.

None of the three existing vendor repairs could reach it, each checked against the page:
`municipal_name_with_prefix` needs a bare place name, `municipal_payee_override` needs a printed
*payable to* line (this notice prints none), and `vendor_domain_tiebreak` finds no domain at all
because `_VENDOR_DOMAIN_RE` requires `http://`, `www.` or `@` while the page prints a bare
`vancouver.ca/property-tax`. **Widening that regex was measured and rejected** — it invents labels on
19 of 38 documents, including `jattempted` from prose, plus `translink`/`metrovancouver` on tax
notices and `shawbusiness` on the Rogers bill.

Fixed with `field_policy.propertytax_vendor_rescue`, gated on the **classification** instead, which
is independent of the vendor slip: on all 9 replicates *including the bad one*, `bill_type` held
`municipal` @0.882 and both `sub_bill_type` twins `propertytax` @0.88. When those clear their bars,
the bucket is municipal, **the vendor resolution has failed**, and the generate twin holds a
`City of X` the page actually prints — write it. Placed *before* `municipal_payee_override` so
`municipal_name_with_prefix` still runs after it.

Measured over all 3,601 cached corpus reads: **fires exactly once**, on the slipping read, taking it
from `REVIEW_B4_CRITICAL_FIELD` to `HAPPY_PATH_CANDIDATE`. It is a no-op on the other 19 property tax
notices because their vendor resolution passes — which is what keeps `property_burnaby` out of it.
`property_vancouver_advance`, added the same day, prints the *same* `Property Tax Office` header and
resolves correctly with no repair, so it is the control for this rule rather than a case of it.

#### A13e (part 2) — the same rescue missed the ABOVE-threshold regime — **FIXED 2026-09-09**

The A13e rescue above was gated on the vendor resolution having **failed**, which was true of
`property_vancouver` (wrong extract at 0.666, below the 0.73 bar). The `commercial-narrative-v20`
roll then produced the same wrong string on a *different* document with the bar cleared:

```
property_Vancouver2 r2   extract='Property Tax Office' @0.745   generate='City of Vancouver' @0.778
                         0.745 >= 0.73, so resolve_twin passes on `elif e_pass`  ->  AUTO-WRITES
```

That is the worse of the two regimes — a failed resolution routes to review, but this one wrote the
wrong vendor unreviewed (**D1**, not D4). The gate now fires in either regime, with the guard each
needs: on a **failed** resolution, no confidence test (there is no trustworthy value to protect);
on a **passing** one, only when the twins disagree **and** the generate twin is at least as
confident as the extract. That second guard exists because a synthetic test caught the rule
overwriting a confident correct extract — a 0.95 `City of Surrey` losing to a 0.778
`City of Vancouver` merely because the page prints the latter somewhere.

Twins that AGREE are never touched: agreement is the corroboration the whole twin design rests on,
and overriding it would discard the generate twin's casing normalisation.

**Measured over all 60 cached property-tax reads: 1 read changes — that one — and 0 reads outside
`propertytax` are even candidates.**

#### A13g — `folio_number` returns a value on documents that are not tax notices — **GUARDED IN CODE 2026-09-09**

Found while backfilling the new field into all 52 sidecars. The `folio_number` prompt says *"if it
is not a property tax notice, return null"*, and CU disagreed on two shapes:

| document | `sub_bill_type` | folio returned | verdict |
|---|---|---|---|
| `delta_water` | water | `163-061-00-0` ×2, `""` ×1 | **genuine** — the bill prints `FOLIO: 163-061-00-0` |
| `richmond_water` | water | `062-376-007` ×3 | genuine, printed |
| `vancouver_water` | water | `670-027-92-0000` ×3 | genuine, printed |
| `260521_0024` | other | **`COMPLEX`** ×3 | **invented** — not a folio at all |

Municipal water bills are property-linked and really do carry a folio, so three of these are
correct reads of the page and simply not wanted — the requirement is property tax only. The fourth
is the familiar generate-twin failure: it answers even when the field is absent.

Guarded in `build_write_values` rather than by re-wording the prompt: `folio_number` is blanked
whenever the resolved `sub_bill_type` is not `propertytax`, the same policy-not-prompt reasoning
already used for the narrative fields. That kills the invented value, drops the unwanted ones, and
makes `""` an assertable constant on all 32 non-tax sidecars instead of a coin flip. **`folio_number`
is not critical outside property tax, so none of this ever affected routing.**

#### A13f — `property_ubc2.vendor_name` coin flip — **FIXED 2026-09-09** (`commercial-narrative-v19`)

Found while measuring the two UBC notices. The letterhead prints
`THE UNIVERSITY OF BRITISH COLUMBIA`; the generate twin returns the registry spelling
`University of British Columbia`. `_prefer_vendor_generate` already has the right rule for this
("if the two normalise equal, write the clean generate spelling") but it never fired, because
`_normalize_vendor` stripped only **trailing** legal suffixes and left the **leading** "the", so the
token lists differed and neither structural branch matched — leaving the confidence tiebreak to flip.
Measured a **50/50 coin flip across 12 live reads** (6 each) on a base-critical field that auto-writes.

Fixed by dropping a leading "the" in `_normalize_vendor`. `_HAS_MUNICIPAL_PREFIX` already treated a
leading "the" as noise, so this makes normalisation consistent with a convention the module already
held. Measured over 3,625 reads: **6 change, all `property_ubc2`** — the only vendor value in the set
starting with "the" — taking it to `University of British Columbia` 12/12. `property_ubc2` is now a
corpus anchor for it.

**Separately fixed, same investigation:** CU's literal `"null"` string defeating the municipal
invoice-number filename fallback — see `commercial-narrative-v17`, `gates._denull`. That one had a
single root cause and is not part of this item.

---

#### A13a. `abbotsford_water`'s billing period — **FIXED 2026-09-08** (`commercial-narrative-v18`)

**Root cause, and a correction to the original entry.** The bill prints `BILLING PERIOD: Mar/Apr
2026` — months only, no day. GPT-5.5 grounds **both** billing-period extract twins on that one
span (markdown offset 302) and returns `2026-04-01` for each, at the same confidence off the same
bounding box. A sweep of **all 3,574 cached reads** found this is the *only* billing-period extract
date whose grounding span does not contain the day it claims: **the `01` is manufactured, not
printed.** One span read as both ends of a range is not a range.

> **Correction 1.** The original entry blamed this solely on
> `generate_rescue = allow_generate_rescue and g_pass and not e_present`. That explains r3–r8, where
> the extract sat at 0.415 and reached `writeValues` through `resolve_twin`'s `elif e_present` tail
> — but **not r0/r1, where the extract was at 0.921 and CLEARED the bar**, winning on `e_pass`.
> Widening the generate rescue, which the entry implied, would have fixed 6 of 8 reads and left 2
> wrong. The shipped rule ignores confidence entirely, which is why it fixes both regimes.

**The prompt was tested first, and is not the lever.** Three arms, n=12 each, all rolled within six
minutes on 2026-09-08 against a same-day control (the confound `CLAUDE.md` names). Both candidate
arms only **removed** text — the prompt already forbids exactly this, naming `'Mar/Apr 2026'`
verbatim, and has done since `5ff3613` on 2026-07-16, through 141 correct reads.

| arm | prompt chars | collapsed | period correct | vs control |
|---|---|---|---|---|
| A control | 1857 | 11/12 | 1/12 | — |
| B (parenthetical removed) | 1799 | 10/12 | 2/12 | Fisher p = **1.000** |
| C (whole month-only sentence removed) | 1684 | 9/12 | 3/12 | Fisher p = **0.590** |

At n=12 the screen could only have detected a jump to **7/12** (p=0.027). More telling than the
arithmetic: **the failure is byte-identical in all three arms** — every failing read returns
`('2026-04-01', '2026-04-01', None, '2026-04-30')`, every passing read has both extracts null, and
no arm produced any intermediate behaviour. That is the signature of CU's `date`-type normalisation
converting the returned span text, which no prompt wording can reach. `analyzers/` was left
untouched; **no prod analyzer push.**

**The fix.** `field_policy.billing_period_extracts_collapsed` + a suppression step in
`gates.evaluate`, placed before the twin resolution *and* `build_write_values` — both re-resolve
from the same `parsed` dict, so a repair applied to one would disagree with the other. Nothing is
lost: `billing_period_span_implausible` already refused a `start == end` period, so a collapsed
pair could never have been written as one; the rule just applies that invariant early enough to
stop the bad end poisoning the derivation (`2026-04-01 − 60 = 2026-01-31` instead of `2026-03-01`).

**Blast radius, measured by replaying all 3,601 cached reads with the rule on and off:** 33 reads
change at all, **60 written values change — 30 reads × 2 fields, every one `abbotsford_water`,
every one wrong→right.** `routingDecision` changes nowhere. The 3 non-`abbotsford` reads reach
identical written values by a different route. `abbotsford_water` goes 1/12 → 12/12 against its
**unchanged** sidecar.

#### A13b. `property_surrey.billing_period_start_date` — **WITHDRAWN, never a defect**

> **Correction 2.** The original entry said *"the notice prints no billing period at all and GPT-5.5
> invents `2026-01-01` on 9/9."* **Both halves are wrong.** CU's own OCR of the payment stub carries,
> at offset 4630: `DUE DATE JULY 2, 2026` / `JANUARY 1 TO DECEMBER 31, 2026`. The range is printed,
> and the value is read from it — `billing_period_start_date_generate` spans the whole line.

> **Correction 3.** Listing this as a D1 "wrong period reaches Dynamics unseen" was wrong in the same
> way. The written value is **correct**; it was the sidecar that was stale.

The sidecar already asserted `billing_period_end_date: 2026-12-31`, read from the *second half of
that same printed line*, which made it the only sidecar in the corpus asserting a period with an
end and no start. Sibling `property_abbotsford` prints *"For the period January 1, 2026 to December
31, 2026"* and already asserts the same pair, extract twins at 0.953 / 0.924. GPT-5.5 **improved**
here: pre-upgrade the value was `""` 6/6, and on 2 of those reads the start extract collapsed onto
`2026-12-31` and was blanked by the span guard.

Sidecar re-baselined `""` → `"2026-01-01"` (user approved, 2026-09-08). **Residual weakness, not a
defect:** the value rides on the *generate* twin at **0.578, below the 0.73 bar**, because
`billing_period_start_date_extract` grounds on the span `'JANUARY 1'` and returns nothing — it does
not apply its own prompt's year-carry rule, even though the prompt states it (the year is printed
once, at the end of the range). So this key is model-sensitive; if CU is retuned it may go red
again, and that would be a real signal rather than a false failure.

#### A13c. `property_surrey.service_address` — **ACCEPTED as the new truth 2026-09-08**

Accepted on the user's call, and the evidence says the **new** value is the more faithful one. The
notice prints `2790 167 ST` under its own **`PROPERTY ADDRESS`** label (markdown offset 618) and
again as `CIVIC` on the payment stub (offset 5535). The city and postal code appear only in the
**mailing** block at offset 406 — `LYU SHUWEN / XUE LI / 2790 167 ST / SURREY BC V3Z 0A9` — which
is the owner's mail-to, not the serviced property. So the pre-upgrade assertion
`2790 167 ST SURREY BC V3Z 0A9` had spliced the property-address line together with the mailing
block's city and postal code; GPT-5.5 stopped doing that.

Stability on the upgraded model: resolved `2790 167 ST` **9/9**, with `service_address_extract`
returning it on **every** replicate at **0.925–0.959** — far above the 0.73 bar. The only 2 reads
where the generate twin appended the city sat at **0.413**, below the bar. Routing is
`HAPPY_PATH_CANDIDATE` 9/9 either way, so nothing about the gate changed.

Sidecar re-baselined; `service_address` **stays asserted**. This re-baselines a value, it does not
drop coverage on a base-critical field — the standing rule against unasserting `service_address` to
make the corpus green is intact. **Record-shape note:** the written address for this document
family now carries no city / province / postal code. That is the normalisation question **DV-4**
already owns (`Normalise vendor_name and service_address`); no new Dataverse item was opened.

### ~~C6.~~ CLOSED 2026-08-27 — the router's `other` wording is not the defect

**Closed without change (user, 2026-08-27).** The premise did not survive measurement. Six property
tax notices from five municipalities were run through the live router: **all six classified
`general_invoice`**, none rejected, all reaching happy path — and one of them is the *same tax bill*
as the rejected `260603_0021` (identical folio `5395-6128-0065`, identical printed amounts).

So the word "notices" in the `other` description is **not sufficient** to cause a reject, and the
22-of-30 collision count was a real correlation over-read as causation — the same confounding error
stage C documents. The 2026-07-17 batch (16 of 16 within three minutes) fits a CU regime effect far
better than a wording one. The B2 rescue makes these documents extract regardless, so there is no
remaining cost to justify editing a prompt that all 1,019 happy-path documents depend on and that
has no golden-corpus coverage.

**Reopen only on evidence:** re-roll several of the 21 rejected notices at n ≥ 12 against the
current definition and show that a reject still reproduces.

**Original analysis retained below for the record.**

### C6 (original analysis, superseded). The router's `other` description contradicts `general_invoice` on three words

**No Dataverse impact once B8 shipped — but it is why B8 has work to do at all.**

`analyzers/create-router-analyzer.json` describes `other` as "contracts, account statements with
no amount due, letters, **notices**, marketing", while `general_invoice` claims "government or
municipal charge notices such as business license renewal fees, permit fees, and other
**city-issued bills**". A *property tax notice* satisfies both sentences. Measured over the 30
production rejects (2026-08-27, audit behind B8):

| Word in `other` | Rejects whose OCR text contains it | The family it swallowed |
|---|---|---|
| `notices` | 22 | 21 municipal property tax notices |
| `statements` | 13 | 3 TELUS statements (which *do* state an amount due) |
| `contracts` | 5 | the Trail sales order, whose page 2 is a Terms and Conditions page |

The precedent for the fix is in the file's own history: `d4d83c5` narrowed "statements" to
"account statements **with no amount due**". The same qualification is owed to `notices`
(→ notices that request no payment) and `contracts` (→ *unpriced* contracts and agreements), and
"statements" needs tightening further since 3 still slipped through.

> ### ⚠ 2026-08-27 — the word-collision hypothesis is NOT established
>
> Six property tax notices (Abbotsford, Burnaby, Richmond, Surrey, Vancouver ×2) were run
> through the live router the same evening: **all six classified `general_invoice`**, none
> was rejected, and all six reached `HAPPY_PATH_CANDIDATE`. One of them is the *same tax
> bill* as the rejected `260603_0021` — identical folio `5395-6128-0065` and identical
> printed amounts, a different scan of the same notice.
>
> So the document type is **not** inherently misrouted, and the word "notices" in the `other`
> description is **not sufficient** to cause a reject. The 22-of-30 collision count above is a
> real correlation but was over-read as causation — the same confounding error stage C
> documents at length, committed again here. A time-based explanation (the 2026-07-17 batch
> was 16 of 16 within three minutes) now fits the evidence better than a wording one.
>
> **Do not edit the router prompt on the strength of the collision table.** Establish first
> whether a reject reproduces at all today: re-roll several of the 21 rejected notices at
> n ≥ 12 against the current definition. If they classify `general_invoice` now, there is no
> wording defect to fix and C6 should be closed as "CU regime, not prompt".

**Deliberately deferred** (user, 2026-08-27: "A now, then B measured"). B8's rescue makes these
documents extract, so the remaining cost is that 21 property tax notices per season land in the
review queue instead of on the happy path. A router-prompt edit moves the classification surface
that **all 1,019 happy-path documents** currently sit on, and the router has **zero** golden-corpus
coverage — `regress.py` provisions `generalinvoicetest` and calls the child analyzer directly, so
the router is never exercised. Do it under the protocol in troubleshooting.md → "Analyzer version
is confounded with wall-clock time": scratch router (`invoicerouterscratch`), both arms rolled the
same session, n ≥ 12 per arm, and the 2 confirmed near-blank rejects as the negative arm.

**Corpus gap to close alongside it:** no property tax notice exists in
`tests/pre-commit-test/` — the dominant misroute family has no coverage at all.

---

## Resolved

| Defect | Fix |
|---|---|
| **B8** — a router category of `other` was a terminal reject, and **26 of the 30** such rejects in the dev ledger were real payables that extracted nothing. **Severity is re-keying, not data loss:** the SharePoint review-queue branch is live (user, 2026-08-27), and `REJECT_*` and `REVIEW_*` both route to it, so each of these reached a human — carrying 13 null fields to type in by hand. Audited 2026-08-27 over all 1,522 rows (30 rejects, 1.97%): **21 municipal property tax notices**, 3 TELUS statements, 1 insurance, 1 sales order (the reported `bug_260827` Trail Appliances doc); only 2 were correctly rejected (40 characters of OCR — a near-blank "NOBLE / PROFESSIONAL PROPERTY MANAGEMENT" cover sheet) and 2 are ambiguous. Not a label flip: 562 municipal bills routed fine, but the 2026-07-17 batch was **16 of 16** rejected. **Every misroute collides with a literal word in the router's `other` description** ("notices" ×22, "statements" ×13, "contracts" ×5) — and `general_invoice` simultaneously claims "municipal charge notices … city-issued bills", so the two descriptions contradict each other on exactly these documents | `gates.apply_b2_rescue` + `function_app._b2_rescue`: on `other`, re-analyze once with the general-invoice analyzer and re-run the gates; the document is then judged by the ordinary gates, so a clean extraction reaches `HAPPY_PATH_CANDIDATE` and a weak one lands in `REVIEW_B4_CRITICAL_FIELD`. Code-side, **no analyzer push**. Verified on the reported document: `REJECT` with 13 nulls → `HAPPY_PATH_CANDIDATE` with all five commercial critical fields passing. An interim version forced a review on the grounds that the router had disagreed; that was **measured to protect nothing** — B4 already catches the shapes the router rejects correctly (a near-blank page fails `vendor_name`, `service_address` and `total_invoice_amount` on its own), so the downgrade only demoted documents whose fields were all good (user, 2026-08-27: "let B4 decide"). Because a rescued row then looks like any other happy path, the ledger stamps **`RouterCategory`** — query `RouterCategory eq 'other'` to find them. Best-effort by contract — any failure, timeout, or empty re-analysis returns the original reject unchanged, and the second call is given only the *remaining* analyze budget so two calls can never exceed the single-call cap (ledger durations p50 13.4s / p95 52.3s / p99 83.6s against a 100s cap; the two-call path measured **live** on 2026-08-27 at 4.5s + 13.5s = **18.0s total**, and a normal invoice still makes exactly one call). **The prompt-wording half is deliberately NOT fixed here** — see C6 |
| `vendor_name` took the dispatch service from a stylized-logo letterhead (`260629_0024`, correct only **2/12**) | `field_policy.vendor_domain_tiebreak` + rescue in `gates.evaluate`; routing now 12/12 on both the old and new analyzers. Code-side, no analyzer push needed |
| `regress.py` / `test.py` / `diag.py` crashed with `UnicodeEncodeError` on a piped stdout once any field carried non-ASCII | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` in all three entry points |
| **B1** — a below-bar, generate-only `service_address` was written but routed to review, 28 times in 1,633 cached replicates across 5 documents | `field_policy.address_corroborated_by_span` + rescue in `gates.evaluate`: CU's own spans for the value must quote text naming the same place. Code-side, no analyzer push |
| **B6c** — `business_license` routing flipped 4 runs in 10 | Same root cause as B1 (3 runs), plus a run where **both** twins returned nothing. The licensed premises is printed only in a `Locations` table column, which no address-block label covers, so `field_policy.licence_location_address` reads that column directly (municipal only, address-shaped cell, declines on more than one site). 10/10 stable |
| **C2** — 73 stable-but-unverified keys unasserted | Worked down to 20 verified additions across 14 sidecars, applied 2026-08-18. The original 73 shrank because ~48 were raw twins (never in `writeValues`), several were the stably-wrong values that became A3/A4/A5, and this session asserted the rest. The 20: nine `amount_excluding_gst` verified by arithmetic (`total - gst`), seven verifiable absences (`null`/`""` where the document prints no PO, GST, account or day count — the assertions that would catch a field *inventing* one, as A5 and B3 did), and four dates read off the page. Corpus went from 571 to **611** assertions. **Three candidates were withheld**: `recommend_240124_0001.vendor_name` and `west_van_water.vendor_name` are the open B7/B6a flips, and `260629_0024.invoice_date` is wrong (below) |
| **C1** — the corpus scored only the FIRST 3 replicates even when 14-31 were cached, so marginal assertions surfaced only when an analyzer edit busted the cache | `regress.py --all-cached` scores every replicate already on disk, and the pre-commit hook now passes it. Zero CU calls, and it lifted each commit's evidence from 87 scored decisions to **450**. The complementary habit — raising `--replicates` *extends* the cache with fresh rolls instead of discarding them, unlike `--force` — is documented in the flag's help. **Correction (2026-08-18):** only half of this shipped. `.git/hooks/` is not versioned, and the installer was never re-run, so the *running* hook stayed a stale pre-C1 copy without the flag — every commit in between was still gated on 87 decisions. Re-installed via `scripts\hooks\install.ps1` and verified against the running copy; see troubleshooting.md → "The tracked pre-commit hook is not the hook that runs" |
| **C3** — `scorecard.py` enforced a 15-**word** rule that `b20104f` had replaced with a 44-**character** one, so the scorecard reported on a rule that no longer existed | `invoice_description_gate` now measures characters against `INVOICE_DESCRIPTION_MAX_CHARS = 44`, `test.py` reports `char_count` instead of `word_count`, and the orphaned `count_words` is gone. This is the only programmatic check of the 44-character limit anywhere, so it now has its own test. Corpus reality: median 30 chars, p90 41, and 5 of 450 values at or over 44 (all on `260521_0024`) |
| **B4** — `total_invoice_amount_generate` disagreed 1 run in 10 on `abbotsford_water` | **Not reproducible.** `1855.11` on **84 of 85** cached observations and **14/14** on the current analyzer; the single `1952.75` came from the retired analyzer `3dfc9f6fdd22`. The resolved total was always correct and the raw twin is not written to Dataverse. Closed on evidence, no code change |
| **A1** — `invoice_date` silently became **today** when both date twins returned nothing, written unreviewed because the field is not critical | `field_policy.find_invoice_date_in_text` reads the date off its own printed label. Label-anchored, never positional: the reproduction page also prints a due date, a billing period, two meter-reading dates and two 2023 payment dates. Slashed forms are refused (ambiguous, and the shape a print timestamp takes) and it declines when two *different* labelled dates appear. Fixed **five** documents, not the one logged — 23 replicates that were writing today. Three of the five rescued values match sidecar assertions verified long before this change |
| **A5** — `account_number` returned the PO/job number (`260629_0024` on 31/31, `bug_260504_0021` on 3/14) | Discarded when its digits equal the resolved PO **and** the page prints no account label. That second test is what protects a genuine account number: all 18 corpus documents that carry one print a label, and both defect documents print none |
| **A2 + B3** — `number_of_days` wrong on three documents: 13 for a 28-day period (its first month only), and an invented `1` against 30-day and 365-day periods | The period dates arbitrate the count (`field_policy.reconcile_number_of_days`). **Corrects only, never supplies** — a blank count stays blank rather than inheriting the period fields' own instability. A count merely *differing* from the span is kept: five corpus water bills legitimately print a metered count 1-7 days off their period, and that is the consumption the utility billed. Tolerance is `max(10 days, 15%)`, sitting in the measured gap between the widest honest deviation (7) and the narrowest defect (15) |
| **A2 + B3 follow-on** — `number_of_days` **30** on `bug_260601_0018`, a commercial invoice that prints neither a period nor a day count. 1 read in 90, `HAPPY_PATH_CANDIDATE`, so it auto-wrote | The write rule kept an ungrounded count whenever `billing_period_end_date` was merely *non-empty*, and that end date can itself be a below-bar generate-only guess (0.516 here, against a confident-null extract twin). One guess licensed the other: count `1` at 0.238 survived, the start derivation collapsed the period to a single day, the printed-range repair widened it to 06/01–06/30, and `reconcile_number_of_days` then promoted the invented 1 to a fully-invented 30. The rule now also requires the end date to have **passed** — the comment's own premise ("is there a period?") with a test that actually asks it. Verified across all **2,327** cached decisions: exactly the 2 target decisions changed, all 46 legitimate reconciliations kept, and `bug_260609_0031`'s generate-only 19 survives 101/101. Code-side, no analyzer push |
| **A2 + B3 follow-on (2)** — `number_of_days` **30** on `bug_260601_0018` again: 1 of 3 reads in the 2026-09-10 live re-roll (1 of 145 cached reads), which failed the corpus as UNSTABLE `['', 30, '']`. Same shape on `bug_260605_0017` (30, 13 reads), `260521_0024` (365, 30 reads) and `property_port_moody` (365, 1 read): a `1` read only by the generate twin, against a period whose end *passed*, was corrected to the span. Because `reconcile_number_of_days` never supplies, every other read of the same bill wrote `""`, so the count depended on whether CU hallucinated | `gates.evaluate` now **discards** a count whose resolution is not `extract` or `agreement` when the period contradicts it, instead of correcting it (`commercial-narrative-v22`; user decision 2026-09-10, "option A"). This **reverses** the span correction the **A2 + B3** row applied to an invented `1`; a count the extract twin read, or both twins agreed on, is still corrected (`bug_260624_0015`'s 13 → 28 unchanged). Verified over **3,994** cached reads against the v21 code: exactly those **45** decisions changed, only `number_of_days` moved (its value, its `fields` copy, its resolution and its advisory line), and no routing changed. **Not fixed:** `property_surrey`, where the invented `1` first derives a one-day period that the span guard rejects, leaving nothing to contradict it: `1` on 2 of 6 current reads (the A13b chain noted under *Authorised sidecar removals* above). Code-side, no analyzer push |
| **Five defects exposed by the first 12-replicate live roll** (2026-08-19, sha `ed9b045`): `delta_water`/`burnaby_water` `vendor_name` reduced to the bare place name (6/12, 1/12); `vancouver_water.billing_period_start_date` = `2025-10-07` (4/12); `recommend_241105_1061.vendor_name` = `HEATING & COOLING LTD` (1/12); `burnaby_water.vendor_name` = `Revenue Services` (1/12); `richmond_water` and `warranty_260120_0062` `service_address` at the wrong completeness (1/12 each). **All routed HAPPY_PATH_CANDIDATE — every one auto-wrote unreviewed.** | One root cause: **correlated twin failure**. Both twins slip the same way, agree, and the agreement boost promotes a sub-threshold pair to a passing resolution — twin disagreement cannot see a perturbation that moves both twins together. Five page-anchored repairs: `municipal_name_with_prefix` (municipal-gated, whole-name match, max two words), `municipal_payee_override` (the printed *payable to* line; municipal payee only, so BC Hydro and FortisBC are untouched), a strict-**tail** rule in `_prefer_vendor_generate` (a leading subset stays on the confidence tiebreak — `FortisBC` and `diag_260414_0028` want opposite answers), `labelled_service_address` (the `FOR SERVICE AT:` cell, street-number guarded), `address_trailing_postal` (contiguous postal only), and a rule that a **printed** period start is never overwritten by arithmetic over a *metered* day count. Verified: 112 tests, 611/611 on 388 scored reads, and 20 of 2,635 replayed decisions changed — all corrections, zero routing changes. Code-side, no analyzer push. Full write-up: `docs/ai/warranty-prompt-retry-plan.md` |
| **A3** — `gst_amount` 27.50 where the bill's own arithmetic says 32.62, on a sectioned *commercial* invoice (`recommend_260120_0036`), plus the derived `amount_excluding_gst` | The sectioned-GST rescue in `gates.evaluate` now covers both buckets. Safety comes from the 5% identity it already checked, not from the bucket: a bill whose GST genuinely breaks the identity still declines. gst is now 32.62 on 14/14 |
| **A4** — `billing_period_start_date` 20 years off (`2006-01-26`), stable, feeding the tenant utility-sharing calculation | The bill prints `Service Period: 06/01/26-06/30/26` in one format; CU forces the end correctly (30 is not a month) and reads the start as YY/MM/DD. `gates.evaluate` now rejects any period that is inverted or longer than a year, then re-reads the start from the printed range anchored on the trusted end date, falling back to `end − (days − 1)` and only then to blank. Also fixed the same misread on `bug_260601_0018` and two *range-collapse* replicates (`fortisbc`, `surrey_water`) where the start twin had returned the end date |
| **B5** — `invoice_number` written as `8001214179 - 01/14/2026`, 1 run in 14, on a document that A3's fix had just moved from review to auto-write | `field_policy.strip_trailing_date` on the written identifier. Now `8001214179` on 14/14 |
| **B6b** — the same address stored under two strings (`4338 Pandora St
Burnaby , BC` vs the spaced form), which blocked asserting `service_address` on 3 documents | `build_write_values` collapses whitespace in `service_address`, as it already did for `vendor_name`. 12 existing sidecar values were normalised in the same change |
| **D2-a** — `diag_260414_0028.vendor_name` is bimodal at ~**1 read in 24**: `Drips & Drains Plumbing and Heating` instead of the printed `…Ltd.`. Routes `REVIEW_B4_CRITICAL_FIELD`, so it never auto-writes | **Accepted, not fixed — four code rules were tried and every one broke more than it repaired.** On 23 of 24 reads the generate twin returns the short common name `Drips & Drains`, which is *not* equal after normalisation, so the confidence tiebreak keeps the printed extract; on the 24th it returns the full name minus `Ltd.`, which **is** equal after normalisation and therefore wins as "the normalised spelling" at 0.416 over a 0.835 extract. Measured against all 2,896 cached decisions: *prefer the fuller printed spelling* breaks **8** sidecars (`ROMA Heating & Cooling`, `JMEC Electric`, `CentiMark`, …); *strip the suffix unconditionally* breaks the literal-extract-on-disagreement contract; *canonicalise only when the twins normalise-equal* never reaches the failing read; *require the generate twin to clear threshold* breaks **9**, including the casing normalisation on four municipal bills (`City of Surrey` → `CITY OF SURREY`). The generate twin's spelling authority is load-bearing and deliberately confidence-independent. **Instead the assertion was narrowed:** `regress.py._values_equal` compares `vendor_name` with trailing legal suffixes stripped, on that field only. The sidecar keeps `vendor_name` — it is a critical field and stays asserted. Cost: a regression whose *only* effect is adding or dropping a legal suffix on `vendor_name` is no longer caught; a wrong vendor, truncation, casing change or empty value still is |
| **D2-b** — `billing_period_end_date` = the invoice date, generate-only, on `recommend_241105_1061` (1/12) and `bug_260601_0018` (2 cached reads). The first routed **HAPPY_PATH_CANDIDATE**, so it auto-wrote a period the bill does not print | Discarded in `gates.evaluate` when the end date is generate-sourced **and** equals the resolved invoice date. Corroborating against the page cannot catch this — the invoice date *is* printed. Only the generate-only source is guarded: `bug_260528_0016` and `west_van_water` legitimately bill through their invoice date and resolve the end from the **extract** twin on 26/26 and 24/24 cached reads. Verified across all 2,896 cached decisions: exactly 3 changed, 0 routing changes. Code-side, no analyzer push |

