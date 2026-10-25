"""system_activity.csv access: catalog-link facts and workflow event rows.

LINK_RESOURCE_IDENTITY rows are data-catalog statements that tie a resource to
a person; they are participant-visible activity facts (cited by activity_id)
for cases whose evidence corpus contains no file about them.

The deletion worker's claimed actions (DELETE_RECORD / REDACT_RECORD / ...) are
used by the planner to recover resource ids the file evidence never names.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime

from .facts import Fact, identifier_tokens

_LINK_STATUS = {
    "export_service": ("USER_EXPORT", "PRESENT"),
    "search_service": ("SEARCH_INDEX_ENTRY", "PRESENT"),
    "integration_service": ("DATA_FLOW", "ACTIVE"),
    "analytics_platform": ("ANALYTICS_PROFILE", "IDENTIFIABLE"),
    "support_platform": ("SUPPORT_MESSAGE", "IDENTIFIABLE"),
    "backup_service": ("BACKUP_ARCHIVE", "ACTIVE"),
    "security_service": ("SECURITY_AUDIT_LOG", "PRESENT"),
    "object_storage": ("SUPPORT_ATTACHMENT", "PRESENT"),
    "marketing_platform": ("MARKETING_PROFILE", "PRESENT"),
    "identity_service": ("ACCOUNT_TOMBSTONE", "PRESENT"),
}


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass
class ActivityRow:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str

    def __post_init__(self):
        self.dt = parse_ts(self.event_time)


class ActivityIndex:
    def __init__(self, activity_csv: str):
        self.rows: list[ActivityRow] = []
        with open(activity_csv, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                self.rows.append(ActivityRow(
                    row["activity_id"], row["event_time"], row["event_type"],
                    row["source_system"], row["target_system"],
                    row["resource_id"], row["reported_result"], row["details"]))
        self.by_resource: dict[str, list[ActivityRow]] = {}
        for r in self.rows:
            self.by_resource.setdefault(r.resource_id, []).append(r)

    # ---- catalog link facts ------------------------------------------------

    def link_facts(self, ident, audit_at: str, freshness_days: int = 14) -> list[Fact]:
        import re as _re
        toks = ident.tokens()
        cutoff = parse_ts(audit_at)
        out = []
        for r in self.rows:
            if r.event_type != "LINK_RESOURCE_IDENTITY" or r.dt is None or r.dt > cutoff:
                continue
            # catalog links are readback statements: they prove state only
            # near the moment they were recorded
            if (cutoff - r.dt).days > freshness_days:
                continue
            row_tokens = set()
            for m in _re.findall(r"[\w.+-]+@[\w.-]+", r.details):
                row_tokens |= identifier_tokens(m)
            for m in _re.findall(r"\buser_\d+\b", r.details):
                row_tokens |= identifier_tokens(m)
            if not (row_tokens & toks):
                continue
            spec = _LINK_STATUS.get(r.target_system)
            if spec is None:
                continue
            rtype, status = spec
            out.append(Fact(system_id=r.target_system, resource_id=r.resource_id,
                            record_type=rtype, status=status,
                            subjects=frozenset(row_tokens & toks),
                            observed_at=r.event_time, file_id=r.activity_id,
                            detail="activity catalog link"))
        return out

    # ---- workflow events -----------------------------------------------------

    def worker_events(self):
        for r in self.rows:
            if r.source_system == "deletion_worker":
                yield r

    def events_for_resource(self, resource_id: str):
        return self.by_resource.get(resource_id, [])
