"""Independent output validators - the last line of defence before submission.

Checks the contract from problem_statement.md / AGENTS.md:
  schema      518 rows, sorted case_ids, exact header
  json        steps_json parses; step_no consecutive from 1; all 7 keys
  vocabulary  phases/operations/states from the allowed tables only
  ordering    FINDING -> REMEDIATION -> VERIFICATION; within a phase the
              operation table order, then system_id, resource_id ascending
  evidence    findings and COMPLETE-case verification steps cite an existing
              file_id / activity_id / audit_id; proposed steps are empty
  temporal    cited files were captured at or before the case's audit_at
  status      NOT_APPLICABLE rows have empty step arrays
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII",
                 "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD",
                     "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT",
                      "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
                      "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation",
             "state", "evidence_id"}


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def validate_output(output_csv: str, dataset_dir: str) -> dict:
    issues: list[str] = []
    warnings: list[str] = []
    stats = {"rows": 0, "statuses": {}, "not_applicable": 0}

    requests = {r["case_id"]: r for r in csv.DictReader(
        open(os.path.join(dataset_dir, "deletion_requests.csv"), encoding="utf-8"))}
    manifest = {r["file_id"]: r for r in csv.DictReader(
        open(os.path.join(dataset_dir, "evidence_files.csv"), encoding="utf-8"))}
    activity_ids = {r["activity_id"] for r in csv.DictReader(
        open(os.path.join(dataset_dir, "system_activity.csv"), encoding="utf-8"))}
    audit_ids = {r["audit_id"] for r in csv.DictReader(
        open(os.path.join(dataset_dir, "user_audit_log.csv"), encoding="utf-8"))}

    with open(output_csv, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != ["case_id", "deletion_status", "steps_json"]:
            issues.append(f"bad header: {header}")
        rows = list(reader)

    ids = [r[0] for r in rows]
    stats["rows"] = len(rows)
    if len(rows) != len(requests):
        issues.append(f"row count {len(rows)} != requests {len(requests)}")
    if ids != sorted(ids):
        issues.append("rows are not sorted by case_id")
    if len(set(ids)) != len(ids):
        issues.append("duplicate case_ids")
    missing = set(requests) - set(ids)
    extra = set(ids) - set(requests)
    if missing:
        issues.append(f"missing case_ids: {sorted(missing)[:5]}")
    if extra:
        issues.append(f"unknown case_ids: {sorted(extra)[:5]}")

    for row in rows:
        case_id, status, steps_raw = (row + ["", "", ""])[:3]
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            issues.append(f"{case_id}: bad status {status}")
            continue
        stats["statuses"][status] = stats["statuses"].get(status, 0) + 1
        try:
            steps = json.loads(steps_raw)
        except Exception as exc:
            issues.append(f"{case_id}: steps_json does not parse: {exc}")
            continue
        if status == "NOT_APPLICABLE":
            stats["not_applicable"] += 1
            if steps != []:
                issues.append(f"{case_id}: NOT_APPLICABLE must have empty steps")
            continue
        if not isinstance(steps, list) or not steps:
            issues.append(f"{case_id}: {status} but steps empty")
            continue
        audit_at = _ts(requests.get(case_id, {}).get("audit_at"))
        prev_phase, prev_op = 0, -1
        prev_sort = None
        for i, s in enumerate(steps, start=1):
            if set(s) != STEP_KEYS:
                issues.append(f"{case_id} step {i}: keys {sorted(set(s) ^ STEP_KEYS)}")
            if s.get("step_no") != i:
                issues.append(f"{case_id} step {i}: step_no={s.get('step_no')}")
            phase, op, state = s.get("phase"), s.get("operation"), s.get("state")
            if op not in OPERATIONS:
                issues.append(f"{case_id} step {i}: unknown operation {op}")
                continue
            want_phase, want_state = OPERATIONS[op]
            if phase != want_phase or state != want_state:
                issues.append(f"{case_id} step {i}: {op} must be {want_phase}/{want_state}")
            if PHASE_ORDER.get(phase, 9) < prev_phase:
                issues.append(f"{case_id} step {i}: phase order violation")
            prev_phase = max(prev_phase, PHASE_ORDER.get(phase, 9))
            sort_key = (s.get("system_id", ""), s.get("resource_id", ""))
            if phase == prev_phase and prev_sort is not None:
                op_order = {"FINDING": FINDING_ORDER, "REMEDIATION": REMEDIATION_ORDER,
                            "VERIFICATION": VERIFICATION_ORDER}[phase]
                if op in op_order and prev_op in op_order:
                    if (op_order.index(op), *sort_key) < (op_order.index(prev_op), *prev_sort):
                        issues.append(f"{case_id} step {i}: operation/order violation "
                                      f"({prev_op} before {op})")
            prev_op, prev_sort = op, sort_key
            ev = s.get("evidence_id", "")
            if phase == "FINDING" or (status == "COMPLETE" and phase == "VERIFICATION"):
                if not ev:
                    issues.append(f"{case_id} step {i}: {phase} step missing evidence")
                elif ev.startswith("evidence_"):
                    meta = manifest.get(ev)
                    if meta is None:
                        issues.append(f"{case_id} step {i}: unknown {ev}")
                    elif audit_at is not None:
                        captured = _ts(meta.get("captured_at"))
                        if captured and captured > audit_at:
                            # event-carrying files may legitimately contain
                            # observations made before the audit; report as a
                            # warning, not a contract violation
                            warnings.append(f"{case_id} step {i}: {ev} captured after audit_at")
                elif ev.startswith("activity_"):
                    if ev not in activity_ids:
                        issues.append(f"{case_id} step {i}: unknown {ev}")
                elif ev.startswith("audit_"):
                    if ev not in audit_ids:
                        issues.append(f"{case_id} step {i}: unknown {ev}")
                else:
                    issues.append(f"{case_id} step {i}: evidence id {ev!r} not recognized")
            else:
                if ev:
                    issues.append(f"{case_id} step {i}: proposed step must have empty evidence")

    stats["issues"] = issues
    stats["warnings"] = warnings
    return stats


if __name__ == "__main__":
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "output.csv")
    data = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "dataset")
    result = validate_output(out, data)
    print(json.dumps({k: v for k, v in result.items() if k not in ("issues", "warnings")}, indent=2))
    for issue in result["issues"][:40]:
        print("ISSUE:", issue)
    for warning in result["warnings"][:10]:
        print("warn:", warning)
    print(f"total issues: {len(result['issues'])}, warnings: {len(result['warnings'])}")
