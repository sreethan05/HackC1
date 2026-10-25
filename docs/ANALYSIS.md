# Ghost Data Still Exists — Deep Analysis (pre-build)

Date: 2026-10-24. This document records everything learned from reading the full repo and dataset.
It is the basis for the implementation plan. Nothing here is hardcoded into the solution; all logic
must be derived from `dataset/` at runtime.

---

## 1. The real-world case (what we are actually building)

Northstar Shop runs a commerce platform. A customer requests account + personal-data deletion.
A deletion workflow performs actions across ~10 internal systems and *claims* a status
(COMPLETE / PARTIALLY_COMPLETE / NOT_STARTED). Claims are unreliable:

- operations report SUCCESS but the record still exists in a replica/index/cache;
- a sync job later recreates a deleted record (dataset contains exactly one such event:
  `activity_3387 RESTORE_BACKUP backup_9287 -> search_4882`);
- backups keep restore paths open;
- dashboards show "Complete" while secondary systems keep direct identifiers.

The agent is an **auditor**: it independently reconstructs, per request, the true state of every
resource belonging to the person **at the `audit_at` cutoff**, applies the org's deletion policy
(`deletion_rules.csv`), and emits an ordered, evidence-cited audit trail
(finding → remediation → verification).

## 2. Data model (all participant-facing files)

| File | Rows | Role |
|---|---|---|
| deletion_requests.csv | 518 | Cases: `case_id, email, requested_at, reported_status, reported_at, audit_at, request_text`. **Not sorted** (case_33…case_550); output must be sorted by case_id. |
| sample_deletion_requests.csv / sample_output.csv | 32 / 32 | Calibration ground truth (all 32 are deletion requests; none is NOT_APPLICABLE). |
| user_identities.csv | 850 | Current identity per `user_XX`: name, email, phone, email_hash, updated_at. Emails are unique. |
| user_audit_log.csv | 306 | Historical identity changes; `field_name ∈ {email(107), email_hash(107), phone(92)}`; up to 4 events/user; 196 users. `old_value` gives historical identifiers. |
| system_activity.csv | 4011 | Global event stream 2026-01-04…2026-10-01. `event_type` ∈ {DELETE_RECORD 890, UPDATE_RECORD 672, SYNC_RECORD 447, INDEX_RECORD 400, SEND_EMAIL 368, REFRESH_CACHE 284, RETAIN_RECORD 206, ANONYMIZE_RECORD 204, REDACT_RECORD 173, CREATE_EXPORT 155, CHECK_DATA_FLOW 89, DISABLE_DATA_FLOW 36, BLOCK_RESTORE 36, CHECK_RESTORE_ACCESS 28, LINK_RESOURCE_IDENTITY 12, CLASSIFY_REQUEST 10, RESTORE_BACKUP 1}. `reported_result` ∈ {SUCCESS, FAILED, PARTIAL} — **claims, not proof**. |
| deletion_rules.csv | 15 | Policy: category → condition → required_action (DELETE / REDACT / ANONYMIZE / RETAIN / tombstone-REDACT). |
| evidence_files.csv | 94 | Manifest `file_id → path, origin, captured_at`. Does **not** say which user a file concerns. |
| evidence/ | 94 files | SQL(24), JSON(28), LOG(26), PNG(11), PDF(4), TXT(1). |

### Request email → identity resolution
- 403 cases: email is a **current** email in user_identities.
- 95 cases: email is a **historical** email (old_value of an email change in user_audit_log).
- 20 cases: email matches only **case-insensitively** (e.g. `SIPHO.MOKOENA@GMAIL.COM`).
- ⇒ normalize to lowercase; resolve current-then-historical; 518/518 resolve to a `user_XX`.

## 3. The canonical evidence fact model (the key discovery)

Across all structured evidence (handover TXT, several JSON manifests, and — buried in different
syntaxes — every log file), each fact is the same tuple:

```
(system_id, resource_id, record_type, result, subject_reference, observed_at)
```

Observed `record_type`s map to `deletion_rules.csv` categories:

| record_type | rule category | required action | violation result |
|---|---|---|---|
| USER_PROFILE | USER_PROFILE | DELETE | FOUND (present) |
| SEARCH_INDEX_ENTRY | SEARCH_INDEX_ENTRY | DELETE | FOUND |
| CACHE_ENTRY | CACHE_ENTRY | DELETE | (present in logs/SQL) |
| USER_EXPORT | USER_EXPORT | DELETE | FOUND |
| SUPPORT_ATTACHMENT | SUPPORT_ATTACHMENT | DELETE (belongs only to subject) | FOUND |
| MARKETING_PROFILE | MARKETING_PROFILE | DELETE | (present) |
| SUPPORT_MESSAGE | SUPPORT_MESSAGE | RETAIN+REDACT | IDENTIFIABLE |
| EMAIL_DELIVERY_LOG | EMAIL_DELIVERY_LOG | REDACT (contains email) | (identifiable) |
| ANALYTICS_PROFILE | ANALYTICS_PROFILE | ANONYMIZE (if attributable) | IDENTIFIABLE |
| PRODUCT_TELEMETRY | PRODUCT_TELEMETRY | ANONYMIZE | (attributable) |
| SECURITY_AUDIT_LOG | SECURITY_AUDIT_LOG | RETAIN (within retention) | FOUND ⇒ must stay |
| LEGAL_HOLD_RECORD | LEGAL_HOLD_RECORD | RETAIN (active hold) | (present ⇒ must stay) |
| BACKUP_ARCHIVE | BACKUP_ARCHIVE | RETAIN + block restore | ACTIVE (restore_allowed) |
| ACCOUNT_TOMBSTONE | ACCOUNT_TOMBSTONE | keep user_id, REDACT identifiers | identifiers non-null |
| DATA_FLOW | (recreation risk) | DISABLE if active | ACTIVE |

Result vocabulary: NOT_FOUND, FOUND, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, BLOCKED.

## 4. Evidence file families (94 files)

1. **Unified record tuples** (same schema, 3 containers): `customer_account_handover_*.txt`
   (pipe table), `connector_configurations_*.json`, `customer_export_manifest_*.json`,
   `security_audit_ledger_*.json`.
2. **Service resource export JSON** (11): consolidated per-service readback: account profile_lookups
   (status 404/200 + profile fields), analytics profiles, backup restore_entries (restore_allowed),
   export jobs, connectors (enabled), search index_queries (documents with email), security
   audit_records, support tickets. Linked via `customer_reference: user_XX` or direct email.
3. **Snapshot bundle JSON** (12): per data_store "snapshots" of collections
   (customer_profiles, deleted_customer_references/tombstones, security_audit_events, …).
4. **customer_analytics_report JSON** (1): analytics records with user_id/email fields.
5. **customer_export_jobs JSON** (1): export objects with owner_email, download_available.
6. **SQL dumps** (24):
   - `customer_accounts_failover_replica` (small): account rows only, primary vs failover drift.
   - `customer_data_{read,reporting,regional,failover}_replica` (12): rows =
     `(resource_id, origin_system, collection, payload_json, updated_at)` in a *primary* table and
     one-or-two *replica* tables; **replication drift** = row present in a replica but absent (or
     scrubbed) in primary, or vice versa. Payload JSON carries full state.
   - `customer_data_inventory` (10): normalized per-service tables incl. `snapshot_scope`
     (origin_system + captured_at), backup_restore_entries (restore_allowed 0/1),
     connector_subscriptions (enabled 0/1), security_audit_records (retention_until).
   - `customer_search_primary` (1): search documents only.
7. **Logs** (26 files, 9 syntactic families — all encode the same events):
   `resource_lookup_trace` (key=value, readback_collector), `service_events` (key=value + noise),
   `structured_service_events` (JSON lines), `syslog_archive` (RFC5424-ish), `operations_stream`
   (pipe-delimited), `cloud_runtime_export` (requestId/function/message), `application_trace`
   (bracketed), `search_index_diagnostic` + `notification_delivery` (record_lookup lines),
   `support_replica` (deletion_marker_check). Facts appear in `*_completed` events with fields
   `rows/http_status/result/enabled/restore_allowed/matched_value/direct_identifiers`,
   correlated to a subject via `lookup_key / correlation_key / subject_ref / subject_key /
   matched_value / customer_reference`. Decoy events for other users fill every file.
8. **PDFs** (4): `security_retention_extract` (signed compliance journal for one audit_record),
   `legal_retention_register` (disputed-order/tax legal holds per user, retain-until dates),
   `account_history_report`, `delivery_correction_attachment` (support attachment containing PII).
9. **PNGs** (11): admin consoles (account admin, analytics profile view, backup restore console,
   connector management, customer export, deleted-account reference registry, marketing profile,
   object storage browser, search index inspector, support ticket console, security audit explorer)
   — visual readbacks showing resource states for specific users.

## 5. Decision semantics (reverse-engineered from the 32 sample answers — verified)

**Per-case evidence selection:** a user's identifiers appear in **at most 2 evidence files**
(624 users in 1 file, 136 in 2, 90 in none). The case's facts usually all live in one file; if a
fact lives in a second file, its step cites that file (sample case_04: account via evidence_54,
analytics via evidence_56). Evidence captured **after `audit_at` must never be cited**.

**Status:**
- `NOT_APPLICABLE` — request does not authorize deletion. Dataset family: cases 534–542
  (marketing opt-out, data export/download, password/access help, name correction, marketing-list
  removal, information request, invoice copy, email change, data copy). These 10 have
  `CLASSIFY_REQUEST` activity rows ("classified … as non-deletion") and `reported_status`
  NOT_STARTED. Output: `[]`.
- `COMPLETE` — every relevant resource has its required treatment *and* no active flow/restore
  path *and* required retained records exist. Output = verification steps only.
- `INCOMPLETE` — any policy violation or recreation risk. Output = findings + remediations +
  verifications.

**Step construction from facts (rule-driven, not per-case):**

| Fact at audit_at | FINDING | REMEDIATION | VERIFICATION |
|---|---|---|---|
| DELETE-category record present | REPORT_REMAINING_RECORD PRESENT | DELETE_RECORD ABSENT | VERIFY_RECORD_ABSENT |
| REDACT/ANONYMIZE-category record with identifiers | REPORT_EXPOSED_PII PRESENT | REDACT_RECORD / ANONYMIZE_RECORD | VERIFY_RECORD_REDACTED / VERIFY_RECORD_ANONYMIZED |
| DATA_FLOW enabled | REPORT_ACTIVE_DATA_FLOW ACTIVE | DISABLE_DATA_FLOW DISABLED | VERIFY_DATA_FLOW_DISABLED |
| BACKUP_ARCHIVE restorable | REPORT_RESTORE_RISK ACTIVE | BLOCK_RESTORE BLOCKED | VERIFY_RESTORE_BLOCKED |
| Account record already deleted (404/absent) | — | — | VERIFY_RECORD_ABSENT |
| SECURITY_AUDIT_LOG retained | — | — | VERIFY_RECORD_PRESENT |
| LEGAL_HOLD active | — | — | VERIFY_RECORD_PRESENT |
| Tombstone clean | — | — | VERIFY_RECORD_REDACTED |
| Data flow already disabled / backup already blocked | — | — | VERIFY_DATA_FLOW_DISABLED / VERIFY_RESTORE_BLOCKED |

**Ordering (verified against all 32 samples):**
phases FINDING → REMEDIATION → VERIFICATION; within phase, the operation order given in the
problem statement, then `system_id` ascending, then `resource_id` ascending. `step_no` 1..n.

**Evidence citation:** findings and COMPLETE-case verifications cite the `file_id` that proves the
fact (or `activity_id` when an activity row is the only proof). INCOMPLETE-case remediation and
post-remediation verification: `evidence_id: ""`.

**Interesting confirmed behaviors in samples:**
- Verification in INCOMPLETE cases *also* includes VERIFY_RECORD_ABSENT for the account record and
  VERIFY_RECORD_PRESENT for the retained security audit record when evidence shows them (the
  "final-state checklist" covers compliant resources too).
- The same evidence_id is reused for all steps it proves.
- Decoys are everywhere: similar-name/different-user rows in SQL, other users' events in logs,
  evidence captured after audit_at.

## 6. Special case families (518-set)

- **A. Normal evidence-backed cases** (~498): identifiers found in 1–2 files; the later-captured
  file (if 2) may still be eligible or excluded by audit_at — per-fact selection handles both.
- **B. NOT_APPLICABLE** (10: case_534–case_542): non-deletion intent (multilingual intent
  classification needed; texts are templates — deterministic multilingual keyword rules + LLM
  fallback for robustness). Output `[]`.
- **C. Activity-only cases** (~9: case_33,35,39,50,52,53,55 and similar): the only proof is a
  `LINK_RESOURCE_IDENTITY` activity row (≤ 1 day before audit_at) linking one resource to the user:
  export_1260/1842 (USER_EXPORT), search_428, data_flow_1121, analytics_7438, support_1034,
  backup_9287, object_19 (SUPPORT_ATTACHMENT via "Attachment catalog confirmed … belongs only
  to"), marketing_001. Findings cite the `activity_id`. Temporal filter matters (case_52 must not
  see the 07-18 link for export_1842).
- **D. PDF/image-proven cases** (case_45 audit_record_003 via security_retention_extract.pdf;
  case_61 object_19 via object_storage_browser PNG; case_63 marketing_001 via
  marketing_profile_console PNG): require PDF text extraction + VLM/OCR of screenshots.
- **E. Temporal traps**: e.g., evidence_92 (09-15) vs evidence_93 (09-16) — different users; a
  restore (`activity_3387`, 08-22) that repopulates a record before some audits and the
  corresponding evidence only after; case_52 vs case_33 share user_18 with different audit cutoffs.

## 7. Risks / open questions to resolve during build & validation

1. **Activity-only cases (C)**: exact expected step content is not directly evidenced by samples.
   Strategy: treat the LINK row as proof that the resource existed/was active at link time
   (event_type-specific), emit FINDING+REMEDIATION+VERIFICATION with activity_id cited on findings.
2. **Whether COMPLETE-case verification must include account/audit checks when the case's evidence
   file does not mention them** (samples suggest: include only what the cited evidence proves).
3. **Historical identifiers**: needed to match evidence whose subject_reference is an old email or
   old hash; resolved from user_audit_log (both old and new values of each change).
4. **Ambiguity when 2 files cover a user** (136 users): choose per-fact; if both prove the same
   fact, prefer the file with captured_at closest to but ≤ audit_at (validated on samples).
5. **IMAGE/PDF extraction quality**: 11 consoles + 4 PDFs. Plan: pdfplumber for PDF text (works),
   VLM (or careful OCR) for PNGs. PNG consoles follow one layout template — a VLM prompt per image
   family returns the fact tuples as JSON.
6. **The grader may or may not require citing `audit_id`** (user_audit_log) anywhere; samples cite
   only file_ids. Keep audit_id citation as fallback when it is the only proof.

## 8. Proposed architecture (hybrid deterministic + AI)

```
dataset ──► Ingestion & normalization (CSV / SQL / JSON / 9 log syntaxes / PDF / PNG)
        ──► Identity resolver (current + historical identifiers per user)
        ──► Evidence index (fact tuples: system, resource_id, record_type, result, subject, time, file_id)
        ──► Intent classifier (multilingual deletion-intent; deterministic rules + LLM fallback)
        ──► Per-case audit (temporal filter ≤ audit_at, decoy rejection, drift merge across tables)
        ──► Policy engine (deletion_rules.csv → required treatment per record_type + condition)
        ──► Step planner (status + ordered findings/remediations/verifications, evidence citation)
        ──► Output builder (sorted output.csv, JSON serialization)
        ──► Validators (schema, ordering, evidence-existence, temporal, sample regression)
```

- **LLM used only where needed**: PNG fact extraction (VLM), ambiguous request_text intent
  (fallback), and QA review — everything else deterministic and cacheable. A token-usage ledger
  records every model call for `usage_report.md`.
- **Validation ladder**: (1) 32-sample exact-match harness; (2) schema/ordering validator on 518;
  (3) ≥3 self-built evaluation cases with known truth; (4) full-run consistency checks
  (e.g., every finding's evidence exists and ≤ audit_at).

## 9. Plan (proposed)

1. **Phase 1 — Ingestion & index** (deterministic): parse all 8 structured families + 9 log
   syntaxes + PDFs; build the fact-tuple index with file_id and timestamps.
2. **Phase 2 — VLM/OCR for the 11 PNGs** into fact tuples (cached to JSON so re-runs are free).
3. **Phase 3 — Identity + intent + policy + planner** exactly as §5 tables; emit output for the
   32 samples; iterate until **32/32 exact match** (status, every step, order, evidence ids).
4. **Phase 4 — Special families** (NOT_APPLICABLE classifier, activity-only, temporal traps);
   re-run samples; then full 518 run + validators.
5. **Phase 5 — Evaluation & reports**: sample regression metrics, 3+ custom cases,
   failures-and-improvements log, usage ledger → `evaluation_report.md`, `usage_report.md`.
6. **Phase 6 — Packaging**: README, code.zip (no dataset/venv/caches), root output.csv, log.txt.
