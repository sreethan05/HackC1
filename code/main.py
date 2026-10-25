"""Ghost Data Still Exists - deletion-audit agent entry point.

Reads the participant-facing dataset (CSV tables + evidence files), resolves
each requester's current and historical identity, reconstructs the state of
every relevant resource at the case's audit_at cutoff from the supplied
evidence, applies deletion_rules.csv, and writes output.csv with the ordered,
evidence-cited audit steps.

Usage:
    python code/main.py [--dataset PATH] [--out PATH]

Defaults: dataset next to the repository root, output written to the
repository root as output.csv.
"""
from __future__ import annotations

import argparse
import csv
from functools import lru_cache
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.activity import ActivityIndex, parse_ts
from src.associate import ASSOCIATED_STATUS, associate_records
from src import parse_json
from src.evidence import EvidenceIndex
from src.facts import Fact
from src.identity import IdentityIndex
from src.planner import CaseAudit
from src.policy import Policy


def _system_for_type(record_type: str) -> str:
    return {
        "USER_PROFILE": "account_service",
        "SEARCH_INDEX_ENTRY": "search_service",
        "CACHE_ENTRY": "cache_service",
        "USER_EXPORT": "export_service",
        "SUPPORT_ATTACHMENT": "support_platform",
        "SUPPORT_MESSAGE": "support_platform",
        "MARKETING_PROFILE": "marketing_platform",
        "ANALYTICS_PROFILE": "analytics_platform",
        "ACCOUNT_TOMBSTONE": "identity_service",
        "DATA_FLOW": "integration_service",
        "BACKUP_ARCHIVE": "backup_service",
        "SECURITY_AUDIT_LOG": "security_service",
        "LEGAL_HOLD_RECORD": "legal_service",
        "EMAIL_DELIVERY_LOG": "email_delivery_service",
        "PRODUCT_TELEMETRY": "telemetry_service",
    }.get(record_type, "unknown")


def find_repo_root(start: str) -> str:
    cur = start
    for _ in range(4):
        if os.path.exists(os.path.join(cur, "dataset", "deletion_requests.csv")):
            return cur
        cur = os.path.dirname(cur)
    return start


def load_requests(dataset_dir):
    path = os.path.join(dataset_dir, "deletion_requests.csv")
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@lru_cache(maxsize=None)
def _has_empty_snapshot(path: str, system_id: str) -> bool:
    """Detect an explicit empty collection in a JSON snapshot bundle."""
    if not path.lower().endswith(".json"):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return False
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if (str(item.get("data_store", "")).lower() == system_id.lower()
                    and item.get("records") == []):
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def account_absence_fact(audit: CaseAudit, system_id: str, record_type: str,
                         resource_id: str):
    """Cite the strongest participant-visible proof that the record is gone:
    the latest snapshot file at or before audit_at that covers this record
    type but does not contain the resource."""
    scopes = getattr(parse_json, "SNAPSHOT_SCOPES", {})
    best = None
    best_present = None
    fact_groups = getattr(audit.evidence, "_facts_by_file_cache", None)
    if fact_groups is None:
        fact_groups = {}
        for fact in audit.evidence.facts:
            fact_groups.setdefault(fact.file_id, []).append(fact)
        audit.evidence._facts_by_file_cache = fact_groups
    identity_files = {f.file_id for f in audit.raw_facts
                      if f.subjects & audit.identity.tokens()}

    for file_id, meta in audit.evidence.manifest.items():
        captured = parse_ts(meta.get("captured_at") or "")
        if captured is None or captured > audit.audit_dt:
            continue
        identity_anchored = file_id in identity_files
        file_facts = [f for f in fact_groups.get(file_id, ())
                      if f.system_id == system_id and f.record_type == record_type]
        covers = (bool(file_facts) or system_id in scopes.get(file_id, set())
                  or (identity_anchored and _has_empty_snapshot(meta.get("path", ""),
                                                                 system_id)))
        if not covers:
            continue
        # a readback that explicitly answered NOT_FOUND for this resource is
        # the strongest absence proof
        absent_proof = [f for f in file_facts
                        if f.resource_id == resource_id and f.status == "ABSENT"]
        if absent_proof:
            if best is None or (meta["captured_at"], file_id) > best:
                best = (meta["captured_at"], file_id)
            continue
        if any(f.resource_id == resource_id for f in file_facts):
            continue  # the file still shows the record present
        if best_present is None or (meta["captured_at"], file_id) > best_present:
            best_present = (meta["captured_at"], file_id)
    if best is None:
        best = best_present
    if best:
        return Fact(system_id=system_id, resource_id=resource_id,
                    record_type=record_type, status="ABSENT",
                    subjects=frozenset({audit.identity.user_id.lower()}),
                    observed_at=best[0], file_id=best[1],
                    detail="absent from snapshot")
    return None


def run(dataset_dir: str, out_path: str, llm=None) -> dict:
    if llm is None:
        try:
            from src import llm as _llm
            if _llm.configured():
                llm = _llm
        except Exception:
            llm = None
    requests = load_requests(dataset_dir)
    identities = IdentityIndex(os.path.join(dataset_dir, "user_identities.csv"),
                               os.path.join(dataset_dir, "user_audit_log.csv"))
    evidence = EvidenceIndex(dataset_dir)
    activity = ActivityIndex(os.path.join(dataset_dir, "system_activity.csv"))
    policy = Policy(os.path.join(dataset_dir, "deletion_rules.csv"))

    audits: dict[str, CaseAudit] = {}
    unresolved: list[str] = []
    for case in requests:
        ident, _form = identities.resolve(case["email"])
        if ident is None:
            unresolved.append(case["case_id"])
            audits[case["case_id"]] = None
            continue
        audits[case["case_id"]] = CaseAudit(case, ident, policy, evidence, activity, llm)

    # workflow association for resources the file evidence never names
    assoc_input = {}
    for cid, audit in audits.items():
        if audit is None:
            continue
        # every case with a workflow participates in the claim matching -
        # including requests that turn out not to authorize deletion (their
        # outputs stay empty, but their claims still belong to them)
        assoc_input[cid] = {
            "needed": audit.needed_record_types(),
            "reported_dt": parse_ts(audit.case["reported_at"]),
            "reported_status": audit.case.get("reported_status", ""),
            "audit_dt": audit.audit_dt,
            "hints": audit.file_resource_hints(),
            "excluded": audit.excluded_resources(),
        }
    assignment = associate_records(assoc_input, activity)

    rows = []
    stats = {"associated": 0, "snapshot_absence": 0}
    for case in sorted(requests, key=lambda c: c["case_id"]):
        cid = case["case_id"]
        audit = audits.get(cid)
        if audit is None:
            rows.append({"case_id": cid, "deletion_status": "NOT_APPLICABLE",
                         "steps_json": "[]"})
            continue
        assoc = assignment.get(cid) or {}
        if assoc:
            for rtype, (rid, aid) in sorted(assoc.items()):
                system_id = _system_for_type(rtype)
                status0 = ASSOCIATED_STATUS.get(rtype, "ABSENT")
                fact = Fact(system_id=system_id, resource_id=rid,
                            record_type=rtype, status=status0,
                            subjects=frozenset({audit.identity.user_id.lower()}),
                            observed_at=None, file_id=aid,
                            detail="workflow-associated resource")
                # attach any evidence facts observed for this resource so
                # citations prefer file evidence over the workflow claim
                for f in audit.evidence.facts:
                    if f.resource_id == rid and f.record_type == rtype and audit._eligible(f):
                        audit.raw_facts.append(f)
                audit.raw_facts.append(fact)
            audit.facts = audit._reconcile()
            stats["associated"] += 1
        status, steps = audit.plan()
        if status == "COMPLETE" and assoc:
            # upgrade activity-cited absences to snapshot-proof citations
            changed = False
            for rtype, (rid, aid) in sorted(assoc.items()):
                if ASSOCIATED_STATUS.get(rtype) != "ABSENT":
                    continue
                pseudo = account_absence_fact(audit, _system_for_type(rtype), rtype, rid)
                if pseudo is not None:
                    audit.raw_facts = [f for f in audit.raw_facts
                                       if not (f.resource_id == rid and f.record_type == rtype
                                               and f.file_id == aid)]
                    audit.raw_facts.append(pseudo)
                    changed = True
                    stats["snapshot_absence"] += 1
            if changed:
                audit.facts = audit._reconcile()
                status, steps = audit.plan()
        rows.append({"case_id": cid, "deletion_status": status,
                     "steps_json": json.dumps(steps, separators=(",", ":"),
                                              ensure_ascii=False)})

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for row in rows:
            writer.writerow([row["case_id"], row["deletion_status"], row["steps_json"]])

    if unresolved:
        print(f"[warn] {len(unresolved)} requests did not resolve to an identity: {unresolved[:5]}")
    return {"rows": len(rows), **stats}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = find_repo_root(os.path.dirname(here))
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.path.join(root, "dataset"))
    ap.add_argument("--out", default=os.path.join(root, "output.csv"))
    args = ap.parse_args()
    stats = run(args.dataset, args.out)
    print(f"wrote {args.out}: {stats}")


if __name__ == "__main__":
    main()
