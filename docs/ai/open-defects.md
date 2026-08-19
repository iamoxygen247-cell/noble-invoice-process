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

Last updated: 2026-08-19 (sha ed9b045: five defects exposed by the first 12-replicate live roll, all fixed and deployed. See docs/ai/warranty-prompt-retry-plan.md).

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

---

## Resolved

| Defect | Fix |
|---|---|
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
| **Five defects exposed by the first 12-replicate live roll** (2026-08-19, sha `ed9b045`): `delta_water`/`burnaby_water` `vendor_name` reduced to the bare place name (6/12, 1/12); `vancouver_water.billing_period_start_date` = `2025-10-07` (4/12); `recommend_241105_1061.vendor_name` = `HEATING & COOLING LTD` (1/12); `burnaby_water.vendor_name` = `Revenue Services` (1/12); `richmond_water` and `warranty_260120_0062` `service_address` at the wrong completeness (1/12 each). **All routed HAPPY_PATH_CANDIDATE — every one auto-wrote unreviewed.** | One root cause: **correlated twin failure**. Both twins slip the same way, agree, and the agreement boost promotes a sub-threshold pair to a passing resolution — twin disagreement cannot see a perturbation that moves both twins together. Five page-anchored repairs: `municipal_name_with_prefix` (municipal-gated, whole-name match, max two words), `municipal_payee_override` (the printed *payable to* line; municipal payee only, so BC Hydro and FortisBC are untouched), a strict-**tail** rule in `_prefer_vendor_generate` (a leading subset stays on the confidence tiebreak — `FortisBC` and `diag_260414_0028` want opposite answers), `labelled_service_address` (the `FOR SERVICE AT:` cell, street-number guarded), `address_trailing_postal` (contiguous postal only), and a rule that a **printed** period start is never overwritten by arithmetic over a *metered* day count. Verified: 112 tests, 611/611 on 388 scored reads, and 20 of 2,635 replayed decisions changed — all corrections, zero routing changes. Code-side, no analyzer push. Full write-up: `docs/ai/warranty-prompt-retry-plan.md` |
| **A3** — `gst_amount` 27.50 where the bill's own arithmetic says 32.62, on a sectioned *commercial* invoice (`recommend_260120_0036`), plus the derived `amount_excluding_gst` | The sectioned-GST rescue in `gates.evaluate` now covers both buckets. Safety comes from the 5% identity it already checked, not from the bucket: a bill whose GST genuinely breaks the identity still declines. gst is now 32.62 on 14/14 |
| **A4** — `billing_period_start_date` 20 years off (`2006-01-26`), stable, feeding the tenant utility-sharing calculation | The bill prints `Service Period: 06/01/26-06/30/26` in one format; CU forces the end correctly (30 is not a month) and reads the start as YY/MM/DD. `gates.evaluate` now rejects any period that is inverted or longer than a year, then re-reads the start from the printed range anchored on the trusted end date, falling back to `end − (days − 1)` and only then to blank. Also fixed the same misread on `bug_260601_0018` and two *range-collapse* replicates (`fortisbc`, `surrey_water`) where the start twin had returned the end date |
| **B5** — `invoice_number` written as `8001214179 - 01/14/2026`, 1 run in 14, on a document that A3's fix had just moved from review to auto-write | `field_policy.strip_trailing_date` on the written identifier. Now `8001214179` on 14/14 |
| **B6b** — the same address stored under two strings (`4338 Pandora St
Burnaby , BC` vs the spaced form), which blocked asserting `service_address` on 3 documents | `build_write_values` collapses whitespace in `service_address`, as it already did for `vendor_name`. 12 existing sidecar values were normalised in the same change |
