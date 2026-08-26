# Plan — prefix `account_number` with `Account No: `

**Status: IMPLEMENTED 2026-08-24, uncommitted.** D1/D2/D3 (§5) settled by the user, §7
backfill approved, all of §3, §6, §7, §9 and §10 applied. Verification: **113 pytest passed**
(112 + the new `test_account_number_label`) and **611 OK / 0 not OK across 29 corpus docs,
0 CU calls**. Not committed and not pushed — awaiting explicit consent. **This plan file is
excluded from the commit by the user's instruction.**

The one deviation from the plan as written: §3.3 listed `functionapp/README.md:331` as needing
the version bump. On inspection that line reads "Since `commercial-narrative-v9` it also
includes the five narrative fields" — a *historical* statement about when the narrative fields
shipped, so it was correctly left alone. Only `README.md:76` (the sample `policyVersion`)
changed.

**Requirement (user, 2026-08-24):** when the account number field is not blank, the written
value carries the label — account number `123456` becomes `Account No: 123456`. A blank
account number stays blank. The label:

* goes on **`writeValues` only**, never on `fields` / `resolutions` (D1);
* is **presentation only** and must not touch any routing logic (D2);
* applies to **both municipal and commercial bills** — it is not bucket-conditional (D3).

---

## 1. Where the field lives today

| Stage | File | What happens |
|---|---|---|
| Extraction | `analyzers/create-generalinvoice-analyzer.json` | Two twins, `account_number_extract` (span-grounded) + `account_number_generate` (validator). Both return the identifier **as printed**, spaces stripped, dashes/dots kept. Always arrives as `valueString` — verified across 4,226 cached CU field reads, zero non-string. |
| Twin resolution | `functionapp/field_policy.py:1564` | `TWIN_FIELDS` entry → one resolved final `account_number`. |
| Write value | `functionapp/field_policy.py:1676` | `build_write_values` already normalises absent/whitespace → `""` (Dataverse TEXT column, never null). |
| Late repair | `functionapp/gates.py:691` | PO-echo guard — see §2. |
| Emission | `functionapp/gates.py:459 / 468 / 1082` | Three `_result(...)` returns, all carrying `writeValues`. |
| Consumption | `functionapp/function_app.py:343` | `writeValues` goes out of the Function to Power Automate → Dataverse. |

**Nothing internal keys off the value.** Verified:

* **Routing does not** — `evaluate_b4` (`gates.py:310`) reads the *resolution* from
  `resolve_field`, never `write_values`.
* **The ledger does not** — `function_app.py:310` writes 18 named columns; `account_number`
  is not among them. (`PolicyVersion` **is**, so the version bump in §3.3 shows up as a
  clean before/after boundary in the ledger.)
* **The scorecard and `scripts/test.py` do not** — they list the field for print ordering
  only, and assert nothing about its value.
* **No format rule applies** — `FIELD_FORMATS` (`field_policy.py:256`) contains
  `po_or_job_number` and nothing else. Measured: `format_violation_reason("account_number",
  "Account No: 12345-001")` → `None`. A label can never trip a format gate.

**So this is a code-side write-value formatting rule.** No analyzer edit ⇒ **no production
analyzer push** — the cheap, low-blast-radius direction the playbook (§3.3) argues for.

---

## 2. The PO number ↔ account number relationship (your question)

There is exactly **one** runtime coupling, and it runs in one direction: **PO → account**.
Four further relationships are structural rather than executable. None of them is affected by
the label, and all five were re-checked against the code for this review.

### 2.1 The PO-echo guard — the only executable coupling

`gates.py:684-701` → `field_policy.account_number_echoes_po` (`field_policy.py:1335`).

It **discards** `account_number` (sets `""`, resolution source `po_echo_rejected`, raises an
advisory) when **both** hold:

1. `digits(account_number) == digits(po_or_job_number)`, non-empty — punctuation stripped on
   both sides before comparing; and
2. the document's OCR text prints **no account label anywhere** — `_ACCOUNT_LABEL` covers
   *account number / no. / #*, *acct*, *a/c*, *customer id / number / no. / #*.

Condition 2 is what protects a genuine account number that happens to equal a PO: every
corpus document carrying a real account number prints a label, and both defect documents
print none.

It **never modifies the PO** — the test at `test_field_policy_gates.py:2105` asserts exactly
that. It runs at line 691, *after* the OCR PO rescue at line 490, so it always compares
against the final PO.

**Origin (defect A5, `docs/ai/open-defects.md:208`).** `260629_0024` prints `# 11022266`
beside the paying party — that is its *job* number — and carries no customer account
anywhere; the analyzer returned it as `account_number` on **31/31** replicates.
`bug_260504_0021` did the same on **3/14**. Both are commercial, where `account_number` is
informational, so the wrong value auto-wrote with no review. Four corpus sidecars assert
`""` specifically to catch this returning.

**Effect of the label on this guard: none.** `account_number_echoes_po` strips every
non-digit from both sides before comparing, so a labelled value is still recognised.
Measured directly:

| account_number | PO | page prints a label? | echo? |
|---|---|---|---|
| `11022266` | `11022266` | no | `True` |
| `Account No: 11022266` | `11022266` | no | **`True`** — unchanged |
| `Account No: 11022266` | `11022266` | yes | `False` |
| `Account No: 4517813` | `11022266` | no | `False` |
| `""` | `11022266` | no | `False` |

The guard's discard writes `""`, not a bare `"Account No: "`.

### 2.2 Format asymmetry — why the confusion goes one way only

`po_or_job_number` has a **hard format rule**: exactly 8 digits, prefix `11` or `33`
(`_PO_EXACT`). It is enforced at B4, and violating it is what triggers the OCR rescue.
`account_number` has **no format rule at all** — letters, dashes and dots are all legal.

That asymmetry is the whole reason A5 exists: a PO is shaped like a plausible account
number, but an account number is usually not shaped like a legal PO. The confusion is
structurally one-directional.

### 2.3 Criticality asymmetry — why A5 was silent for so long

| Field | Critical for | Informational for |
|---|---|---|
| `po_or_job_number` | commercial (`COMMERCIAL_DELTA`) | municipal |
| `account_number` | municipal (`MUNICIPAL_DELTA`) | commercial |

They are critical in **opposite buckets**. Both A5 documents are commercial, where
`account_number` is not critical — so the wrong value passed straight to Dynamics without
ever routing to a human.

### 2.4 Prompt-level mutual exclusion — advisory only

Both analyzer prompts tell the model to avoid the other field:

* `account_number_extract`: *"Do not return the invoice number, statement number, bill
  number, **PO number, job number, work order number**, …"*
* `account_number_generate` step 4: *"Never answer with a **PO number, job number, work
  order number**, …"*
* `po_or_job_number_generate` step 5: *"Never answer with a … invoice number, **account
  number, customer number**, quote number …"*

A5 is proof these do not hold — which is precisely why the code guard in §2.1 exists. This
is the playbook's own thesis: fix it in code, not in the prompt.

### 2.5 Shared write-value normalisation — cosmetic, not logic

`field_policy.py:1676` loops over `(PO_FINAL, ACCOUNT_FINAL)` together purely because both
are Dataverse **TEXT** columns, so an absent value is `""` rather than `null` (the amount
fields keep `null` — numeric columns reject `""`). They share a line of code, not a rule.
**This loop is where the label goes** (§3.2), applied to `ACCOUNT_FINAL` only.

### 2.6 What is *not* related

* `find_po_candidates` (`field_policy.py:363`) scans the OCR markdown for 8-digit 11/33
  numbers. It never consults `account_number`.
* `resolve_sub_bill_type` derives the commercial sub-type from the PO's first two digits
  (`11` → repair, `33` → service). `account_number` drives nothing downstream.
* The invoice-number filename fallback, the billing-period derivation and the vendor
  rescues touch neither field.

---

## 3. The change

### 3.1 New helper in `field_policy.py`

```python
ACCOUNT_LABEL = "Account No: "

def format_account_number(value: Any) -> str:
    """The written account number, labelled: '123456' -> 'Account No: 123456'."""
```

Rules:

* blank (`None`, `""`, whitespace) → `""` — a bare `"Account No: "` is never written;
* non-blank → `ACCOUNT_LABEL + value`;
* already labelled → returned unchanged. An anchored check for a leading
  `Account No` / `Account Number` / `Acct` (case-insensitive) so a CU read that swallowed
  its own label cannot produce `Account No: Account No: 123456`. Distinct from
  `_ACCOUNT_LABEL`, which searches the *document text*, not the value.

Placed beside `strip_trailing_date` and `normalize_written_text` — the other two write-value
shaping helpers — and unit-tested directly.

### 3.2 Application point — `build_write_values`, at the existing PO/ACCOUNT block

`functionapp/field_policy.py:1676` already reads:

```python
for name in (PO_FINAL, ACCOUNT_FINAL):
    if write[name] is None or (isinstance(write[name], str) and write[name].strip() == ""):
        write[name] = ""
```

The label goes on immediately after, applied to `ACCOUNT_FINAL` only.

**Why here, and not at the end of `gates.evaluate`.** My first reading of this said "decorate
just before `_result`". **That was wrong**, and the correction is the main structural finding
of this review. `gates.evaluate` returns through `_result` in **three** places — verified by
enumerating every `return` in the function:

| Line | Path |
|---|---|
| `gates.py:459` | B2 `REJECT_B2_OTHER_CATEGORY` |
| `gates.py:468` | `REVIEW_NO_CHILD_EXTRACTION` |
| `gates.py:1082` | the normal path |

Both early exits emit `writeValues`, so an end-of-function decoration would emit an
**unlabelled** account number on two response shapes. `build_write_values` is called once at
`gates.py:455`, before all three, so a single edit covers every path — and the rule stays in
the module whose docstring says it owns "the derived write values sent to Dynamics".

The one interaction it creates — the PO-echo guard receiving a labelled string — is proven
harmless in §2.1. A comment goes in at that call site recording that the guard sees the
labelled value and matches on digits, so the next person adding an `account_number` rule to
`gates.py` knows what they are holding.

### 3.3 `POLICY_VERSION` bump

`field_policy.py:39`: `commercial-narrative-v9` → `commercial-narrative-v10`. This repo bumps
it on every requirement change (`bc305b6` account number, `5ff3613` billing info, `3c403ce`
narrative fields). Two doc sites quote the current value and follow along:
`functionapp/README.md:76` and `:331`.

`docs/power-automate-design.html:336/681` and `docs/ai/troubleshooting.md:1855` cite v9 as the
version a past feature *shipped in* — historical statements, left alone.

---

## 4. Verification already performed

Everything below was measured, not assumed. **No repo file was modified**; the simulation ran
from a scratchpad script that monkeypatches `build_write_values` in memory.

### 4.1 Baseline is green

```
.\.venv\Scripts\python.exe -m pytest                                  -> 112 passed in 2.25s
.\.venv\Scripts\python.exe scripts\regress.py --load-local-settings --all-cached
   -> 611 OK / 0 not OK across 29 docs · 348 scored · 0 CU calls, 348 cache hits · PASS
```

The `0 CU calls` line is the direct proof of the §8 cost claim: `regress.py` keys its cache on
(sha12 of the analyzer JSON bytes, sha12 of the PDF bytes, replicate index) — `regress.py:96-107`
— and a change confined to `gates.py` / `field_policy.py` moves neither hash.

### 4.2 Simulated change, replayed over the whole cache

The proposed helper was applied as a wrapper and **every cached CU result replayed twice**
(baseline vs patched) through `gates.evaluate`, diffing the full `writeValues` map,
`routingDecision`, and `fields.account_number`:

```
corpus: 29 docs with cached replicates (2896 raw results)

keys that differ between baseline and patched: ['account_number']
routingDecision changes: NONE
docs whose account_number is unstable across replicates: [('diag_260414_0028', ['', 'Account No: 1714548'])]
```

That is 2,896 replays — the full cache across every analyzer version on disk, not just the
348 the current-analyzer run scores. Three claims are now evidence rather than reasoning:

1. **Only `account_number` changes.** Every other `writeValues` key is byte-identical on all
   2,896 replays. No amount, date, address or narrative field moves.
2. **`routingDecision` never changes** — the §5 D2 claim, confirmed at n = 2,896.
3. **The audit surface stays raw** — `fields.account_number` is unchanged on every replay,
   which is the §5 D1 recommendation behaving as described.

The blank branch is exercised too: 9 documents produce `""`, never a bare `"Account No: "`.

### 4.3 One observation worth recording

`diag_260414_0028` flips between `""` and `Account No: 1714548` across replicates. **This is
pre-existing and unrelated** — its sidecar note already documents it ("account_number flips
between the invoice number 1714548 and null across replicates — pre-existing noise, this doc
prints no account number") and the field is deliberately not asserted there. The label does
not create the flip; it makes it visible in the simulation output. No action.

### 4.4 Count correction

`open-defects.md:404` says "all **18** corpus documents that carry one print a label".
Measured from the sidecars today: **19**. The entry dates from 2026-08-18 and one document has
been added since. Not material to A5's reasoning, and I am not editing that historical entry —
noting it so the 19 in §7 does not look like a miscount against it.

---

## 5. Decisions — settled (user, 2026-08-24)

### D1 — `writeValues` only, not `fields` ✅ DECIDED

The response carries the value twice, and only the second one gets the label:

```json
"fields":      { "account_number": {"value": "425096", "confidence": 0.95} },
"writeValues": { "account_number": "Account No: 425096" }
```

`fields.*` and `resolutions.*` stay raw — they are the review/audit surface recording what CU
literally read, with its confidence. `writeValues` is what Dynamics receives, and it is the
only place the label appears.

**Implementation consequence:** the label goes in `build_write_values` (§3.2) and **not** in
`_result`. `_result` injects the resolved final into `fields.<name>` from the `resolutions`
map, which is untouched — so this decision falls out of the chosen application point for free.
Verified in §4.2: `fields.account_number` is identical on all 2,896 replays.

`_result`'s docstring says "the response, the scorecard, and the Dynamics write all read the
same resolved value". That acquires one deliberate exception, which the code comment will
name.

### D2 — presentation only, never routing ✅ DECIDED

The label must not reach any routing logic. It does not, structurally:

* `evaluate_b4` (`gates.py:310`) reads the **resolution** via `resolve_field`, never
  `write_values` — so a labelled write value is invisible to the critical-field gate.
* `account_number` has **no format rule** (`FIELD_FORMATS` holds `po_or_job_number` only), so
  the label cannot trip a format violation.
* The PO-echo guard compares **digits only** (§2.1), so the label cannot change its verdict.
* The ledger does not store `account_number` at all.

Measured at n = 2,896 in §4.2: **zero `routingDecision` changes**. `REVIEW_B4_CRITICAL_FIELD`
reasons and advisory flags keep naming the bare field, which is correct — they describe what
was read, not what is written.

Test 4 in §6 pins this: a municipal bill writes the labelled value **and** still routes
`HAPPY_PATH_CANDIDATE`.

### D3 — both buckets, unconditionally ✅ DECIDED

The label applies to **municipal and commercial bills alike**. It is not bucket-conditional.

This is what the chosen application point already does: the label sits in the
`(PO_FINAL, ACCOUNT_FINAL)` block at `field_policy.py:1676`, which runs before and outside the
`bucket` branch. `build_write_values` has exactly **one** bucket-conditioned write rule — the
narrative blanking of `COMMERCIAL_ONLY_FIELDS` on municipal bills (`field_policy.py:1748`) —
and the account label is deliberately not joining it.

Confirmed on the corpus. Of the 19 documents that carry an account number:

| Bucket | Count | Documents |
|---|---|---|
| municipal | 15 | `260521_0024`, `abbotsford_water`, `bchydro`, `bug_260528_0016`, `bug_260609_0031`, `bug_260624_0015`, `bug_260629_0026`, `burnaby_water`, `business_license`, `delta_water`, `fortisbc`, `richmond_water`, `surrey_water`, `vancouver_water`, `west_van_water` |
| commercial | 4 | `bug_260601_0018`, `bug_260605_0017`, `recommend_260120_0036`, `warranty_260120_0062` |

All 19 get the label in the §4.2 simulation. The 9 blanks are all commercial — every
municipal bill in the corpus prints an account number, which is consistent with it being a
municipal-critical field.

Note the asymmetry this does **not** disturb: `account_number` is *critical* for municipal and
*informational* for commercial (§2.3). That is a routing distinction and stays exactly as it
is. The written label is bucket-blind; the gate that decides whether a missing account number
sends the invoice to a human is not.

---

## 6. Unit tests (`tests/test_field_policy_gates.py`)

Four existing assertions read the bare value out of `writeValues` and gain the label:

| Line | Case | Now | After |
|---|---|---|---|
| 2116 | labelled page keeps a PO-shaped account | `"11022266"` | `"Account No: 11022266"` |
| 2284 | printed form, dashes kept | `"12345-001"` | `"Account No: 12345-001"` |
| 2300 | twins agree, extract's literal form written | `"123456789012"` | `"Account No: 123456789012"` |
| 2334 | commercial, informational, sub-threshold | `"A-778812"` | `"Account No: A-778812"` |

One assertion deliberately does **not** change: line 2101, the PO-echo discard, still expects
`""`.

New test `test_account_number_label`:

1. non-blank → labelled;
2. blank / `None` / whitespace → `""`, never a bare `"Account No: "`;
3. already-labelled input → not doubled;
4. through `gates.evaluate`: a municipal bill writes the labelled value **and** still routes
   `HAPPY_PATH_CANDIDATE` (D2 asserted, not assumed);
5. through `gates.evaluate`: a **commercial** bill writes the labelled value too, and
   `fields.account_number` stays raw on both buckets (D1 + D3 asserted, not assumed);
6. through `gates.evaluate`: the PO-echo discard still yields `""` on a labelled value —
   the §2.1 interaction, pinned;
7. the B2 early-exit path (`gates.py:459`) also emits the labelled value — the regression the
   §3.2 correction exists to prevent, and the one case the corpus cannot cover.

---

## 7. Golden corpus — sidecar backfill (**needs your OK**)

This is a requirement change to what `account_number` means, so the standing rule in
`CLAUDE.md` applies: *a requirement change must update **every** sidecar*.

The exact edit list, produced by the §4.2 simulation rather than by hand:

| Sidecar | Now | After |
|---|---|---|
| `260521_0024` | `5077636` | `Account No: 5077636` |
| `abbotsford_water` | `120950` | `Account No: 120950` |
| `bchydro` | `12956781` | `Account No: 12956781` |
| `bug_260528_0016` | `4517813` | `Account No: 4517813` |
| `bug_260601_0018` | `7300-689280-0000` | `Account No: 7300-689280-0000` |
| `bug_260605_0017` | `22-57740-63006` | `Account No: 22-57740-63006` |
| `bug_260609_0031` | `13574616` | `Account No: 13574616` |
| `bug_260624_0015` | `13621820` | `Account No: 13621820` |
| `bug_260629_0026` | `13632134` | `Account No: 13632134` |
| `burnaby_water` | `3001221` | `Account No: 3001221` |
| `business_license` | `994194` | `Account No: 994194` |
| `delta_water` | `42068` | `Account No: 42068` |
| `fortisbc` | `3019807` | `Account No: 3019807` |
| `recommend_260120_0036` | `250255` | `Account No: 250255` |
| `richmond_water` | `117697` | `Account No: 117697` |
| `surrey_water` | `425096` | `Account No: 425096` |
| `vancouver_water` | `6012807` | `Account No: 6012807` |
| `warranty_260120_0062` | `6042641001` | `Account No: 6042641001` |
| `west_van_water` | `122901` | `Account No: 122901` |

**9 keep `""`** — `260629_0010`, `260629_0024`, `bug_260504_0021`, `bug_260615_0006`,
`bug_260629_0012`, `bug_260703_0038`, `diag_260106_0008`, `recommend_240124_0001`,
`recommend_241105_1061`. Four are the A5 anchors; they keep asserting absence and are the
guard that a blank never becomes a bare `"Account No: "`.

**1 does not assert the field** — `diag_260414_0028` (§4.3). Untouched.

**No re-verification against the PDFs is needed, and none is being skipped.** Each of the 19
values was verified against its PDF when first asserted; this is a mechanical prefix of an
already-verified value, not a new claim about what the document says. No corpus document is
added (there is no bug and no reproduction PDF here), and none is removed or loosened.

This touches 19 tracked files, which is exactly the kind of sidecar change the standing rule
says to ask about first. **Confirm before I edit them.**

---

## 8. Verification to run after implementing

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\regress.py --load-local-settings --all-cached
```

Expected: 112 + new tests pass; **611 OK / 0 not OK across 29 docs, 0 CU calls**. Tier B is
free (§4.1) and makes no network call.

Because no CU call happens, none of the analyzer-attribution cautions apply — no concurrent
control, no n ≥ 12 arm, no analyzer version confounded with wall-clock time. A red run here is
a code bug in the diff, full stop.

---

## 9. Documentation

| File | Change |
|---|---|
| `functionapp/README.md:82` | Sample response shows `"account_number": "123456789012"` → labelled form. |
| `functionapp/README.md:76`, `:331` | `policyVersion` → `commercial-narrative-v10`. |
| `functionapp/README.md:325` | The §3 flow-mapping note describing what `writeValues.account_number` carries. |
| `docs/ai/dataverse-todo.md` | New TODO — see §10. |

---

## 10. Dataverse TODO (record-write half, logged not fixed)

Per the standing rule, anything whose only impact is the written record is a TODO in
`docs/ai/dataverse-todo.md`, not a defect:

1. **Column width.** The label adds 12 characters. The longest account number in the corpus is
   `7300-689280-0000` (16 chars) → 28 with the label. Confirm the Dataverse text column is at
   least ~64 wide so nothing truncates silently.
2. **Is the label wanted in the data at all?** Baking a label into a stored value is
   presentation-in-the-database. If the CRM form could render `Account No:` as a field label
   instead, that is the cleaner home for it. Worth one confirmation before this ships, because
   unwinding it later means a data backfill, not a code revert. The requirement as stated is
   explicit, so this is a flag, not a blocker.
3. **Existing rows are not backfilled.** Records written before this change keep bare account
   numbers, so the column will hold both shapes. Consistent with how every prior fix shipped,
   but worth stating in case the CRM side filters on it. `PolicyVersion` in the ledger marks
   the boundary (`commercial-narrative-v9` → `v10`).

---

## 11. Deployment

* **No analyzer push.** `analyzers/*.json` is untouched, so `create_analyzer.py` is not run
  and the prod `generalinvoice` analyzer stays at its current definition.
* **Function app deploy** makes it live (`docs/deploy-to-azure.html`).
* Ordering does not matter — unlike the `invoice_date` twin rollout, there is no
  analyzer/code coupling to sequence.

---

## 12. Out of scope

No extraction change, no prompt change, no routing change, no change to the critical-field
policy, no corpus additions or removals, no adjacent cleanup. No commit and no push without
explicit consent.

---

## 13. Next step

**Settled:** D1, D2, D3 (§5), user 2026-08-24.

**Still waiting on: approval of the 19-sidecar backfill (§7).** That is the only open item.

On approval the implementation is: the helper + application point in `field_policy.py`,
`POLICY_VERSION` → v10, one comment at the PO-echo call site in `gates.py`, 4 changed test
assertions + 1 new test, 19 sidecars, 4 documentation lines, 1 Dataverse TODO — then the two
commands in §8. No commit or push without explicit consent.
