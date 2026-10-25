"""End-to-end tests: run the pipeline on the 32 sample requests and assert the
output contract plus the accuracy floor. Run with: python -m pytest code/tests
(or python code/tests/test_pipeline.py).
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE_DIR)
sys.path.insert(0, CODE_DIR)

from src.validate import validate_output  # noqa: E402


def _materialize_sample_dataset(workdir: str) -> str:
    os.makedirs(workdir, exist_ok=True)
    for f in ("user_identities.csv", "user_audit_log.csv", "system_activity.csv",
              "deletion_rules.csv", "evidence_files.csv"):
        os.link(os.path.join(ROOT, "dataset", f), os.path.join(workdir, f)) if False else \
            open(os.path.join(workdir, f), "wb").write(
                open(os.path.join(ROOT, "dataset", f), "rb").read())
    rows = list(csv.DictReader(open(os.path.join(ROOT, "dataset", "sample_deletion_requests.csv"),
                                    encoding="utf-8")))
    with open(os.path.join(workdir, "deletion_requests.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    ev_dst = os.path.join(workdir, "evidence")
    if not os.path.exists(ev_dst):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "dataset", "evidence")):
            rel = os.path.relpath(dirpath, os.path.join(ROOT, "dataset"))
            target = os.path.join(workdir, rel)
            os.makedirs(target, exist_ok=True)
            for name in files:
                open(os.path.join(target, name), "wb").write(
                    open(os.path.join(dirpath, name), "rb").read())
    return workdir


def test_sample_run_contract_and_accuracy():
    import main as agent
    with tempfile.TemporaryDirectory() as workdir:
        _materialize_sample_dataset(workdir)
        out = os.path.join(workdir, "output.csv")
        agent.run(workdir, out)
        rows = list(csv.DictReader(open(out, encoding="utf-8")))
        assert len(rows) == 32
        assert [r["case_id"] for r in rows] == sorted(r["case_id"] for r in rows)
        truth = {r["case_id"]: r for r in csv.DictReader(
            open(os.path.join(ROOT, "dataset", "sample_output.csv"), encoding="utf-8"))}
        status_ok = 0
        for r in rows:
            steps = json.loads(r["steps_json"])
            assert r["deletion_status"] in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
            if r["deletion_status"] == "NOT_APPLICABLE":
                assert steps == []
            for i, s in enumerate(steps, 1):
                assert s["step_no"] == i
                assert set(s) == {"step_no", "phase", "system_id", "resource_id",
                                  "operation", "state", "evidence_id"}
            status_ok += int(r["deletion_status"] == truth[r["case_id"]]["deletion_status"])
        # the pipeline reproduces every sample deletion-status decision
        assert status_ok == 32, f"status accuracy {status_ok}/32"


if __name__ == "__main__":
    test_sample_run_contract_and_accuracy()
    print("test_sample_run_contract_and_accuracy: PASS")
