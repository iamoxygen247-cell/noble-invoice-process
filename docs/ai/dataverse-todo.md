# Dataverse / Dynamics — TODO list

Work that only matters once invoice values reach a **Dynamics record**. These are **TODOs, not
defects** (user, 2026-08-19).

**Standing rule — where a finding goes.**

| Finding | File |
|---|---|
| Wrong value, bad routing, corpus or tooling health | `docs/ai/open-defects.md` |
| Mapping, column shape, normalisation for the Dynamics write | **this file** |

If a finding is both, log the extraction half as a defect and the record-write half here, and
cross-reference.

---

## First, the naming: Dataverse *is* Dynamics CRM's database

These are not two stores. **Dataverse is the data layer that Dynamics 365 CRM (Sales, Customer
Service, Field Service) runs on.** A Power Automate flow writing to Dynamics CRM does so through
the **Microsoft Dataverse** connector's *Add a new row* action — the rows land in Dataverse
tables, and the CRM app is the UI over them.

So the "Phase 4 Dataverse write" in `docs/power-automate-design.html` and the write already built
in the flow are **the same thing**, described in different words. Nothing in this repo proposes a
second copy of the data, and no additional storage is being suggested.

One caveat worth confirming once: this identity holds for Dynamics 365 **CE / CRM**. Dynamics 365
**Business Central** and **Finance & Operations** have their own databases and only sync to
Dataverse optionally. If the flow targets one of those instead, the column-metadata instructions
in DV-1 do not apply.

---

## Current state — the write is live; the ledger does not know it

**The flow already writes invoice values, including the narrative fields, into Dynamics**
(user, 2026-08-19). That is the authoritative statement of what exists.

The ledger tells a different story, and the gap between them is itself a finding. Queried live on
2026-08-19 (`InvoiceExtractProcessLog` on `stinvoicedevwestus`, 1,387 rows, 2026-07-17 →
2026-08-20):

| Signal | Value |
|---|---|
| `Status` | `Extracted` × **1387** — zero `Written` |
| Rows carrying a `DynamicsRecordId` | **0** |
| `RoutingDecision` | `HAPPY_PATH_CANDIDATE` 942 · `REVIEW_B4_CRITICAL_FIELD` 418 · `REJECT_B2_OTHER_CATEGORY` 27 |

`ledger.py` reserves `Status=Written` + `DynamicsRecordId` for the flow to stamp *after Dynamics
returns 201*. The flow writes but does not stamp, so **the ledger cannot tell you which invoices
reached CRM** — see DV-2. Roughly 942 happy-path invoices should have records; the ledger records
none of them.

**Consequence that follows from the write being live:** values have been landing in real records
since at least 2026-07-17, so extraction defects were corrupting records rather than sitting
latent. That is what `open-defects.md` lines 35-41 already say, and it is correct — I had briefly
concluded otherwise from the empty ledger stamp, which was the wrong reading. All seven of those
defects are now fixed and live in prod. There is **no backfill**: records written before each fix
still carry the wrong value.

---

## The list

| ID | TODO | Urgency |
|---|---|---|
| DV-1 | Read the invoice table's real column sizes | **now** — an oversized value fails the row write in production |
| DV-2 | Stamp `Written` + `DynamicsRecordId` back to the ledger after the write | **now** — no audit trail today |
| DV-3 | Audit which of the 21 `WRITE_FIELDS` the flow actually maps; `pst_amount` is undocumented | **now** — unmapped keys are silently discarded |
| DV-4 | Normalise `vendor_name` / `service_address` before the write | data quality |
| DV-5 | Narrative column headroom (ex-C4, closes stage D1) | mitigated, see below |
| DV-6 | Refresh the stale `warranty` contract in `power-automate-design.html` | **now** — semantics changed tonight |
| DV-7 | `account_number` is now written with an `Account No: ` label | **now** — column width + is the label wanted in the data at all |
| DV-10 | Telecom bills (Telus, Rogers/Shaw) now write `bill_type = municipal` | **informational** — value change in a live column, no backfill |

---

### DV-1 — Read the real column sizes

`docs/power-automate-design.html` **prescribes** Diagnosis Solution 1000, Diagnosis Solution
(Chinese) 1000, Recommendation 1000, Recommendation (Chinese) 1000, Warranty 500. That is a build
instruction, not an observation. Since the write is live, an undersized column is not a future
risk — it rejects the whole row and loses the invoice **today**.

Maker portal: make.powerapps.com → the flow's environment → **Tables** → switch the view from
*Recommended* to **All** (custom tables are hidden under Recommended) → the invoice table →
**Columns** → each column's *Maximum character count*.

Or query the metadata directly, signed into the same browser:

```
https://YOUR_ORG.crmN.dynamics.com/api/data/v9.2/EntityDefinitions?$select=LogicalName,SchemaName&$filter=contains(LogicalName,'invoice')

https://YOUR_ORG.crmN.dynamics.com/api/data/v9.2/EntityDefinitions(LogicalName='YOUR_TABLE')/Attributes/Microsoft.Dynamics.CRM.MemoAttributeMetadata?$select=LogicalName,MaxLength
```

"Multiple Lines of Text" is `MemoAttributeMetadata`. A column missing from those results was
created as single-line Text (`StringAttributeMetadata`), which defaults to **100 characters** and
would reject every narrative value — check explicitly.

Record the org URL and table logical name in `CLAUDE.local.md`; they are tenant-specific and no
doc in this repo has them.

### DV-2 — Stamp the ledger after the write

The flow writes to Dynamics but never sets `Status=Written` or `DynamicsRecordId`, so all 1,387
rows sit at `Extracted` forever. Costs today:

- No way to answer "did invoice X reach CRM?" without opening CRM.
- No reconciliation is possible — the monitoring design's reconciliation flow depends on this
  stamp.
- A silent Dynamics failure is invisible; the ledger looks identical whether the write succeeded
  or threw.

`ledger.py` already supports it: `upsert` is idempotent, preserves the original `IngestedUtc`, and
never blanks a recorded `DynamicsRecordId`. The flow needs an update action after the Dataverse
step, mapping the returned row id.

### DV-3 — Which of the 21 `WRITE_FIELDS` are actually mapped?

`field_policy.WRITE_FIELDS` carries 21 keys. The design doc documents mapping expressions for 8
(the billing-period trio and the five narratives). **`pst_amount` appears nowhere in `docs/`** —
it has been in `WRITE_FIELDS` since 2026-07-13 with no column and no mapping recorded.

An unmapped key is silently discarded at the flow boundary with no backfill, so every invoice
processed while it is unmapped loses that value permanently. Worth a field-by-field pass against
the live flow.

Typing traps the design doc already calls out: date and number columns reject `""`, so those need
`if(empty(...), null, ...)` guards; text columns take `""` happily, and blank is meaningful
(municipal bill, or the invoice says nothing).

### DV-4 — Normalise `vendor_name` and `service_address`

*Migrated from `open-defects.md` P2 #14 (defects B2 / B6a / B6b / B7).*

The stored value is defensible on every run, but the same real-world entity lands under different
strings, breaking grouping, dedupe and vendor reporting:

| | vs |
|---|---|
| `FortisBC Energy Inc.` | `FortisBC - Natural gas` |
| `DISTRICT OF WEST VANCOUVER` | `District of West Vancouver` |
| `CAMBIE ROOFING CONTRACTORS LTD.` | `CAMBIE ROOFING CONTRACTORS` |

One normalisation step — case-fold for comparison, strip legal suffixes, collapse address line
breaks — fixes all of them together.

**Caution:** four code rules aimed at the vendor-name half were measured against all 2,896 cached
decisions and every one broke more than it repaired (see D2-a in `open-defects.md` *Resolved*).
The generate twin's spelling authority is load-bearing and deliberately confidence-independent —
it is what turns `CITY OF SURREY` into `City of Surrey`. Any normalisation must sit **after**
resolution, on the way into the write, never inside the twin tiebreak.

### DV-5 — Narrative column headroom

*Migrated from `open-defects.md` P3 #15 (C4).* Also closes **stage D1** of
`docs/ai/warranty-prompt-retry-plan.md`.

The five narrative limits (800 / 800 / 400 / 800 / 800) live in the analyzer prompt only; nothing
truncates in code, by decision. A model cannot count characters, so the limit is a target, not a
bound — the real protection is the column being larger than the limit (DV-1).

**Stage D1 proposed setting all five prompt limits to 500. It should not be done:**

1. `warranty`'s prescribed column is 500, so a 500 prompt limit makes the instruction exactly
   equal to the column, removing the 100 characters of absorption that is the actual mitigation.
   For `diagnosis_solution` / `recommendation`, 800 → 500 tightens instructions nothing has
   approached.
2. The overshoot it was built around is gone. Measured across cached replicates of both analyzer
   versions, 0 CU calls:

   | field | before D2 (`42db7d9eaee4`) | after D2 (`bc708c840f30`) |
   |---|---|---|
   | `warranty` | 2 docs, max **435 / 400** — a real overshoot on `warranty_260120_0062` | 1 doc, max **255 / 400** |
   | `diagnosis_solution` | max 474 / 800 | max 426 / 800 |
   | `recommendation` | max 405 / 800 | max 396 / 800 |
   | `diagnosis_solution_zh_hant` | max 256 / 800 | max 234 / 800 |
   | `recommendation_zh_hant` | max 167 / 800 | max 168 / 800 |

   The D2 edit (`9406364`) cut `warranty` from 435 to 255 by removing text that should never have
   been in the field.

**D1 is superseded by D2, not done** — the edit never shipped and should not. If a hard guarantee
is ever wanted, a code-side cap in `build_write_values` gives one; it was deliberately not done so
that no summary is silently cut.

Sample caveat: only one corpus document emits a non-empty `warranty` after D2. Re-measure against
production traffic before treating this as settled.

### DV-6 — The design doc's `warranty` contract is stale

`docs/power-automate-design.html` describes `warranty` as capturing "any warranty, guarantee or
coverage statement — **including disclaimers such as 'repairs cannot be guaranteed'**". Commit
`9406364`, pushed to prod 2026-08-19, reversed exactly that: `warranty` now reports coverage
**only** and returns `""` when the sole warranty wording is a disclaimer, limitation or denial.

Because the write is live, this is a **semantic change to a column already in use**. Records
written before 2026-08-19 contain disclaimer summaries; records after contain coverage or blank.
No backfill. Anyone reading that column for reporting needs to know the cut-over date.

The section's sizing rationale is stale too — it argues from a measured 403-character `warranty`,
and the post-D2 maximum is 255.

### ~~DV-9~~ — WITHDRAWN 2026-08-27

`invoice_date` is **not** written blank after all. Two variants were written and both reverted
before shipping (blank everywhere, then blank on municipal only); the user's decision is that a
blank invoice date always becomes **today's date, in both buckets**. The CRM date column therefore
sees no change and needs no action. See open-defects A8 for the measurement that prompted it.

### DV-10 — Telecom bills change bucket: `bill_type` goes `commercial` → `municipal`

User requirement, 2026-09-08: *"If you see vendor name is Roger Business, Shaw or Telus, please
classify the invoice as Municipal bill."* Shipped in `commercial-narrative-v16`, code-side in
`field_policy.vendor_bill_type_override` — the analyzer was **not** touched, so there was no prod
analyzer push. Like DV-8, this changes written values for a document class **already flowing
through the pipeline**, so it is recorded here rather than as a defect.

Measured on the two corpus anchors, 5 replicates each: CU classified both `commercial` on 5/5
(the `bill_type` prompt lists *"private telecom, internet, and phone companies"* as commercial in
as many words), and the override flipped both on 5/5.

**Five columns change. No backfill** — records written before the cut-over carry the old values,
and anyone reporting on these columns needs the cut-over date:

1. **`bill_type`** — `commercial` → `municipal` for Telus and Rogers/Shaw invoices.
2. **The five narrative columns** (`diagnosis_solution`, `diagnosis_solution_zh_hant`,
   `recommendation`, `recommendation_zh_hant`, `warranty`) are now force-blanked for these
   vendors, because `build_write_values` blanks them for the municipal bucket. This is a
   *correction*, not a loss: before the change the generate twins were inventing phone-service
   prose on these bills (`diagnosis_solution` was unstable across replicates on both documents).
   Historical telecom rows may therefore contain invented narrative text.
3. **`invoice_number`** now carries the SharePoint filename fallback on these bills
   (`260825_telus`, `260901_rogers`), because the municipal bucket makes `invoice_number`
   critical. Under the old commercial bucket the column was written empty. `defaultedFields`
   names it, so a defaulted value stays distinguishable from a read one.
4. **`sub_bill_type`** keeps the value `other` but by a different route — the commercial rule
   derives it from the PO prefix, the municipal rule from the classified label. Measured `other`
   on 5/5 both ways, so no visible change today; it would diverge if CU ever labelled a telecom
   bill with a municipal sub-type.
5. **`routingDecision`**, indirectly: the Rogers bill moves `REVIEW_B4_CRITICAL_FIELD` →
   `HAPPY_PATH_CANDIDATE` (the municipal bucket drops the `po_or_job_number` + `gst_amount`
   requirements a telecom bill can never satisfy), so telecom invoices begin auto-writing where
   they previously queued for review. This is the point of the change, but it means the row
   volume into Dynamics goes up.

**Column readiness is not a blocker**, on the same standing decision recorded under DV-8 (*"No
need to worry about dynamics columns"*, user 2026-08-27): `municipal` is an existing `bill_type`
value, not a new one, so a choice set would already accept it.

**Sub-item — `docs/power-automate-design.html` is now incomplete** (the DV-6 pattern, but a
widening rather than a reversal). Two passages explain municipal behaviour by example and those
examples no longer cover the class:

* the narrative blanking is described as *"always `""` on a municipal bill (a water or hydro bill
  diagnoses nothing…)"* — telecom bills now join that set;
* the invoice-number default is described as *"A municipal bill carried no invoice number, so the
  Function defaulted it from `fileName`"* — still true, now also reached by telecom bills.

Neither is wrong; both would mislead a reader trying to predict which documents blank narratives.

### DV-8 — `sub_bill_type` gains a new value, `propertytax`

User requirement, 2026-08-27: *"property tax bills are considered municipal bills. sub_bill_type
should be `propertytax`."* Shipped in `commercial-narrative-v11`. This reverses an explicit rule —
both `sub_bill_type` prompts previously routed property tax to `other` in as many words — so the
written value **changes for a document class that is already flowing through the pipeline**.

Measured on a scratch analyzer, 2026-08-27: six tax notices (Abbotsford, Burnaby, Richmond,
Surrey, Vancouver ×2) all classified `propertytax`, both twins agreeing at 0.858–0.892; controls
held (`burnaby_water`→water, `business_license`→business_license, `bchydro`→electric). All six
already reach `HAPPY_PATH_CANDIDATE` and auto-write today, so this changes a value in **live**
records, not a latent one.

**Column readiness is NOT a blocker (user, 2026-08-27):** asked whether the Dynamics
`sub_bill_type` column is a choice set that would reject a new value, the user answered *"No need
to worry about dynamics columns."* So the analyzer ships without a CRM-side change and the
write-failure risk is accepted. Recorded because if property tax rows do start failing their row
write after v11, this is the first thing to re-examine.

Remaining, informational only:

1. **Spelling.** `propertytax` is one word, deliberately as the user specified, and is
   inconsistent with the existing `business_license`. Cheap to change now, expensive once records
   carry it.
2. **No backfill.** Property tax notices written before v11 carry `other`. Anyone reporting on
   `sub_bill_type` needs the cut-over date, exactly as for DV-6.

### DV-7 — `account_number` now carries an `Account No: ` label

User requirement, 2026-08-24: a non-blank account number is written as `Account No: 123456`
rather than `123456`. A blank one stays `""`. Applies to **both** buckets — municipal and
commercial alike — and to `writeValues` only; `fields.account_number` keeps the raw read for the
review UI. Shipped in `commercial-narrative-v10`, code-side in
`field_policy.format_account_number`, no analyzer change.

Three things to confirm on the Dynamics side:

1. **Column width.** The label adds 12 characters. The longest account number in the corpus is
   `7300-689280-0000` (16) → 28 labelled. Confirm the column is at least ~64 wide so nothing
   truncates silently. Same question as DV-1, for this column specifically.
2. **Is the label wanted in the data at all?** Baking a label into a stored value is
   presentation-in-the-database. If the CRM form can render `Account No:` as a field label, that
   is the cleaner home for it. Raised with the user before implementation and the requirement was
   confirmed as stated — recorded here because unwinding it later is a data backfill, not a code
   revert.
3. **No backfill.** Rows written before this change hold bare account numbers, so the column now
   holds both shapes. `PolicyVersion` in the ledger marks the boundary
   (`commercial-narrative-v9` → `v10`). Anyone filtering or joining on that column needs the
   cut-over date, exactly as with DV-6.

### DV-11 — `folio_number` is a new `WRITE_FIELDS` entry

User requirement, 2026-09-09: capture the **folio / roll number** on property tax notices, with
the extract + generate twin treatment, and make it a **critical field on those notices only**.
Shipped in `commercial-narrative-v20` — new `folio_number_extract` / `folio_number_generate`
fields in the analyzer, `field_policy.FOLIO_FINAL` in `WRITE_FIELDS`, and
`field_policy.PROPERTYTAX_DELTA` in `critical_fields`. **This one needs an analyzer push as well
as a function deploy** (order matters: analyzer first, or the field returns null for every
invoice).

Four things to settle on the Dynamics side:

1. **A new column is required.** `writeValues` now carries a 22nd key. If the flow maps fields
   explicitly, `folio_number` is dropped silently until the mapping is added; if it maps
   dynamically, the write fails on an unknown column. Confirm which, before the deploy.
2. **It is now IDENTICAL to `account_number` on every property tax notice — decided 2026-09-09.**
   The `account_number` prompt already listed `Folio, Folio Number, Folio No., or Roll Number`
   among its labels, so the two fields already agreed on 16 of the 20 notices. The user then
   required that where a notice prints **both**, the folio wins — implemented code-side in
   `field_policy.propertytax_account_from_folio` (municipal + `propertytax` only; 11 reads, 4
   documents). So on a tax notice the two columns now always carry the same value.

   **Amended 2026-09-09 after the live re-roll:** the substitution now also drives the *review*
   decision, not just the written value. It first changed only `write[account_number]`, leaving
   `evaluate_b4` to judge the account twins the folio had replaced — so when CU returned no
   `account_number` at all (blank field entry) while the folio came back at 0.875–0.988 from the
   same span, the notice went to a human even though the record was correct. `gates.evaluate` now
   sets `resolutions[account_number]` to the folio's value and confidence with
   `source="propertytax_folio"`. The bar is unchanged: the folio must still pass its own
   resolution. **For the Dataverse write nothing changes** — the same value was always written;
   what changed is how often a tax notice reaches the flow without a human touching it first.

   **Two consequences to settle before Phase 4:**

   * **The displaced values were the notices' own labelled account numbers.** `property_ubc` wrote
     `RPT-1088-2731` (its eTaxBC account) and `property_north_van_district` wrote `100198790007` —
     which that stub explicitly labels as the number to use for **ONLINE BANKING**. If anything
     downstream pays, matches or reconciles from `account_number`, it now holds a property
     identifier the bank does not recognise. Raised with the user before implementation and
     confirmed as stated; recorded here because reversing it later is a data backfill, not a code
     revert. The displaced values are preserved in each sidecar's note.
   * **Redundancy.** Two Dataverse columns holding one value on this document family. Either is
     safe to map; mapping both stores it twice.

   `account_number`'s **prompt** was deliberately left untouched, so the 18 existing
   `account_number` assertions on other documents did not move.
3. **Width and shape.** Values observed run to 16 characters (`631000816.058`,
   `23-0122-012-000`) and keep dashes, dots and leading zeros — so the column must be **text,
   not numeric**: `0025.100` and `095016.000` lose meaning as numbers.
4. **Empty on every other document, enforced in code.** Non-tax invoices write `""`, not null
   (same treatment as `po_or_job_number` and `account_number`). `build_write_values` blanks the
   field whenever the resolved `sub_bill_type` is not `propertytax`, because CU returned a value
   anyway on four non-tax documents — three municipal **water** bills that genuinely print a folio
   (`delta_water` prints `FOLIO: 163-061-00-0`), and `260521_0024`, which invented the word
   `COMPLEX`. See A13g. Confirm the column is nullable-or-empty-safe.

5. **`number_of_days` is no longer asserted on property tax notices** (user, 2026-09-09: the flow
   does not consume it for that family). The field is still written — this changed the corpus's
   assertions, not the record. Its observed value on a tax notice flips between `""` and the
   period-derived `365`, so anything consuming it for property tax should treat it as unreliable.

---

## History

The seven defects once classed `D1` — *Dataverse, silently wrong* (A1, A2, A3, A4, A5, B3, B5) —
are all fixed, code-side, and live in prod as of 2026-08-19. They stay in `open-defects.md`
*Resolved*: they were real defects that corrupted real records, and rewriting that history would
lose the reproduction detail.
