# Warranty prompt retry — staged plan

**Living tracker.** Two attempts to change the `warranty` field's prompt were reverted after an
unrelated field regressed. This file records why, and the staged path to landing both edits.
Update it as each stage completes; record the commit sha in the checklist.

Opened 2026-08-19. Source analysis: all **2,464 cached CU responses across 30 analyzer versions**.

---

## 1. The two failed attempts

| # | attempt | what it changed in `analyzers/create-generalinvoice-analyzer.json` |
|---|---|---|
| 1 | **C4 — narrative character limits** | The max-character number in **five** descriptions, all set to 500: `diagnosis_solution`, `recommendation`, **`warranty`**, `diagnosis_solution_zh_hant`, `recommendation_zh_hant`. A sixth field, `invoice_description`, also carries a limit (44) and was **not** touched. Reverted before commit, so its exact bytes are not in git. |
| 2 | **Warranty disclaimer wording** | `warranty` only — rewrote steps 3, 5 and the closing line so a disclaimer-only invoice returns `""`. Committed `3af5354`, reverted `68dc21e`. |

Common factor: the **`warranty` description**. Attempt 1 changed its number, attempt 2 rewrote
its text.

## 2. Root cause

**A two-parse letterhead.** On `delta_water` and `burnaby_water` the municipal name is separately
locatable as both the full string and the bare city name, and the extractor picks one:

```
GOOD  spans = 'City of Delta'        conf 0.883
SLIP  spans = 'Delta'                conf 0.416
GOOD  spans = 'City of' + 'Burnaby'  conf 0.875   <- two runs, still resolves correctly
SLIP  spans = 'Burnaby'              conf 0.415
```

Two discrete alternatives, not drift. `richmond_water`, `vancouver_water`, `abbotsford_water`
and `260521_0024` never slip in 82–119 reads each — their letterheads offer no second parse.

**Why it reaches `writeValues`: correlated twin failure.** All three bad reads resolve with
`source = "agreement"`:

```
delta_water   r0/r2   extract 'Delta'/0.416    generate 'Delta'/0.343
burnaby_water r1      extract 'Burnaby'/0.415  generate 'Burnaby'/0.341
```

Both twins sit far below the 0.73 threshold, but they **slip together and agree**, and the
agreement boost promotes the pair to a passing resolution. Twin disagreement — the main safeguard
against a bad read — cannot protect against a perturbation that moves both twins the same way.

It is **not** a classification effect: `bill_type` stays `'municipal'` at 0.856 on every read,
slips included. That hypothesis was tested and discarded.

**Why it matters:** `vendor_name` is a critical field, but `'Delta'` is a plausible non-empty
string, so no gate fires — it routes `HAPPY_PATH_CANDIDATE` and auto-writes to Dynamics
unreviewed.

## 3. Evidence

Two independent fields are anomalous in **exactly** the two warranty-touching analyzer versions
and nowhere else.

**`vendor_name` losing the `City of` prefix** — 3 reads in 768 across the eight `City of X` docs:

| analyzer version | edit | affected reads |
|---|---|---|
| `67b315677a27` | C4 | `delta_water` r0, r2 |
| `bc708c840f30` | warranty rewrite | `burnaby_water` r1 |
| ~17 other versions | various | **0** in 762 reads |

**`account_number_generate` returning the PO number** on `recommend_241105_1061` — 1 of 3 under
C4, 1 of 3 under the warranty rewrite, **0 of 23** elsewhere. Already neutralised at the
write-value level by the shipped A5 rescue, so visible only in the raw twins.

Those two versions hold ~6 % of the reads and 100 % of both anomalies: **p ≈ 3×10⁻⁴**.

> **Method note.** Analysing `burnaby_water` alone gives p = 0.037 and the wrong conclusion
> ("noise"). Pooling the two incidents gives p ≈ 3×10⁻⁴ and the right one. See the attribution
> standard in stage B.

## 4. Stage checklist

| stage | what | status | sha |
|---|---|---|---|
| **A** | `City of` guard — code-side, ships to prod | ☐ not started | |
| **B** | Attribution standard recorded in `troubleshooting.md` | ☐ not started | |
| **C** | Trigger experiment — scratch analyzer, nothing ships | ☐ not started | |
| **D1** | Retry C4 character limits | ☐ blocked on C | |
| **D2** | Retry warranty disclaimer wording | ☐ blocked on C | |

### Stage A — the guard

> On a **municipal** bill, when the resolved `vendor_name` is **exactly** a bare place name and
> the page prints `City of <name>`, write the printed full form.

- Helper in `functionapp/field_policy.py` matching
  `(City|District|Township|Corporation|Town|Village) of <resolved-name>`. The place name may be
  **multi-word** (`District of West Vancouver`), so the pattern must not assume one token.
- Rescue in `functionapp/gates.evaluate`, after twin resolution, **gated on
  `bill_type == municipal`**, emitting an advisory flag.
- Tests in `tests/test_field_policy_gates.py`: bare `'Delta'`, split-span `'City of' + 'Burnaby'`,
  plus a negative case asserting a commercial bill is untouched.

Two constraints, both required, both measured:

- **(a) municipal only** — `bill_type` is `'municipal'` on **100 % of 766 reads** across all eight
  `City of X` documents; zero commercial, so the gate never excludes a legitimate case. It also
  held at 0.856 on the three slip reads, so it does not fail in company with `vendor_name`.
- **(b) exact whole-name match** — so `Delta Plumbing Ltd.` on an invoice mentioning
  `City of Delta` is never rewritten.

Simulated across all 2,464 cached reads before writing any code: fires on exactly **3 reads**,
all municipal, all currently wrong. **Zero false positives.**

Verification:

```powershell
.\.venv\Scripts\python.exe -m pytest
# replay all 2,464 cached responses before/after, diff every writeValues field + routing
.\.venv\Scripts\python.exe scripts\regress.py --replicates 12 --load-local-settings   # 153 live CU calls
```

Then re-score `bc708c840f30` and `67b315677a27` — both must drop to **0** sidecar failures,
confirming this single fragility caused both incidents.

Rollout: verification → commit → stamp → `func azure functionapp publish` → end-to-end check →
push to `main`. **No analyzer push in stage A.**

### Stage B — attribution standard

To be recorded in `docs/ai/troubleshooting.md`. Before attributing a regression to an edit that
did not touch that field, require all three:

1. **Pool related incidents before testing.** One document alone gave the wrong answer.
2. **n ≥ 12 per arm on the affected document**, via `regress.py --replicates N`. n = 3 cannot
   separate a real effect from a coin flip.
3. **Check the confidence distribution, not just the value.** Real coupling showed a confidence
   collapse (z = −3.41); noise did not.

### Stage C — trigger experiment

Runs **after A**, so a slip is observable in the twin data while the guard prevents it corrupting
a written value. **Measure the raw twins** (`vendor_name_extract` / `vendor_name_generate`), not
`writeValues` — once A is in place the guard repairs the written value.

Scope: `delta_water` + `burnaby_water` only. n = 12 per arm.

| arm | edit applied to the **scratch** analyzer | tests |
|---|---|---|
| 1 · control | none — current HEAD verbatim | baseline slip rate |
| 2 · warranty-only | the reverted `3af5354` disclaimer wording, `warranty` only | is it warranty's *content*? |
| 3 · other-narrative | `diagnosis_solution` char limit 600 → 500, nothing else | is it any narrative field? |
| 4 · non-narrative | semantically null edit to a non-narrative description (e.g. trailing full stop on `pst_amount`) | does *any* byte change do it? |

Arm 4 is the important control — it tests the "any edit at all" hypothesis the historical data
cannot rule out.

Steps:

1. `create_analyzer.py --analyzer-id generalinvoicescratch` per arm — **never `generalinvoice`**.
   Scratch pushes are not stamp-gated.
2. Roll n = 12 per arm on the two documents with `--replicates` (never `--force`).
3. Compare each arm against arm 1 **and** the 762-read historical baseline, applying stage B.
4. Delete the scratch analyzer.

Cost: arm 1 is already paid for by stage A's verification roll, so **~72 additional CU calls**.

Power: at the observed effect size (C4 gave 2 of 3 on `delta_water`), n = 12 detects a ≥20 % slip
rate ~93 % of the time. **If an arm returns 0 slips, extend it** — 0 of 12 still admits rates up
to ~22 %.

#### C results — empty until measured

| arm | `delta_water` slips / n | `burnaby_water` slips / n | notes |
|---|---|---|---|
| 1 · control | / | / | |
| 2 · warranty-only | / | / | |
| 3 · other-narrative | / | / | |
| 4 · non-narrative | / | / | |

### Stage D — the retries, contingent on C

| C result | meaning | retry approach |
|---|---|---|
| slips in arm 2 only | warranty's *content* is the trigger | retry the warranty wording with A guarding; n ≥ 12 on both docs **plus a full live corpus pass** to find casualties beyond the two known |
| slips in arms 2 **and** 3 | the whole narrative block is sensitive | both retries carry the same risk; attempt one at a time, never together |
| slips in arm 4 too | *any* analyzer edit perturbs this parse | the retries are no riskier than any other analyzer change; the real conclusion is that every future analyzer edit needs n ≥ 12 baselines |
| no slips in any arm | A removed the practical failure mode | proceed with both retries under normal verification |

Each retry: edit → commit → **full live corpus pass** (not `--all-cached`) → n ≥ 12 on
`delta_water` + `burnaby_water` → stamp → `create_analyzer.py --load-local-settings`. Stage D
changes no code, so no function-app deploy should be needed.

## 5. Standing cautions

- **Never bundle a code fix with a prompt change.** It destroys attribution — the exact failure
  this investigation is about. C4 and the warranty wording are **two separate commits**.
- **Never `--force`.** It re-calls every replicate and discards accumulated evidence, including
  the 3 reads that are the only surviving record of this defect. `--replicates` adds; `--force`
  destroys.
- **Scratch analyzer only in stage C** (`generalinvoicescratch`), and delete it afterwards.
- **Never `create_analyzer.py --force` on `generalinvoice`**, and never weaken a sidecar to make a
  retry pass. A red corpus means investigate, not suppress.
- **A guards the two *known* casualties, not all casualties.** We know of `vendor_name` and
  `account_number` only because the corpus asserts them; narrative fields are deliberately
  unasserted. Each retry still needs its own live corpus pass.
- **The cache preserves CU's output, not historical write values.** Replaying old reads through
  current code masks defects that later fixes now guard — which is why the `account_number`
  damage above is invisible unless you read the raw twins.

## 6. Open questions

- **The agreement boost can promote two sub-threshold twins that failed the same way.** The
  `City of` guard fixes this instance; the general hole is untouched and could affect any twinned
  field. How often correlated twin failure occurs elsewhere has **not** been measured. Worth its
  own investigation, not part of these stages.
- **Why `warranty` specifically** is not observable from cached data — only two analyzer versions
  have ever edited it, so "warranty's content" and "the narrative block" cannot be separated
  without stage C.
- **Vendor-name casing** (`DISTRICT OF WEST VANCOUVER` from the extract twin vs title case from
  generate) remains unnormalised — open defects **B6a / B2 / B7**. Stage A closes only the
  prefix-loss half.
