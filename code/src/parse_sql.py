"""Parsers for the SQLite-compatible SQL snapshot evidence.

Two shapes exist:
1. Normalized per-service tables (customer_profiles, customer_search_documents,
   ..., snapshot_scope) - the "inventory" dumps.
2. Generic (resource_id, origin_system, collection, payload_json, updated_at)
   rows split across a primary table and one or more replica tables - the
   "customer_data_*_replica" dumps used to demonstrate replication drift.

Drift rule: if any copy (primary or replica) still holds a live record, the
record is PRESENT in that system; the fact's subjects come from the payload.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .facts import Fact, record_type_for, identifier_tokens, normalize_phone

_PAYLOAD_STATUS_FIELDS = ("full_name", "email", "phone", "email_hash")


def _flatten_payload(p: dict) -> dict:
    """Readback payloads nest content under document/profile; lift those
    fields so identity extraction sees them."""
    for key in ("document", "profile"):
        sub = p.get(key)
        if isinstance(sub, dict):
            merged = {k: v for k, v in p.items() if k != key}
            merged.update(sub)
            return merged
    return p


def _strip(replica_name: str) -> str:
    for suffix in ("_failover_replica", "_read_replica", "_reporting_replica", "_primary"):
        if replica_name.endswith(suffix):
            return replica_name[: -len(suffix)]
    return replica_name


def _subjects(payload: dict) -> set:
    toks = set()
    for key in (
        "email", "customer_email", "owner_email", "requester_email",
        "subject_reference", "customer_reference", "user_id", "email_hash",
    ):
        toks |= identifier_tokens(payload.get(key))
    for key in ("phone", "requester_phone", "owner_phone"):
        toks |= identifier_tokens(payload.get(key))
    # free-text fields may embed direct identifiers (support conversations)
    for key in ("message_text", "message_excerpt", "details", "notes"):
        val = payload.get(key)
        if isinstance(val, str):
            import re as _re
            for m in _re.findall(r"[\w.+-]+@[\w.-]+", val):
                toks |= identifier_tokens(m)
    return toks


def _observed(payload: dict, table_row: dict) -> str | None:
    for key in ("observed_at", "recorded_at", "updated_at", "captured_at"):
        if payload.get(key):
            return payload[key]
    return table_row.get("_row_time")


def derive_status(record_type: str, p: dict) -> str | None:
    """Canonical status for a live payload row, or None to drop the fact."""
    # readback-style rows carry an explicit status code
    code = p.get("status_code") or p.get("http_status")
    if code is not None and record_type in ("USER_PROFILE", "ANALYTICS_PROFILE"):
        code = str(code)
        if code in ("404", "410"):
            return "ABSENT"
        if code.startswith("2"):
            return "PRESENT"
    if record_type == "USER_PROFILE":
        idents = [p.get(k) for k in _PAYLOAD_STATUS_FIELDS]
        has_pii = any(v not in (None, "") for v in idents)
        acct = (p.get("account_status") or "").upper()
        if acct == "DELETED" and not has_pii:
            return "ABSENT"
        if has_pii or acct in ("ACTIVE", "DELETION_PENDING", ""):
            return "PRESENT"
        return None
    if record_type == "SEARCH_INDEX_ENTRY":
        return "PRESENT"
    if record_type == "ANALYTICS_PROFILE":
        idents = [p.get(k) for k in ("user_id", "customer_reference", "email", "phone", "email_hash")]
        return "IDENTIFIABLE" if any(v not in (None, "") for v in idents) else "ANONYMIZED"
    if record_type == "SUPPORT_MESSAGE":
        idents = [p.get(k) for k in ("requester_name", "requester_email", "requester_phone")]
        return "IDENTIFIABLE" if any(v not in (None, "") for v in idents) else "REDACTED"
    if record_type == "SUPPORT_ATTACHMENT":
        return "PRESENT"
    if record_type == "USER_EXPORT":
        return "PRESENT"
    if record_type == "DATA_FLOW":
        enabled = p.get("enabled")
        return "ACTIVE" if enabled in (True, 1, "1", "true") else "DISABLED"
    if record_type == "BACKUP_ARCHIVE":
        if p.get("archive_present") in (False, 0, "0"):
            return None
        allowed = p.get("restore_allowed")
        return "ACTIVE" if allowed in (True, 1, "1", "true") else "BLOCKED"
    if record_type == "SECURITY_AUDIT_LOG":
        return "PRESENT"
    if record_type == "LEGAL_HOLD_RECORD":
        return "PRESENT"
    if record_type == "ACCOUNT_TOMBSTONE":
        idents = [p.get(k) for k in ("name", "email", "phone", "email_hash")]
        return "IDENTIFIABLE" if any(v not in (None, "") for v in idents) else "REDACTED"
    if record_type == "CACHE_ENTRY":
        return "PRESENT"
    if record_type == "MARKETING_PROFILE":
        return "PRESENT"
    if record_type == "EMAIL_DELIVERY_LOG":
        return "IDENTIFIABLE"
    if record_type == "PRODUCT_TELEMETRY":
        idents = [p.get(k) for k in ("user_id", "email", "phone", "email_hash")]
        return "IDENTIFIABLE" if any(v not in (None, "") for v in idents) else "ANONYMIZED"
    return None


def parse_sql_file(path: str, file_id: str, captured_at: str | None):
    """Yield Facts from one SQL dump."""
    sql = open(path, encoding="utf-8", errors="ignore").read()
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(sql)
        con.commit()
    except Exception:
        # Fall back to statement-by-statement so one bad PRAGMA does not kill the file.
        con = sqlite3.connect(":memory:")
        for stmt in re.split(r";\s*\n", sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                con.execute(stmt)
            except Exception:
                continue
    out = []
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            cols = [d[1] for d in con.execute(f'PRAGMA table_info("{table}")')]
            base = _strip(table)
            colset = set(cols)
            is_generic = "resource_id" in colset and "origin_system" in colset and (
                {"collection", "payload"} <= colset
                or {"collection_name", "record_json"} <= colset)
            rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
            for row in rows:
                d = dict(zip(cols, row))
                if is_generic:
                    rid = d.get("resource_id")
                    origin = d.get("origin_system") or "unknown"
                    collection = d.get("collection") or d.get("collection_name") or ""
                    try:
                        payload = json.loads(d.get("payload") or d.get("record_json") or "{}")
                    except Exception:
                        continue
                    payload = _flatten_payload(payload)
                    rtype = record_type_for(rid, collection)
                    if rtype is None:
                        continue
                    status = derive_status(rtype, payload)
                    if status is None:
                        continue
                    out.append(Fact(
                        system_id=origin,
                        resource_id=rid,
                        record_type=rtype,
                        status=status,
                        subjects=frozenset(_subjects(payload)),
                        observed_at=_observed(payload, {"_row_time": d.get("updated_at") or captured_at}),
                        file_id=file_id,
                        detail=f"{table}:{collection}",
                        retention_until=payload.get("retention_until") or payload.get("retained_until"),
                    ))
                else:
                    rtype = record_type_for(base, base)
                    if rtype is None:
                        if base == "snapshot_scope":
                            continue
                        continue
                    # normalized table: columns carry the payload directly
                    status = derive_status(rtype, d)
                    if status is None:
                        continue
                    subjects = frozenset(_subjects(d))
                    out.append(Fact(
                        system_id=_table_system(base),
                        resource_id=d.get("resource_id") or "",
                        record_type=rtype,
                        status=status,
                        subjects=subjects,
                        observed_at=d.get("updated_at") or captured_at,
                        file_id=file_id,
                        detail=table,
                        retention_until=d.get("retention_until") or d.get("retained_until"),
                    ))
    finally:
        con.close()
    return out


def _table_system(base_table: str) -> str:
    mapping = {
        "customer_profiles": "account_service",
        "customer_search_documents": "search_service",
        "search_documents": "search_service",
        "customer_analytics_profiles": "analytics_platform",
        "support_records": "support_platform",
        "customer_exports": "export_service",
        "connector_subscriptions": "integration_service",
        "backup_restore_entries": "backup_service",
        "security_audit_records": "security_service",
        "deleted_customer_references": "identity_service",
        "retained_order_records": "legal_service",
    }
    return mapping.get(base_table, base_table)
