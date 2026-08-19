# Warranty prompt retry — staged plan

**Living tracker.** Two attempts to change the `warranty` field's prompt were reverted after an
unrelated field regressed. This file records why, and the staged path to landing both edits.
Update it as each stage completes; record the commit sha in the checklist.

Opened 2026-08-19. Source analysis: all **2,464 cached CU responses across 30 analyzer versions**.

> ### ⚠ 2026-08-19 — the trigger hypothesis was measured and is FALSE
>
> Stage C ran three arms at n = 24 each on a scratch analyzer. **No arm differs from any other.**
> Editing the `warranty` description does not move the `vendor_name` parse — the pre-warranty
> definition slips just as often as the edited ones.
>
> The historical "0 slips" baseline was **real**, not a sampling artifact: 0 in 66 `delta_water`
> reads from 2026-07-23 to 2026-08-18. What actually happened is a **step change in CU's
> behaviour around 2026-08-18/19** — the rate went from ~0 % to ~42 % for *every* definition. The
> two edited versions were simply the ones being tested when it happened. See §4 *Stage C — the
> confound*.
>
> **Both reverted prompt edits are exonerated.** Sections 2 and 3 below are kept for the record
> with their withdrawn claims marked; read §4 *Stage C — results* for what is actually true.

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
against a bad read — cannot protect against a failure that moves both twins the same way.

**This half of the root cause survived stage C and is confirmed.** What did *not* survive is the
claim that a prompt edit is what perturbs it — see the banner above. There is no perturbation:
the bare parse is chosen ~42 % of the time on these two documents, always, on every version.

It is **not** a classification effect: `bill_type` stays `'municipal'` at 0.856 on every read,
slips included. That hypothesis was tested and discarded.

**Why it matters:** `vendor_name` is a critical field, but `'Delta'` is a plausible non-empty
string, so no gate fires — it routes `HAPPY_PATH_CANDIDATE` and auto-writes to Dynamics
unreviewed.

## 3. Evidence — ⚠ WITHDRAWN 2026-08-19

**This section's conclusion is wrong.** It is preserved because the *reasoning error* is worth
keeping: it is the same thin-sampling error the stage B standard exists to prevent, committed in
the very analysis that motivated stage B. What follows was the argument; the refutation is after
it.

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
> ("noise"). Pooling the two incidents gives p ≈ 3×10⁻⁴. See the attribution standard in stage B.

### Why that p-value was wrong — analyzer version is confounded with wall-clock time

The arithmetic was right and the baseline was **real**. `delta_water` genuinely returned
`City of Delta` on **66 of 66 reads** between 2026-07-23 and 2026-08-18, across 14 analyzer
definitions — one of them sampled 14 times on its own. At a 42 % slip rate that run of luck has
probability ~10⁻¹⁴. The old regime existed.

The error is **confounding**, not sampling. In this repo *an analyzer version is only ever rolled
on the day it is created.* Nobody re-rolls a superseded definition. So in the cache,
"which definition" and "which day" are the same variable, and any change in CU's own behaviour
over time is indistinguishable from an effect of the edit that happened to be under test that day.

| | measured | when |
|---|---|---|
| 14 older definitions | 0 slips / 66 reads | 2026-07-23 → 08-18 |
| C4 (`67b315677a27`) | 2 slips / 3 reads | 2026-08-18 |
| warranty rewrite (`bc708c840f30`) | 1 slip / 3 (`burnaby`) | 2026-08-18 |
| HEAD (`42db7d9eaee4`), `generalinvoicetest` | 6 slips / 12 | 2026-08-19 |
| HEAD, pre-warranty, and reverted-wording on scratch | 10–14 slips / 24 each | 2026-08-19 |

Read down that column: the rate rises with the **date**, and on 08-19 it is ~42 % for
*definitions that predate the warranty edit entirely*. The edits were bystanders.

**Lesson, generalised:** a cache that rolls each version exactly once cannot attribute anything to
a version. It needs a **concurrent control** — the old definition re-rolled *today*, beside the
new one. That is precisely what arm 0 was, and it is the only reason this was catchable.

## 4. Stage checklist

| stage | what | status | sha |
|---|---|---|---|
| **A** | `City of` guard + four more repairs — code-side | ☑ **done, deployed 2026-08-19** | `ed9b045` |
| **B** | Attribution standard recorded in `troubleshooting.md` | ☑ done | `ed9b045` |
| **C** | Trigger experiment — scratch analyzer, nothing ships | ☑ **done 2026-08-19 — result: NO trigger exists** | n/a (scratch only) |
| **D1** | Retry C4 character limits | ☐ **unblocked** by C — proceed under normal verification | |
| **D2** | Retry warranty disclaimer wording | ☐ **unblocked** by C — but see the `surrey_water` caveat in §4 | |

### What stage A actually shipped

The live `--replicates 12` roll (171 fresh CU calls) took every document from 3 replicates to 12
and **exposed four more defects that 3 replicates had been hiding** — the "a green corpus can be
a lucky draw" effect, measured. All five were fixed together:

| defect | rate | cause | fix |
|---|---|---|---|
| `delta_water` / `burnaby_water` `vendor_name` → bare place name | 6/12, 1/12 | two-parse letterhead | `municipal_name_with_prefix` |
| `vancouver_water.billing_period_start_date` → `2025-10-07` | 4/12 | A4 derivation overwrote a **printed** date using a *metered* day count (117) that legitimately differs from the Oct 1 – Jan 31 span | a printed start is never overwritten by arithmetic |
| `recommend_241105_1061.vendor_name` → `HEATING & COOLING LTD` | 1/12 | extract lost the leading brand word; confidence tiebreak preferred the fragment | extract that is a strict **tail** of generate loses |
| `burnaby_water.vendor_name` → `Revenue Services` | 1/12 | both twins read the remittance block | `municipal_payee_override` — the printed "payable to" line |
| `richmond_water` / `warranty_260120_0062` `service_address` | 1/12 each | twins agreed on the mailing block / stopped before the postal code | `labelled_service_address`, `address_trailing_postal` |

Verification: **112 tests** (from 108), corpus **611/611** on 388 scored reads, and a replay of all
**2,635 cached decisions** showing **20 changed — every one a correction, zero routing changes**.

### Two findings that change how to read the corpus

**1. The test analyzer and prod do not behave the same.** `delta_water.vendor_name` was wrong
**6 times in 12** on `generalinvoicetest`, but **21/21 correct** on the prod `generalinvoice`
analyzer. The two are provisioned from identical JSON. So a corpus failure rate describes the
*test* analyzer and **does not transfer to production** — quote it as such.

**2. The new guards are exercised offline only.** None has fired in production, because the
defects are not reproducing there. They are proven against the 20 cached reads where they do
fire. Deployment itself is confirmed by the publish log (17:43:04Z) against a clean tree at
`ed9b045`, not by observing a repair live.

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

#### The design was broken, and the control arm is what exposed it

Run as designed, **arm 1 slipped 6/12 on `delta_water` and 6/12 on `burnaby_water` — with no edit
applied at all.** The "control" was supposed to establish a near-zero baseline against which an
edited arm would stand out. Instead it reproduced the defect at will.

The reason: **commit `54a4669` "warranty modification" — current HEAD, and current production —
*is* the first warranty attempt, and it was kept.** Arm 1 was never a control; it already
contained a warranty edit. The design compared two edited states and called one of them the
baseline.

**Redesign (arm 0).** Added an arm at commit `3c403ce`, the last state before any warranty edit.
Verified before spending anything that `warranty` is the **only** field whose definition differs
between `3c403ce` and `54a4669` (1481 → 1274 chars), so arm 0 vs arm 1 isolates exactly one
prompt edit. All arms ran on the same `generalinvoicescratch` id, holding the analyzer instance
constant.

#### C results — measured 2026-08-19, n = 12 per document per arm

Slips = the `vendor_name` twin returning the bare place name instead of the printed `City of X`.

| arm | analyzer state | `delta_water` extract | `burnaby_water` extract | pooled extract | pooled generate |
|---|---|---|---|---|---|
| **0 · pre-warranty** | `3c403ce` — original warranty wording (desc 1481 ch) | 5/12 | 5/12 | **10/24** | 17/24 |
| **1 · current** | `54a4669` — kept warranty edit, = HEAD (desc 1274 ch) | 6/12 | 6/12 | **12/24** | 18/24 |
| **2 · reverted wording** | `3af5354` — the reverted disclaimer edit (desc 1490 ch) | 6/12 | 8/12 | **14/24** | 18/24 |
| **5 · LIVE PROD** | `generalinvoice`, same definition as arm 1, different instance | 2/12 | 6/12 | **8/24** | 16/24 |

**Validity check.** `begin_create_analyzer(allow_replace=True)` was confirmed to actually swap the
definition — after each push the live `warranty` description length was fetched back and matched
the local file (1481 / 1490 / 1481 on a repeat). The arms were genuinely different on the service,
so a null result means "no effect", not "the push silently no-opped".

Arm 5 vs arm 1 — **same definition, two different analyzer instances** — gives p = 0.38. Instance
identity does not explain it either.

Fisher's exact, one-sided, against arm 0: arm 1 **p = 0.39**, arm 2 **p = 0.19**. Generate twin:
both **p = 0.50**. **No arm differs from any other.**

Arms 3 (other-narrative) and 4 (non-narrative) were **not run, and are not needed**. They existed
to localise an effect — is it warranty's content, the narrative block, or any byte at all? With
the strongest candidate (arm 2, the actual reverted edit) showing no effect against a true
pre-warranty control, there is no effect left to localise.

#### What stage C establishes

1. **Editing the `warranty` prompt does not perturb `vendor_name`.** Measured directly, twice
   (arm 1 and arm 2), against a clean control.
2. **CU's behaviour on these documents stepped from ~0 % to ~40 % around 2026-08-18/19**, for
   every definition and every instance tested. Not the prompt, not the analyzer id.
   **Production is in the degraded regime** (arm 5, measured directly on `generalinvoice`).
3. **The stage A guard holds against it.** All **96** measured reads — the 24 prod reads included
   — were replayed through `gates.evaluate` at `ed9b045`: **0 wrong `vendor_name`, 96/96 repaired,
   all routing `HAPPY_PATH_CANDIDATE`.** The guard shipped hours before the regime change was
   noticed, which was luck, not planning.
4. **`--replicates 3` cannot support any claim about an analyzer version**, because version and
   date are confounded (above). Every historical "this version is clean" statement rests on 3–5
   reads taken on one day.

#### What stage C does NOT establish

The disclaimer attempt was reverted because the **corpus went red on `surrey_water`**, not
because of `vendor_name`. Stage C measured `vendor_name` on two municipal documents; it says
nothing about `surrey_water`. That failure needs re-examining on its own terms before D2 —
it may be the same ~coin-flip-under-thin-sampling story, or it may be real.

### Stage D — the retries, contingent on C

**C resolved to a row the table did not anticipate: equal slips in *every* arm, including a true
pre-warranty control.** The nearest row is the last one — proceed under normal verification — but
for a different reason than it assumed. A did not remove the failure mode; the failure mode was
never edit-triggered, and A repairs it continuously.

| C result | meaning | retry approach |
|---|---|---|
| slips in arm 2 only | warranty's *content* is the trigger | retry the warranty wording with A guarding; n ≥ 12 on both docs **plus a full live corpus pass** to find casualties beyond the two known |
| slips in arms 2 **and** 3 | the whole narrative block is sensitive | both retries carry the same risk; attempt one at a time, never together |
| slips in arm 4 too | *any* analyzer edit perturbs this parse | the retries are no riskier than any other analyzer change; the real conclusion is that every future analyzer edit needs n ≥ 12 baselines |
| ~~no slips in any arm~~ | ~~A removed the practical failure mode~~ | ~~proceed with both retries under normal verification~~ |
| **⇐ actual: equal slips in all arms** | **no analyzer edit perturbs this parse; the slip is the documents** | **proceed with both retries.** The `vendor_name` risk that blocked them does not exist. Remaining risk is ordinary: any prompt edit can change the field it edits, and the corpus must still pass at n ≥ 12 |

**Before D2 specifically:** re-examine the `surrey_water` corpus failure that actually caused the
revert. Stage C did not test it. Establish whether it reproduces at n ≥ 12 on the *current*
analyzer without any warranty edit — if it does, it is a pre-existing coin flip and not a
consequence of the disclaimer wording at all. Given the regime change, the prior for "it was a
bystander too" is now high.

**New standing requirement — every arm needs a concurrent control.** Never compare a new analyzer
version against cached numbers from an older one. Re-roll the old definition *the same day*,
beside the new one. Cached history is a record of what CU did **then**, and CU has now been
observed to change underneath a fixed definition.

Each retry: edit → commit → **full live corpus pass** (not `--all-cached`) → n ≥ 12 on
`delta_water` + `burnaby_water` → stamp → `create_analyzer.py --load-local-settings`. Stage D
changes no code, so no function-app deploy should be needed.

### Blast-radius sweep — was `vendor_name` the only casualty? (2026-08-19, 0 CU calls)

Compared every field across the regime boundary using the cache alone: `cd1e585c2f1f`
(08-18, 450 reads, 29 docs) vs `42db7d9eaee4` (08-19, 388 reads, 29 docs).

**Only `vendor_name_extract` degraded** — unstable on 5 docs before, 9 after. Every other field
held flat or improved on both instability and empty rate. The narrative fields specifically:

| field | unstable docs before → after | empty rate before → after |
|---|---|---|
| `warranty` | 2 → 2 | 93.8 % → 91.2 % |
| `recommendation` | 4 → 4 | 86.7 % → 86.3 % |
| `diagnosis_solution` | 14 → 13 | 53.3 % → 53.4 % |
| `recommendation_zh_hant` | 4 → 4 | 87.6 % → 87.6 % |
| `diagnosis_solution_zh_hant` | 13 → 12 | 58.4 % → 58.8 % |

Their *text* differs run to run, but that is ordinary paraphrase variance in a generative field
and reads as equivalent quality on inspection — no drop in coverage or specificity.

**The four newly unstable documents are exactly the four `vendor_name` defects stage A fixed**,
and all four were rock-solid before:

| document | before (n=14) | after (n=12) | writeValues after the guard |
|---|---|---|---|
| `delta_water` | `City of Delta` 14/14 | `Delta` on 6 | `City of Delta` 12/12 |
| `burnaby_water` | `City of Burnaby` 14/14 | `Revenue Services` on 1 | `City of Burnaby` 12/12 |
| `business_license` | `CITY OF VANCOUVER` 14/14 | casing flip on 1 | `City of Vancouver` 12/12 |
| `recommend_241105_1061` | `ROMA Heating & Cooling Ltd` 14/14 | `HEATING & COOLING LTD` on 4 | `ROMA Heating & Cooling` 12/12 |

**This reframes stage A's finding.** Those defects were recorded as long-standing coin flips that
3 replicates had been hiding. They were not — they were **14/14 stable the day before** and are
new with the regime change. The "a green corpus can be a lucky draw" reading was wrong here; the
guards were built the same day the defects appeared, which is why they landed in time.

**Conclusion: the blast radius is one field, and it is fully guarded.** No action needed on the
narrative fields.

## 5. Standing cautions

- **Never bundle a code fix with a prompt change.** It destroys attribution — the exact failure
  this investigation is about. C4 and the warranty wording are **two separate commits**.
- **Never `--force`.** It re-calls every replicate and discards accumulated evidence.
  `--replicates` adds; `--force` destroys. (The original wording called 3 reads "the only
  surviving record of this defect" — stage C showed the defect reproduces at ~42 % on demand, so
  that particular justification was wrong. The caution stands on its own merits.)
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

- **Correlated twin failure — no longer a hypothesis, now a measured pattern.** The agreement
  boost promotes two sub-threshold twins that failed the *same* way. Stage A found this to be the
  cause of **five of the five** defects fixed, across **four different fields**:

  | document | field | twins |
  |---|---|---|
  | `delta_water`, `burnaby_water` | `vendor_name` | 0.416 / 0.343, 0.415 / 0.341 |
  | `recommend_241105_1061` | `vendor_name` | 0.492 / 0.432 |
  | `burnaby_water` | `vendor_name` | 0.537 / 0.37 |
  | `richmond_water` | `service_address` | 0.609 / 0.662 |
  | `warranty_260120_0062` | `service_address` | 0.669 / 0.812 |

  Twin disagreement is the pipeline's main safeguard against a bad read, and it is **structurally
  blind** to any perturbation that moves both twins together. Every fix so far is a page-anchored
  corroboration bolted on afterwards — five patches for one hole. A sixth patch is not the
  answer; the resolution rule itself deserves review (e.g. whether agreement should confer a pass
  when *both* twins sit far below threshold, or only corroborate a value one twin already
  supports). **Not scheduled** — worth its own investigation.
- ~~**Why `warranty` specifically**~~ — **answered by stage C: it isn't.** The `warranty` prompt
  has no measurable effect on `vendor_name`. The question was malformed; it presupposed an effect
  that thin sampling had manufactured.
- **Does `--replicates 3` invalidate other conclusions in this repo?** Every "version X is clean"
  claim rests on 3–5 reads. At a 42 % defect rate that is a ~11 % chance of a false all-clear per
  version. Worth auditing which historical decisions depend on such a claim. **Not scheduled.**
- **Vendor-name casing** (`DISTRICT OF WEST VANCOUVER` from the extract twin vs title case from
  generate) remains unnormalised — open defects **B6a / B2 / B7**. Stage A closes only the
  prefix-loss half.
