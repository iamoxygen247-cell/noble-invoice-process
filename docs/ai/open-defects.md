# Open defects — running list

Defects found but **not fixed**, with the evidence behind each. Deferred deliberately so the
work in flight stays reviewable; this file is the backlog they were deferred into.

**How to use this file.** Add a row when you find a defect you are not fixing now. Record the
measured rate, not an impression — "5/10 runs" is actionable, "sometimes" is not. When a defect
is fixed, move it to *Resolved* with the commit sha rather than deleting it, so a future
regression on the same document is recognisable.

**Conventions.** Corpus documents are named by their `tests/pre-commit-test/<stem>` sidecar. A
measured rate like `5/10` means 5 of 10 replicate CU calls on the same document and analyzer.
Rates in this file were measured on analyzer hash `cd1e585c2f1f` (2026-08-17) unless stated.

Last updated: 2026-08-18 (A3, A4, B5 fixed).

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
| 1 | **C1** | The corpus only passes because the cache freezes one roll | No cost per run, but **highest leverage on this list**: it is why the other 15 defects sat unnoticed. The pre-commit hook never passes `--force`, so marginal assertions only surface when someone edits the analyzer and busts the cache | Schedule a periodic `--force` run, or raise the pre-commit replicate count. Not a code fix |
| 2 | **C2** | 73 stable-but-unverified keys are unasserted | Corpus coverage gap. Note the payoff is double-sided: verifying them against the PDFs is also how A3, A4 and A5 were found, so this task *surfaces* Dataverse bugs rather than just adding assertions | Per-document verification against each PDF; do not bulk-add (4 known-wrong, ~48 are raw twins) |
| 3 | **C3** | `scripts/scorecard.py` `invoice_description_gate` is stale | The scorecard reports `invoice_description.length_gate` against a 15-**word** rule that was replaced by a 44-**character** rule in `b20104f`. Misleading output; nothing checks 44 chars anywhere | Small, self-contained |
| 4 | **B4** | `total_invoice_amount_generate` disagrees 1 run in 10 | None operationally — a raw twin, already unasserted, and the resolved total stays correct | Lowest priority on this list; listed for completeness |

## P2 — Deferred until Phase 4 (Dataverse only)

Fix **before** the Dataverse write goes live, not before that.

| # | ID | Class | Defect |
|---|---|---|---|
| 8 | A1 | D1 | `invoice_date` silently becomes today when both date twins fail (**5/10** on one document) |
| 9 | A2 | D1 | `number_of_days` returns 13 or 28 for the same bill — feeds tenant cost-sharing |
| 11 | A5 | D1 | `account_number` returns the PO number |
| 13 | B3 | D1 | `number_of_days` invents a 1-day count (1/10) |
| 14 | B2 + B6a + B7 | D2 | Vendor-name inconsistency — nothing normalises the *name* (legal suffix, case). The address half (B6b) was fixed on 2026-08-18: `build_write_values` now collapses whitespace in `service_address` as it already did for `vendor_name` |

## P3 — Already mitigated

| # | ID | Class | Status |
|---|---|---|---|
| 15 | C4 | D3 | Narrative limits are prompt-only and a `warranty` value measured 403 chars against 400. Mitigated by sizing the Dataverse columns 1000/1000/500 — revisit only if an overshoot is seen in production |

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
| A1 | Invoice Date | not a critical field |
| A2 | Number of Days | not a critical field; feeds tenant cost-sharing |
| A5 | Account Number | critical on *municipal* only; this document is commercial |
| B3 | Number of Days | not a critical field |

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
| C1, C2, C3 | Corpus and tooling health; no runtime effect |

**Count:** D1 = 7, D2 = 3, D3 = 1 (**11 Dataverse-affecting**) and D4 = 5 (**impact today**).
One caveat on the arithmetic: section `B6` covers three variants and is tagged `D2` because two
of them are, but its third variant (`business_license` routing) is `D4` — so the honest split is
11 Dataverse concerns and 6 non-Dataverse ones across 16 sections.

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

### C2. 73 stable-but-unverified keys are unasserted

**`D4` not Dataverse — corpus health**

The audit found 73 keys that are stable at n=10 but not asserted anywhere. They are **not**
safe to add wholesale: at least four are stably *wrong* (A3, A4, A5 above, plus the
`amount_excluding_gst` derived from A3), and ~48 are raw `vendor_name_extract` / `_generate`
twins, which the corpus rule forbids asserting (assert the resolved value, never a raw twin).

The remainder are worth adding **after** per-document verification against the PDF. That would
raise coverage meaningfully — `amount_excluding_gst` and `payment_due_date` are unasserted on
several documents where they are stable and probably correct.

### C3. `scripts/scorecard.py` `invoice_description_gate` is stale

**`D4` not Dataverse — tooling**

Enforces a 15-**word** warning that was superseded by the 44-**character** rule in `b20104f` /
`9a056b8`. Emitted into the scorecard as `invoice_description.length_gate`, so it reports on a
rule that no longer exists. Nothing checks 44 characters programmatically anywhere.

### C4. Narrative character limits are prompt-only

**`D3` the row write itself fails → Diagnosis / Recommendation / Warranty**

`diagnosis_solution` / `recommendation` (800) and `warranty` (400) are enforced in the analyzer
prompt only — by decision, no code truncates. A `warranty` value was measured at **403
characters**. Mitigated by sizing the Dataverse columns at 1000/1000/500, which makes the limit
a target rather than a cliff; revisit only if an overshoot is ever seen in production.

---

## Resolved

| Defect | Fix |
|---|---|
| `vendor_name` took the dispatch service from a stylized-logo letterhead (`260629_0024`, correct only **2/12**) | `field_policy.vendor_domain_tiebreak` + rescue in `gates.evaluate`; routing now 12/12 on both the old and new analyzers. Code-side, no analyzer push needed |
| `regress.py` / `test.py` / `diag.py` crashed with `UnicodeEncodeError` on a piped stdout once any field carried non-ASCII | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` in all three entry points |
| **B1** — a below-bar, generate-only `service_address` was written but routed to review, 28 times in 1,633 cached replicates across 5 documents | `field_policy.address_corroborated_by_span` + rescue in `gates.evaluate`: CU's own spans for the value must quote text naming the same place. Code-side, no analyzer push |
| **B6c** — `business_license` routing flipped 4 runs in 10 | Same root cause as B1 (3 runs), plus a run where **both** twins returned nothing. The licensed premises is printed only in a `Locations` table column, which no address-block label covers, so `field_policy.licence_location_address` reads that column directly (municipal only, address-shaped cell, declines on more than one site). 10/10 stable |
| **A3** — `gst_amount` 27.50 where the bill's own arithmetic says 32.62, on a sectioned *commercial* invoice (`recommend_260120_0036`), plus the derived `amount_excluding_gst` | The sectioned-GST rescue in `gates.evaluate` now covers both buckets. Safety comes from the 5% identity it already checked, not from the bucket: a bill whose GST genuinely breaks the identity still declines. gst is now 32.62 on 14/14 |
| **A4** — `billing_period_start_date` 20 years off (`2006-01-26`), stable, feeding the tenant utility-sharing calculation | The bill prints `Service Period: 06/01/26-06/30/26` in one format; CU forces the end correctly (30 is not a month) and reads the start as YY/MM/DD. `gates.evaluate` now rejects any period that is inverted or longer than a year, then re-reads the start from the printed range anchored on the trusted end date, falling back to `end − (days − 1)` and only then to blank. Also fixed the same misread on `bug_260601_0018` and two *range-collapse* replicates (`fortisbc`, `surrey_water`) where the start twin had returned the end date |
| **B5** — `invoice_number` written as `8001214179 - 01/14/2026`, 1 run in 14, on a document that A3's fix had just moved from review to auto-write | `field_policy.strip_trailing_date` on the written identifier. Now `8001214179` on 14/14 |
| **B6b** — the same address stored under two strings (`4338 Pandora St
Burnaby , BC` vs the spaced form), which blocked asserting `service_address` on 3 documents | `build_write_values` collapses whitespace in `service_address`, as it already did for `vendor_name`. 12 existing sidecar values were normalised in the same change |
