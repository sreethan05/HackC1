# Evaluation Report - Ghost Data Still Exists

Produced by `code/evaluation/main.py`; every number below is computed by running the agent, never hard-coded.

## 1. Case-level deletion-status accuracy (32 public sample cases)

- status accuracy: **32/32 = 100.0%**
- exact case match (status + full ordered step array + evidence ids): **17/32 = 53.1%**

## 2. Step-level and field-level accuracy (sample cases)

- step precision: **0.944**
- step recall: **0.876**
- step F1: **0.909**
- field-level accuracy over aligned steps (phase, system_id, resource_id, operation, state, evidence_id): **0.835**

## 3. Sequence accuracy

- fully ordered-sequence match: **17/32 = 53.1%**. The planner emits phases and operations in the mandated table order with system_id/resource_id ascending, so ordering is deterministic by construction.

## 4. Additional evaluation cases created by the team

### custom_01_intent - access-reset request (non-deletion intent)

- derivation: The person asks for login help and explicitly says not to close the account; no deletion is authorized, so no audit steps may be proposed.
- result: status correct **True**, exact **True**, field accuracy **1.00**, step F1 **1.00**

### custom_02_attachment_incomplete - user_07: object-storage attachment owned only by the subject

- derivation: The object-storage browser capture (evidence_46) shows delivery_confirmation_19.pdf still stored under support-attachments and the attachment catalog (activity_4007) confirms it belongs only to the subject. Policy deletes such attachments; nothing else about this person is evidenced near the audit cutoff, so only the attachment carries steps.
- result: status correct **True**, exact **False**, field accuracy **0.94**, step F1 **0.67**

### custom_03_retained_complete - user_03: deletion complete, security audit record retained

- derivation: The signed compliance journal (evidence_55) proves audit_record_003 for user_03 exists with a retention date (25 Aug 2027) beyond the audit cutoff; policy retains the security audit log, so the required check is that the retained record is still present and no remediation is proposed.
- result: status correct **True**, exact **False**, field accuracy **0.83**, step F1 **1.00**

### custom_04_tombstone_redact - user_542: tombstone retains direct identifiers

- derivation: The snapshot bundle (evidence_51) shows tombstone_9178 for user_542 with name, email, phone and email_hash populated while account_status=DELETED. The tombstone rule keeps the stable reference and strips identifiers, so the finding is exposed PII with a redaction remediation; the deleted account and the retained audit record are evidenced in the same bundle.
- result: status correct **True**, exact **True**, field accuracy **1.00**, step F1 **1.00**

### custom_05_export_delete - user_08: user export archive still downloadable

- derivation: The export-job listing (evidence_59) shows export_001 owned by the subject with the download still available; USER_EXPORT is an unconditional DELETE. The account itself is evidenced nowhere near the audit cutoff, so the export is the sole remediated resource and the account check is a proposed future verification.
- result: status correct **True**, exact **True**, field accuracy **1.00**, step F1 **1.00**

### custom_06_marketing_delete - user_09: live marketing profile with direct identifiers

- derivation: The marketing console (evidence_60) shows marketing_001 live with the subject's email and phone, and the data catalog (activity_4008) ties the resource to the subject; MARKETING_PROFILE is an unconditional DELETE, so the case is incomplete until the profile is removed.
- result: status correct **True**, exact **False**, field accuracy **0.94**, step F1 **0.67**

### custom_07_support_redact - user_22: support conversation still names the customer

- derivation: The support console capture (evidence_28) shows ticket CS-5034 whose conversation text contains the customer's email; a support conversation is retained but must be redacted, so the finding is exposed PII with a redaction remediation. The account resource id for this person is only recoverable from the workflow claim, so the account check is a proposed future verification.
- result: status correct **True**, exact **False**, field accuracy **0.94**, step F1 **0.67**

### custom_08_flow_disable - user_14: identity-sync data flow still enabled

- derivation: The connector console (evidence_14) shows data_flow_1121 healthy and continuously syncing with the subject's address as its selector; an enabled data flow can recreate deleted data, so the risk must be reported and the flow disabled before verification can pass.
- result: status correct **True**, exact **False**, field accuracy **0.94**, step F1 **0.67**

## 5. Per-record-type step accuracy (sample cases)

Aggregated over all sample steps by resource family - shows which resource families the pipeline handles perfectly and where the residual cost lands.

| family | tp | fp | fn | precision | recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| account | 27 | 7 | 11 | 0.794 | 0.711 | 0.750 |
| search | 35 | 1 | 1 | 0.972 | 0.972 | 0.972 |
| analytics | 15 | 7 | 10 | 0.682 | 0.600 | 0.638 |
| support | 22 | 5 | 5 | 0.815 | 0.815 | 0.815 |
| export | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| backup | 8 | 4 | 4 | 0.667 | 0.667 | 0.667 |
| data_flow | 14 | 7 | 7 | 0.667 | 0.667 | 0.667 |
| tombstone | 4 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| audit_record | 15 | 2 | 9 | 0.882 | 0.625 | 0.732 |

## 6. Measured ablation study (each row is a full re-run)

| variant | status | exact | precision | recall | F1 |
|---|---|---:|---:|---:|---:|
| full pipeline | 32/32 | 17 | 0.944 | 0.876 | 0.909 |
| no workflow association | 32/32 | 17 | 0.95 | 0.881 | 0.914 |
| no hint bonus | 32/32 | 17 | 0.95 | 0.881 | 0.914 |
| no per-case exclusion | 32/32 | 17 | 0.95 | 0.881 | 0.914 |

Reading: on this public sample slice, the variants have the same exact case-match count, while ablations change step-level precision/recall. This is a small calibration set; it does not establish which variant generalizes best to the hidden cases.

## 7. Error analysis of the non-exact sample cases

The public sample evaluation has 15 non-exact cases: `case_03`, `case_04`, `case_09`, `case_18`, `case_20`, `case_21`, `case_23`, `case_24`, `case_25`, `case_26`, `case_27`, `case_28`, `case_29`, `case_30`, `case_31`. Status accuracy is reported above. Residual differences are in ordered steps, resource attribution, or evidence citation; the aggregate metrics do not by themselves establish that any mismatch is impossible to resolve from the participant-visible corpus. Treat these as remaining accuracy gaps, not as proven dataset defects. The full output is schema-validated, but schema validity does not establish hidden-case prediction accuracy.
## 8. Failures found and improvements made

During development, the evaluation harness surfaced these concrete failure
modes and motivated the listed changes. They improved behavior, but the remaining
sample mismatches below mean the current implementation is not fully exact.

1. **Whole-string identifier tokens.** `LINK_RESOURCE_IDENTITY` details were tokenized as one
   string, so activity catalog links matched no identity and activity-only cases came out
   `COMPLETE` with no steps. Fixed by regex-extracting emails and user references from the
   details text, plus a freshness window (catalog links are readback statements).
2. **Nested evidence payloads.** Search readbacks nest documents under `hits.documents[]`, SQL
   readback payloads under `document`/`profile`, and support conversations embed identifiers in
   free text. Subjects were empty, so facts never matched their person. Fixed by payload
   flattening and free-text identifier scraping.
3. **HTTP-status readbacks dropped.** `profile_lookup` rows with `status_code: 404` fell through
   `derive_status` (no PII fields present), discarding the strongest absence proofs. Fixed with
   an explicit status-code branch.
4. **Claim-type cross-matching.** Workflow association let a `DELETE_RECORD` claim for an account
   satisfy a search-index need, producing steps with wrong system ids. Fixed by pinning claim
   events to the record type of their resource id.
5. **Assignment ties.** Clustered workflow claims (several cases reporting at the same minute)
   were split arbitrarily. Fixed with an exact minimum-cost assignment per component, one-sided
   claim windows (claims run up to the report), and evidence-co-occurrence hints.
6. **Stale snapshot rows.** Later-exported bundles carried pre-deletion rows that beat fresher
   404 readbacks. Reconciliation now prefers manifest-citable files and the most recent
   observation, with a positive-absence (explicit NOT_FOUND) preference for citations.
7. **Historical identities.** Requests signed with a superseded email/hash initially matched
   nothing. Fixed by resolving both sides of every identity-field change in `user_audit_log.csv`.

Known residual limitation: workflow-claim association can assign the wrong resource to a case
when the evidence does not uniquely link the claim to a subject. The remaining mismatch list
is measured against public sample answers; further source-level investigation is needed to
separate implementation errors from underdetermined associations.

## 9. Contract validation over the final 518-row output

- rows: 518
- contract issues: **0** (schema, ordering, vocabulary, evidence existence, empty-array rules)
- temporal notes (file exported after audit_at while every cited observation was made at/before audit_at): 0
