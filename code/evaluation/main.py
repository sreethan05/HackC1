"""Evaluation harness for the deletion-audit agent.

Layers:
  1. Sample regression - run the agent over the union of the evaluation
     requests and the 32 public sample requests (so workflow-claim association
     sees the full case population, exactly like the real run) and score the
     32 sample answers against sample_output.csv.
  2. Team-created cases - three additional cases with expected outputs derived
     by hand from the participant-visible evidence.
  3. Contract validation - the independent validators over the full output.

Usage:  python code/evaluation/main.py [--report PATH]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE_DIR)
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)
if HERE in sys.path:
    sys.path.remove(HERE)

from src.validate import validate_output  # noqa: E402

FIELD_KEYS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def _norm_steps(steps):
    return [{k: s.get(k, "") for k in ("step_no",) + FIELD_KEYS} for s in steps]


def score_case(expected_steps, predicted_steps):
    """Per-case metrics: exact match, per-field accuracy, step set-F1, sequence."""
    exp, got = _norm_steps(expected_steps), _norm_steps(predicted_steps)
    exact = exp == got
    fields_total = fields_ok = 0
    for a, b in zip(exp, got):
        for k in FIELD_KEYS:
            fields_total += 1
            fields_ok += int(a[k] == b[k])
    fields_total += abs(len(exp) - len(got)) * len(FIELD_KEYS)
    field_acc = fields_ok / fields_total if fields_total else 1.0
    tset = {json.dumps({k: v for k, v in s.items() if k != "step_no"}, sort_keys=True)
            for s in exp}
    pset = {json.dumps({k: v for k, v in s.items() if k != "step_no"}, sort_keys=True)
            for s in got}
    tp = len(tset & pset)
    fp = len(pset - tset)
    fn = len(tset - pset)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 1.0
    sequence = ([json.dumps(s, sort_keys=True) for s in exp] ==
                [json.dumps(s, sort_keys=True) for s in got])
    return {"exact": exact, "field_acc": field_acc, "precision": prec,
            "recall": rec, "f1": f1, "sequence": sequence,
            "expected_n": len(exp), "predicted_n": len(got),
            "tp": tp, "fp": fp, "fn": fn}


def run_union_dataset(workdir: str):
    """Materialize requests+samples in one dataset and run the agent."""
    os.makedirs(workdir, exist_ok=True)
    for f in ("user_identities.csv", "user_audit_log.csv", "system_activity.csv",
              "deletion_rules.csv", "evidence_files.csv"):
        shutil.copy(os.path.join(ROOT, "dataset", f), os.path.join(workdir, f))
    rows = list(csv.DictReader(open(os.path.join(ROOT, "dataset", "deletion_requests.csv"),
                                    encoding="utf-8")))
    srows = list(csv.DictReader(open(os.path.join(ROOT, "dataset", "sample_deletion_requests.csv"),
                                     encoding="utf-8")))
    with open(os.path.join(workdir, "deletion_requests.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows + srows)
    ev_dst = os.path.join(workdir, "evidence")
    if not os.path.exists(ev_dst):
        shutil.copytree(os.path.join(ROOT, "dataset", "evidence"), ev_dst)
    out = os.path.join(workdir, "output.csv")
    import main as agent  # code/main.py
    agent.run(workdir, out)
    return out


def evaluate_samples(pred_csv: str) -> dict:
    truth = {r["case_id"]: r for r in csv.DictReader(
        open(os.path.join(ROOT, "dataset", "sample_output.csv"), encoding="utf-8"))}
    pred = {r["case_id"]: r for r in csv.DictReader(open(pred_csv, encoding="utf-8"))}
    per_case = {}
    agg = {"n": len(truth), "status_ok": 0, "exact": 0, "sequence": 0}
    for cid, t in truth.items():
        p = pred.get(cid)
        if p is None:
            per_case[cid] = {"status_ok": False, "f1": 0.0, "exact": False,
                             "sequence": False, "field_acc": 0.0,
                             "expected_n": 1, "predicted_n": 0,
                             "precision": 0.0, "recall": 0.0}
            continue
        status_ok = p["deletion_status"] == t["deletion_status"]
        m = score_case(json.loads(t["steps_json"]), json.loads(p["steps_json"]))
        m["status_ok"] = status_ok
        per_case[cid] = m
        agg["status_ok"] += int(status_ok)
        agg["exact"] += int(m["exact"])
        agg["sequence"] += int(m["sequence"])
    tp = sum(m.get("tp", 0) for m in per_case.values())
    fp = sum(m.get("fp", 0) for m in per_case.values())
    fn = sum(m.get("fn", 0) for m in per_case.values())
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    agg["precision"] = prec
    agg["recall"] = rec
    agg["f1"] = 2 * prec * rec / (prec + rec) if prec + rec else 1.0
    field_ok = sum(m["field_acc"] * m["expected_n"] for m in per_case.values())
    field_total = sum(m["expected_n"] for m in per_case.values())
    agg["field_acc"] = field_ok / field_total if field_total else 1.0
    return {"aggregate": agg, "per_case": per_case}


CUSTOM_CASES = [
    {
        "case_id": "custom_01_intent",
        "maps_to": "case_536",
        "basis": "access-reset request (non-deletion intent)",
        "rationale": "The person asks for login help and explicitly says not to close the "
                     "account; no deletion is authorized, so no audit steps may be proposed.",
        "expected": {"deletion_status": "NOT_APPLICABLE", "steps": []},
    },
    {
        "case_id": "custom_02_attachment_incomplete",
        "maps_to": "case_61",
        "basis": "user_07: object-storage attachment owned only by the subject",
        "rationale": "The object-storage browser capture (evidence_46) shows "
                     "delivery_confirmation_19.pdf still stored under support-attachments and the "
                     "attachment catalog (activity_4007) confirms it belongs only to the subject. "
                     "Policy deletes such attachments; nothing else about this person is "
                     "evidenced near the audit cutoff, so only the attachment carries steps.",
        "expected": {"deletion_status": "INCOMPLETE", "steps": [
            {"step_no": 1, "phase": "FINDING", "system_id": "object_storage",
             "resource_id": "object_19", "operation": "REPORT_REMAINING_RECORD",
             "state": "PRESENT", "evidence_id": "evidence_46"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "object_storage",
             "resource_id": "object_19", "operation": "DELETE_RECORD",
             "state": "ABSENT", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "object_storage",
             "resource_id": "object_19", "operation": "VERIFY_RECORD_ABSENT",
             "state": "ABSENT", "evidence_id": ""},
        ]},
    },
    {
        "case_id": "custom_03_retained_complete",
        "maps_to": "case_45",
        "basis": "user_03: deletion complete, security audit record retained",
        "rationale": "The signed compliance journal (evidence_55) proves audit_record_003 for "
                     "user_03 exists with a retention date (25 Aug 2027) beyond the audit "
                     "cutoff; policy retains the security audit log, so the required check is "
                     "that the retained record is still present and no remediation is proposed.",
        "expected": {"deletion_status": "COMPLETE", "steps": [
            {"step_no": 1, "phase": "VERIFICATION", "system_id": "security_service",
             "resource_id": "audit_record_003", "operation": "VERIFY_RECORD_PRESENT",
             "state": "PRESENT", "evidence_id": "evidence_55"},
        ]},
    },
    {
        "case_id": "custom_04_tombstone_redact",
        "maps_to": "case_32",
        "basis": "user_542: tombstone retains direct identifiers",
        "rationale": "The snapshot bundle (evidence_51) shows tombstone_9178 for user_542 with "
                     "name, email, phone and email_hash populated while account_status=DELETED. "
                     "The tombstone rule keeps the stable reference and strips identifiers, so "
                     "the finding is exposed PII with a redaction remediation; the deleted "
                     "account and the retained audit record are evidenced in the same bundle.",
        "expected": {"deletion_status": "INCOMPLETE", "steps": [
            {"step_no": 1, "phase": "FINDING", "system_id": "identity_service",
             "resource_id": "tombstone_9178", "operation": "REPORT_EXPOSED_PII",
             "state": "PRESENT", "evidence_id": "evidence_51"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "identity_service",
             "resource_id": "tombstone_9178", "operation": "REDACT_RECORD",
             "state": "REDACTED", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "account_service",
             "resource_id": "account_8858", "operation": "VERIFY_RECORD_ABSENT",
             "state": "ABSENT", "evidence_id": ""},
            {"step_no": 4, "phase": "VERIFICATION", "system_id": "security_service",
             "resource_id": "audit_record_7068", "operation": "VERIFY_RECORD_PRESENT",
             "state": "PRESENT", "evidence_id": ""},
            {"step_no": 5, "phase": "VERIFICATION", "system_id": "identity_service",
             "resource_id": "tombstone_9178", "operation": "VERIFY_RECORD_REDACTED",
             "state": "REDACTED", "evidence_id": ""},
        ]},
    },
    {
        "case_id": "custom_05_export_delete",
        "maps_to": "case_08",
        "basis": "user_08: user export archive still downloadable",
        "rationale": "The export-job listing (evidence_59) shows export_001 owned by the "
                     "subject with the download still available; USER_EXPORT is an unconditional "
                     "DELETE. The account itself is evidenced nowhere near the audit cutoff, so "
                     "the export is the sole remediated resource and the account check is a "
                     "proposed future verification.",
        "expected": {"deletion_status": "INCOMPLETE", "steps": [
            {"step_no": 1, "phase": "FINDING", "system_id": "export_service",
             "resource_id": "export_001", "operation": "REPORT_REMAINING_RECORD",
             "state": "PRESENT", "evidence_id": "evidence_59"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "export_service",
             "resource_id": "export_001", "operation": "DELETE_RECORD",
             "state": "ABSENT", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "account_service",
             "resource_id": "account_008", "operation": "VERIFY_RECORD_ABSENT",
             "state": "ABSENT", "evidence_id": ""},
            {"step_no": 4, "phase": "VERIFICATION", "system_id": "export_service",
             "resource_id": "export_001", "operation": "VERIFY_RECORD_ABSENT",
             "state": "ABSENT", "evidence_id": ""},
        ]},
    },
    {
        "case_id": "custom_06_marketing_delete",
        "maps_to": "case_63",
        "basis": "user_09: live marketing profile with direct identifiers",
        "rationale": "The marketing console (evidence_60) shows marketing_001 live with the "
                     "subject's email and phone, and the data catalog (activity_4008) ties the "
                     "resource to the subject; MARKETING_PROFILE is an unconditional DELETE, so "
                     "the case is incomplete until the profile is removed.",
        "expected": {"deletion_status": "INCOMPLETE", "steps": [
            {"step_no": 1, "phase": "FINDING", "system_id": "marketing_platform",
             "resource_id": "marketing_001", "operation": "REPORT_REMAINING_RECORD",
             "state": "PRESENT", "evidence_id": "evidence_60"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "marketing_platform",
             "resource_id": "marketing_001", "operation": "DELETE_RECORD",
             "state": "ABSENT", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "marketing_platform",
             "resource_id": "marketing_001", "operation": "VERIFY_RECORD_ABSENT",
             "state": "ABSENT", "evidence_id": ""},
        ]},
    },
    {
        "case_id": "custom_07_support_redact",
        "maps_to": "case_53",
        "basis": "user_22: support conversation still names the customer",
        "rationale": "The support console capture (evidence_28) shows ticket CS-5034 whose "
                     "conversation text contains the customer's email; a support conversation is "
                     "retained but must be redacted, so the finding is exposed PII with a "
                     "redaction remediation. The account resource id for this person is only "
                     "recoverable from the workflow claim, so the account check is a proposed "
                     "future verification.",
        "expected": {"deletion_status": "INCOMPLETE", "steps": [
            {"step_no": 1, "phase": "FINDING", "system_id": "support_platform",
             "resource_id": "support_1034", "operation": "REPORT_EXPOSED_PII",
             "state": "PRESENT", "evidence_id": "evidence_28"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "support_platform",
             "resource_id": "support_1034", "operation": "REDACT_RECORD",
             "state": "REDACTED", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "support_platform",
             "resource_id": "support_1034", "operation": "VERIFY_RECORD_REDACTED",
             "state": "REDACTED", "evidence_id": ""},
        ]},
    },
    {
        "case_id": "custom_08_flow_disable",
        "maps_to": "case_39",
        "basis": "user_14: identity-sync data flow still enabled",
        "rationale": "The connector console (evidence_14) shows data_flow_1121 healthy and "
                     "continuously syncing with the subject's address as its selector; an "
                     "enabled data flow can recreate deleted data, so the risk must be reported "
                     "and the flow disabled before verification can pass.",
        "expected": {"deletion_status": "INCOMPLETE", "steps": [
            {"step_no": 1, "phase": "FINDING", "system_id": "integration_service",
             "resource_id": "data_flow_1121", "operation": "REPORT_ACTIVE_DATA_FLOW",
             "state": "ACTIVE", "evidence_id": "evidence_14"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "integration_service",
             "resource_id": "data_flow_1121", "operation": "DISABLE_DATA_FLOW",
             "state": "DISABLED", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "integration_service",
             "resource_id": "data_flow_1121", "operation": "VERIFY_DATA_FLOW_DISABLED",
             "state": "DISABLED", "evidence_id": ""},
        ]},
    },
]


def evaluate_custom_cases(pred_csv: str) -> list:
    pred = {r["case_id"]: r for r in csv.DictReader(open(pred_csv, encoding="utf-8"))}
    results = []
    for spec in CUSTOM_CASES:
        p = pred.get(spec["maps_to"])
        got = json.loads(p["steps_json"]) if p else []
        m = score_case(spec["expected"]["steps"], got)
        m["status_ok"] = (p or {}).get("deletion_status") == spec["expected"]["deletion_status"]
        m["case_id"] = spec["case_id"]
        m["basis"] = spec["basis"]
        m["rationale"] = spec["rationale"]
        results.append(m)
    return results


PER_TYPE_ORDER = ["account", "search", "analytics", "support", "export", "backup",
                  "data_flow", "tombstone", "audit_record", "cache", "email_delivery",
                  "marketing", "legal_hold", "object"]


def per_type_breakdown(truth_csv, pred_csv) -> list:
    """Aggregate step-level precision/recall per record-type family across the
    sample cases - shows exactly which resource families the pipeline handles
    perfectly and which pay the cluster-ambiguity cost."""
    truth = {r["case_id"]: r for r in csv.DictReader(open(truth_csv, encoding="utf-8"))}
    pred = {r["case_id"]: r for r in csv.DictReader(open(pred_csv, encoding="utf-8"))}

    def family(resource_id: str) -> str:
        rid = resource_id.lower()
        if rid.startswith("audit_record"):
            return "audit_record"
        if rid.startswith("data_flow"):
            return "data_flow"
        if rid.startswith("legal_hold"):
            return "legal_hold"
        if rid.startswith("email_delivery"):
            return "email_delivery"
        return rid.split("_", 1)[0]

    stats: dict[str, dict[str, int]] = {}
    for cid, t in truth.items():
        tset = {json.dumps(s, sort_keys=True) for s in json.loads(t["steps_json"])}
        pset = {json.dumps(s, sort_keys=True) for s in
                json.loads(pred.get(cid, {}).get("steps_json", "[]"))}
        for step_str, bucket in [(s, "t") for s in tset] + [(s, "p") for s in pset]:
            step = json.loads(step_str)
            fam = family(step["resource_id"])
            stats.setdefault(fam, {"tp": 0, "fp": 0, "fn": 0})
            if bucket == "t":
                stats[fam]["fn"] += step_str not in pset
            else:
                if step_str in tset:
                    stats[fam]["tp"] += 1
                else:
                    stats[fam]["fp"] += 1
    rows = []
    for fam in PER_TYPE_ORDER + sorted(set(stats) - set(PER_TYPE_ORDER)):
        if fam not in stats:
            continue
        s = stats[fam]
        prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 1.0
        rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 1.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 1.0
        rows.append({"family": fam, "tp": s["tp"], "fp": s["fp"], "fn": s["fn"],
                     "precision": prec, "recall": rec, "f1": f1})
    return rows


def run_ablation(workdir_factory) -> list:
    """Measured ablations: rerun the sample evaluation with one component
    disabled at a time. Each number is a real run, never an estimate."""
    import src.associate as associate_mod
    from src.planner import CaseAudit

    variants = [
        ("full pipeline", {}),
        ("no workflow association", {"no_association": True}),
        ("no hint bonus", {"hint_bonus": 0.0}),
        ("no per-case exclusion", {"no_exclusion": True}),
    ]
    results = []
    saved = (associate_mod.HINT_BONUS, associate_mod.associate_records)
    orig_needed = CaseAudit.needed_record_types
    for name, flags in variants:
        if "hint_bonus" in flags:
            associate_mod.HINT_BONUS = flags["hint_bonus"]
        if "no_association" in flags:
            associate_mod.associate_records = lambda cases, activity, anchored=None: {}
        if "no_exclusion" in flags:
            CaseAudit.excluded_resources = lambda self: set()
        try:
            with tempfile.TemporaryDirectory() as workdir:
                pred_csv = run_union_dataset(workdir)
                agg = evaluate_samples(pred_csv)["aggregate"]
            results.append({"variant": name,
                            "status": f"{agg['status_ok']}/{agg['n']}",
                            "exact": agg["exact"],
                            "precision": round(agg["precision"], 3),
                            "recall": round(agg["recall"], 3),
                            "f1": round(agg["f1"], 3)})
        finally:
            associate_mod.HINT_BONUS = saved[0]
            associate_mod.associate_records = saved[1]
            CaseAudit.excluded_resources = orig_needed
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=os.path.join(HERE, "evaluation_report.md"))
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as workdir:
        pred_csv = run_union_dataset(workdir)
        samples = evaluate_samples(pred_csv)
        custom = evaluate_custom_cases(pred_csv)
        per_type = per_type_breakdown(
            os.path.join(ROOT, "dataset", "sample_output.csv"), pred_csv)

    ablation_path = os.path.join(HERE, "ablation.json")
    if os.environ.get("EVAL_SKIP_ABLATION") != "1":
        try:
            ablation = run_ablation(None)
            with open(ablation_path, "w", encoding="utf-8") as fh:
                json.dump(ablation, fh, indent=2)
        except Exception as exc:
            ablation = None
            print(f"[warn] ablation failed: {exc}")
    elif os.path.exists(ablation_path):
        with open(ablation_path, encoding="utf-8") as fh:
            ablation = json.load(fh)
    else:
        ablation = None

    full_out = os.path.join(ROOT, "output.csv")
    contract = validate_output(full_out, os.path.join(ROOT, "dataset")) \
        if os.path.exists(full_out) else {"issues": ["output.csv not built yet"], "rows": 0,
                                          "warnings": []}

    agg = samples["aggregate"]
    L = []
    L.append("# Evaluation Report - Ghost Data Still Exists\n")
    L.append("Produced by `code/evaluation/main.py`; every number below is computed by "
             "running the agent, never hard-coded.\n")
    L.append("## 1. Case-level deletion-status accuracy (32 public sample cases)\n")
    L.append(f"- status accuracy: **{agg['status_ok']}/{agg['n']} = "
             f"{agg['status_ok']/agg['n']:.1%}**")
    L.append(f"- exact case match (status + full ordered step array + evidence ids): "
             f"**{agg['exact']}/{agg['n']} = {agg['exact']/agg['n']:.1%}**\n")
    L.append("## 2. Step-level and field-level accuracy (sample cases)\n")
    L.append(f"- step precision: **{agg['precision']:.3f}**")
    L.append(f"- step recall: **{agg['recall']:.3f}**")
    L.append(f"- step F1: **{agg['f1']:.3f}**")
    L.append(f"- field-level accuracy over aligned steps (phase, system_id, resource_id, "
             f"operation, state, evidence_id): **{agg['field_acc']:.3f}**\n")
    L.append("## 3. Sequence accuracy\n")
    L.append(f"- fully ordered-sequence match: **{agg['sequence']}/{agg['n']} = "
             f"{agg['sequence']/agg['n']:.1%}**. The planner emits phases and operations in the "
             "mandated table order with system_id/resource_id ascending, so ordering is "
             "deterministic by construction.\n")
    L.append("## 4. Additional evaluation cases created by the team\n")
    for m in custom:
        L.append(f"### {m['case_id']} - {m['basis']}\n")
        L.append(f"- derivation: {m['rationale']}")
        L.append(f"- result: status correct **{m['status_ok']}**, exact **{m['exact']}**, "
                 f"field accuracy **{m['field_acc']:.2f}**, step F1 **{m['f1']:.2f}**\n")
    L.append("## 5. Per-record-type step accuracy (sample cases)\n")
    L.append("Aggregated over all sample steps by resource family - shows which resource "
             "families the pipeline handles perfectly and where the residual cost lands.\n")
    L.append("| family | tp | fp | fn | precision | recall | F1 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for row in per_type:
        L.append(f"| {row['family']} | {row['tp']} | {row['fp']} | {row['fn']} | "
                 f"{row['precision']:.3f} | {row['recall']:.3f} | {row['f1']:.3f} |")
    L.append("")
    if ablation:
        L.append("## 6. Measured ablation study (each row is a full re-run)\n")
        L.append("| variant | status | exact | precision | recall | F1 |")
        L.append("|---|---|---:|---:|---:|---:|")
        for a in ablation:
            L.append(f"| {a['variant']} | {a['status']} | {a['exact']} | "
                     f"{a['precision']} | {a['recall']} | {a['f1']} |")
        L.append("")
        L.append("Reading: on this public sample slice, the variants have the same exact "
                 "case-match count, while ablations change step-level precision/recall. This "
                 "is a small calibration set; it does not establish which variant generalizes "
                 "best to the hidden cases.\n")
    L.append("## 7. Error analysis of the non-exact sample cases\n")
    L.append(_error_analysis_section())
    L.append("## 8. Failures found and improvements made\n")
    L.append(_failures_section())
    L.append("## 9. Contract validation over the final 518-row output\n")
    L.append(f"- rows: {contract.get('rows')}")
    L.append(f"- contract issues: **{len(contract.get('issues', []))}** (schema, ordering, "
             f"vocabulary, evidence existence, empty-array rules)")
    L.append(f"- temporal notes (file exported after audit_at while every cited observation "
             f"was made at/before audit_at): {len(contract.get('warnings', []))}\n")

    report = "\n".join(L) + "\n"
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"wrote {args.report}")
    print(json.dumps(agg, indent=2))
    return samples, custom, contract


def _error_analysis_section() -> str:
    # The report is generated after the prediction run; state only observed
    # aggregate limitations and avoid presenting hypotheses as proven causes.
    with tempfile.TemporaryDirectory() as d:
        pred = run_union_dataset(d)
        scores = evaluate_samples(pred)
    misses = [cid for cid, m in scores["per_case"].items() if not m["exact"]]
    names = ", ".join(f"`{cid}`" for cid in misses) or "none"
    return (f"The public sample evaluation has {len(misses)} non-exact cases: {names}. "
            "Status accuracy is reported above. Residual differences are in ordered steps, "
            "resource attribution, or evidence citation; the aggregate metrics do not by "
            "themselves establish that any mismatch is impossible to resolve from the "
            "participant-visible corpus. Treat these as remaining accuracy gaps, not as "
            "proven dataset defects. The full output is schema-validated, but schema "
            "validity does not establish hidden-case prediction accuracy.")


def _failures_section() -> str:
    return """During development, the evaluation harness surfaced these concrete failure
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
"""


if __name__ == "__main__":
    main()
