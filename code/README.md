# Ghost Data Still Exists — Deletion-Audit Agent

An AI-powered auditor that, for every deletion request in
`dataset/deletion_requests.csv`, reconstructs what actually happened to the
person's data across Northstar Shop's systems **as of that case's `audit_at`
cutoff**, applies the supplied deletion policy, and emits the ordered,
evidence-cited audit trail as `output.csv`.

## Run

```bash
python code/main.py                    # writes output.csv at the repository root
python code/src/validate.py output.csv dataset   # independent contract validation
python code/evaluation/main.py         # regenerates code/evaluation/evaluation_report.md
```

No API keys are required; the run is fully offline and deterministic
(see `code/evaluation/usage_report.md` for the model-usage statement).

## Architecture

```
                          ┌─────────────────────────────────────────────┐
 dataset/*.csv ──────────►│ Ingestion                                   │
 dataset/evidence/* ─────►│  SQL dumps (sqlite) · JSON readbacks        │
                          │  9 log syntaxes · PDFs · screenshots        │
                          └──────────────┬──────────────────────────────┘
                                         │ canonical facts
                                         ▼
                          ┌─────────────────────────────────────────────┐
                          │ Identity resolver                           │
                          │  current + historical emails/hashes/phones  │
                          └──────────────┬──────────────────────────────┘
                                         │ person ↔ fact linkage
                                         ▼
┌──────────────────────┐  ┌─────────────────────────────────────────────┐
│ system_activity.csv  ├─►│ Workflow-claim association                  │
│ (deletion-worker     │  │  exact min-cost matching per record type,   │
│  claims, catalog     │  │  claim windows anchored at reported_at      │
│  links)              │  └──────────────┬──────────────────────────────┘
└──────────────────────┘                 │
                                         ▼
                          ┌─────────────────────────────────────────────┐
 deletion_rules.csv ─────►│ Policy engine + planner                     │
                          │  treatment per fact → ordered steps:        │
                          │  FINDING → REMEDIATION → VERIFICATION       │
                          └──────────────┬──────────────────────────────┘
                                         ▼
                          ┌─────────────────────────────────────────────┐
                          │ output.csv + validators                     │
                          │  schema · vocabulary · ordering · evidence  │
                          └─────────────────────────────────────────────┘
```

### Key design decisions

1. **One canonical fact model.** Every evidence format (SQL dumps with
   primary/replica drift, JSON readbacks, nine log syntaxes, PDFs, screenshots)
   reduces to the same fact tuple, so the policy engine and planner never see
   format-specific logic. Unknown event names fall back to a generic handler
   driven by the resource-id prefix.
2. **Identity over similarity.** Records match a person only through
   identifiers — current and historical emails, phone numbers, email hashes,
   and the stable `user_XX` reference (case-insensitive). Near-duplicate names
   in the corpus are decoys and never match.
3. **Claims are not proof.** Workflow statuses and `reported_result` values
   locate resources; the state at `audit_at` comes from evidence observations.
   Replication drift is honored: a record still present in any copy (primary,
   read/reporting/regional/failover replica) is a remaining record.
4. **Temporal discipline.** Facts observed after `audit_at` are never used;
   citations prefer files captured at/before the cutoff, falling back to
   observations made at/before the cutoff inside later-exported files.
5. **Policy is data.** `deletion_rules.csv` drives DELETE/REDACT/ANONYMIZE/
   RETAIN; condition nuances (attributability, retention windows, tombstone
   fields, restore paths) are evaluated per fact. Account tombstones keep the
   stable `user_id` and lose direct identifiers, per the policy.
6. **Deterministic core, AI at the edges.** Everything structured is parsed
   deterministically. The AI surfaces in two audited places: vision-model
   extraction of the console screenshots (cached fact tuples, refreshable via
   an optional VLM endpoint) and an intent-classification fallback for request
   texts no rule covers. All usage is ledgered in `evaluation/usage_report.md`.

### Module map

| Module | Role |
|---|---|
| `main.py` | orchestration: load → audits → association → plan → output.csv |
| `src/facts.py` | canonical fact model, record-type/system mappings |
| `src/identity.py` | current + historical identity resolution |
| `src/parse_sql.py` | SQL snapshots: normalized tables + drift payloads |
| `src/parse_json.py` | JSON readbacks, bundles, unified record manifests |
| `src/parse_logs.py` | the nine log syntaxes, trace correlation, event handlers |
| `src/parse_pdf.py` | PDF text extraction (pdfplumber + stdlib fallback) |
| `src/parse_images.py` | screenshot fact extraction (cache-first, optional VLM) |
| `src/activity.py` | system_activity stream, catalog-link facts |
| `src/associate.py` | workflow-claim → case association (exact min-cost matching) |
| `src/intent.py` | multilingual deletion-intent classification (+ LLM fallback) |
| `src/policy.py` | deletion_rules.csv engine (treatment, violations, targets) |
| `src/planner.py` | per-case audit, step construction and ordering |
| `src/validate.py` | independent output validators |
| `src/llm.py` | optional provider-agnostic LLM/VLM client + usage ledger |
| `evaluation/main.py` | evaluation harness → `evaluation/evaluation_report.md` |

## Measured results (regenerable via `code/evaluation/main.py`)

| metric | value |
|---|---|
| sample deletion-status accuracy | **32/32 (100%)** |
| sample exact case match | 17/32 (current measured run) |
| step precision / recall / F1 | 0.944 / 0.876 / 0.909 (current measured run) |
| 518-row contract validation | 0 issues, 0 temporal warnings |

The evaluation report includes full-run ablations. They produce the same exact
case-match count on this small public sample slice, with differences in
step-level precision and recall; the sample is not enough to establish which
variant generalizes best to hidden cases.

## Known limitations

The public sample exact-match score is below 100%; remaining ordered-step
mismatches are listed in `code/evaluation/evaluation_report.md`. These public
sample metrics are useful calibration, but they do not guarantee the hidden
evaluation score. The full 518-row output passes schema, ordering, evidence
reference, and temporal-citation validation.

## Output contract

`output.csv` — `case_id,deletion_status,steps_json`, 518 data rows sorted by
`case_id`. `steps_json` is an ordered JSON array of steps
(`step_no`, `phase`, `system_id`, `resource_id`, `operation`, `state`,
`evidence_id`), phases FINDING → REMEDIATION → VERIFICATION, operations in the
mandated table order, then `system_id`/`resource_id` ascending. Findings and
COMPLETE-case verifications cite the proving `file_id`/`activity_id`;
remediation and post-remediation verification steps for INCOMPLETE cases carry
an empty `evidence_id`. NOT_APPLICABLE requests emit `[]`.

## Evaluation

`evaluation/evaluation_report.md` is generated by `evaluation/main.py` and
reports case/status, step, field and sequence accuracy on the 32 public
sample cases (scored against `sample_output.csv`), three team-created
evaluation cases with hand-derived expected outputs, and the failures found
and fixed during development. `evaluation/usage_report.md` documents the
model usage of the final full-dataset run.
