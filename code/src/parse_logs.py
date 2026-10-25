"""Parsers for the application/infrastructure log evidence.

Nine syntactic families all encode the same operational events:
  key=value (resource_lookup_trace, service_events, support_replica,
             search_index_diagnostic, notification_delivery),
  JSON lines (structured_service_events), syslog (syslog_archive),
  pipe-delimited (operations_stream), bracketed (application_trace),
  cloud-function (cloud_runtime_export).

Every line is reduced to (ts, service, trace_id, event, resource_id, fields).
Events are then grouped by trace_id so a subject carried on a "started" line
(lookup_key / subject_key / match_key / scan_key / subject_ref) can be joined
to the resource described on the "completed" line. Completed events are turned
into Facts via a handler table; anything unknown falls through to a generic
handler driven by the resource-id prefix, so unseen event names still work.
"""
from __future__ import annotations

import json
import re

from .facts import Fact, identifier_tokens, record_type_for
from .parse_sql import derive_status

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)")

_KV = re.compile(r'(\w+)=("([^"]*)"|\'([^\']*)\'|[^\s]+)')


def _kv_fields(text: str) -> dict:
    out = {}
    for m in _KV.finditer(text):
        key = m.group(1)
        val = m.group(3) if m.group(3) is not None else m.group(2)
        val = val.strip().strip('"').strip("'")
        out[key] = val
    return out


def _unquote(val: str) -> str:
    return (val or "").replace('\\"', '"').strip('"')


def _cloud_fields(message: str) -> dict:
    out = {}
    for m in re.finditer(r'(\w+)\\?"=\\?"((?:[^"\\]|\\.)*)\\?"', message or ""):
        out[m.group(1)] = _unquote(m.group(2))
    return out


def _fields_from_braces(text: str) -> dict:
    m = re.search(r"\{.*\}", text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0).replace('\\"', '"'))
        if isinstance(data, dict):
            return {k: ("" if v is None else str(v)) for k, v in data.items()}
    except Exception:
        pass
    return {}


def parse_line(line: str):
    """Return (ts, service, trace_id, event, resource_id, fields) or None."""
    ts_m = _TS.match(line)
    ts = ts_m.group(1) if ts_m else None
    body = line[ts_m.end():] if ts_m else line

    if "{" in body and '"event"' in body:
        fields = _fields_from_braces(body)
        if fields:
            return (ts, fields.get("logger") or fields.get("service") or "",
                    fields.get("trace_id", ""), fields.get("event", ""),
                    fields.get("resource_id", ""), fields)
    if body.lstrip().startswith("["):
        m = re.match(r"\s*\[(\w+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*([\w.]+):\s*(.*)$", body)
        if m:
            fields = _kv_fields(m.group(5))
            return (ts, m.group(2), m.group(3), m.group(4), fields.get("resource_id", ""), fields)
    if body.count("|") >= 5:
        parts = [p.strip() for p in body.split("|")]
        # ts | LEVEL | service | trace | event | k=v...
        fields = _kv_fields(" ".join(parts[5:]))
        for p in parts[5:]:
            if "=" not in p and p:
                fields.setdefault("event", p.strip('"'))
        return (ts, parts[2], parts[3].strip('"') if len(parts) > 3 else "",
                fields.get("event") or (parts[4].strip('"') if len(parts) > 4 else ""),
                fields.get("resource_id", ""), fields)
    if body.lstrip().startswith("<"):
        m = re.match(r"\s*<\d+>\d+\s+\S+\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+(.*)$", body)
        if m:
            fields = _kv_fields(m.group(3))
            return (ts, m.group(1), m.group(2), fields.get("event", ""),
                    fields.get("resource_id", ""), fields)
    if "message=" in body and "function=" in body:
        head, _, msg = body.partition("message=")
        fields = _cloud_fields(msg)
        fn = re.search(r"function=(\S+)", body)
        rid = re.search(r"requestId=(\S+)", body)
        return (ts, (fn.group(1) if fn else ""), (rid.group(1) if rid else ""),
                fields.get("event", ""), fields.get("resource_id", ""), fields)
    # default: key=value stream
    fields = _kv_fields(body)
    svc = re.search(r"service=(\S+)", body)
    return (ts, (svc.group(1) if svc else fields.get("logger", "")),
            fields.get("trace_id", ""), fields.get("event", ""),
            fields.get("resource_id", ""), fields)


_SUBJECT_KEYS = ("lookup_key", "subject_key", "match_key", "scan_key",
                 "subject_ref", "correlation_key", "matched_value",
                 "customer_reference", "subject_reference")


def _subject_tokens(fields: dict) -> set:
    toks = set()
    for key in _SUBJECT_KEYS:
        toks |= identifier_tokens(fields.get(key))
    for key in ("requester_phone", "phone"):
        toks |= identifier_tokens(fields.get(key))
    return toks


def _nonempty(v) -> bool:
    return v not in (None, "", "null", "None")


def _emit(rtype: str, status: str, svc: str, rid: str, subjects, ts, file_id,
          retention=None, detail=""):
    if not rid or not rtype or not status:
        return None
    return Fact(system_id=svc, resource_id=rid, record_type=rtype, status=status,
                subjects=frozenset(subjects), observed_at=ts, file_id=file_id,
                detail=detail, retention_until=retention)


def facts_from_log(path: str, file_id: str, captured_at: str | None):
    raw_lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    entries = []
    for ln in raw_lines:
        parsed = parse_line(ln)
        if parsed and parsed[3]:
            entries.append(parsed)

    # join started/completed halves of a trace
    trace_subjects: dict[str, set] = {}
    for ts, svc, trace, event, rid, fields in entries:
        toks = _subject_tokens(fields)
        if toks and trace:
            trace_subjects.setdefault(trace, set()).update(toks)

    out = []
    for ts, svc, trace, event, rid, fields in entries:
        subjects = set(_subject_tokens(fields)) | trace_subjects.get(trace, set())
        ts_use = ts or captured_at
        f = _handle_event(svc, event, rid, fields, subjects, ts_use, file_id)
        if f:
            out.append(f)
    return out


def _handle_event(svc, event, rid, fields, subjects, ts, file_id):
    if not rid:
        return None
    f = (event or "").lower()
    if svc == "account_service" and "profile_lookup_completed" in f:
        rows = fields.get("rows", "")
        status = "ABSENT" if rows in ("0", "") or fields.get("http_status") == "404" else "PRESENT"
        return _emit("USER_PROFILE", status, svc, rid, subjects, ts, file_id)
    if svc == "search_service" and "index_query_completed" in f:
        result = (fields.get("result") or "").lower()
        hits = fields.get("total_hits", "1")
        status = "PRESENT" if result == "hit" or hits not in ("0", "") else "ABSENT"
        return _emit("SEARCH_INDEX_ENTRY", status, svc, rid, subjects, ts, file_id)
    if svc == "analytics_platform" and ("identity_scan" in f or "profile_read" in f or "profile_scan" in f):
        if fields.get("direct_identifiers") in ("1", "2", "3") or _nonempty(fields.get("matched_value")) \
           or _nonempty(fields.get("email")) or _nonempty(fields.get("customer_reference")):
            status = "IDENTIFIABLE"
        elif "read" in f or "read_completed" in f:
            status = "ANONYMIZED" if not any(_nonempty(fields.get(k)) for k in
                                             ("email", "phone", "email_hash", "customer_reference")) else "IDENTIFIABLE"
        else:
            status = None
        return _emit("ANALYTICS_PROFILE", status, svc, rid, subjects, ts, file_id)
    if svc == "support_platform" and "message_pii_scan" in f:
        return _emit("SUPPORT_MESSAGE", "IDENTIFIABLE", svc, rid, subjects, ts, file_id)
    if svc == "support_platform" and "attachment_pii_scan" in f:
        return _emit("SUPPORT_ATTACHMENT", "PRESENT", svc, rid, subjects, ts, file_id)
    if svc == "support_platform" and "ticket_read_completed" in f:
        idents = any(_nonempty(fields.get(k)) for k in ("requester_name", "requester_email", "requester_phone"))
        return _emit("SUPPORT_MESSAGE", "IDENTIFIABLE" if idents else "REDACTED", svc, rid, subjects, ts, file_id)
    if svc == "support_replica" and "contact_query_completed" in f:
        rows = fields.get("rows", "1")
        return _emit("SUPPORT_ATTACHMENT", "PRESENT" if rows not in ("0", "") else "ABSENT",
                     "support_platform", rid, subjects, ts, file_id)
    if "deletion_marker_check" in f:
        if (fields.get("marker_found") or "").lower() == "false":
            return _emit("SUPPORT_ATTACHMENT", "PRESENT", "support_platform", rid, subjects, ts, file_id)
        return _emit("SUPPORT_ATTACHMENT", "ABSENT", "support_platform", rid, subjects, ts, file_id)
    if svc == "security_service" and "audit_record" in f and "completed" in f:
        rows = fields.get("rows", "1")
        return _emit("SECURITY_AUDIT_LOG", "PRESENT" if rows not in ("0", "") else "ABSENT",
                     svc, rid, subjects, ts, file_id,
                     retention=fields.get("retention_until"))
    if svc == "integration_service" and ("connector" in f and ("heartbeat" in f or "config_read_completed" in f or "status" in f)):
        enabled = (fields.get("enabled") or "").lower()
        status = "ACTIVE" if enabled in ("true", "1") else "DISABLED"
        return _emit("DATA_FLOW", status, svc, rid, subjects, ts, file_id)
    if svc == "backup_service" and "restore" in f and ("completed" in f or "evaluated" in f):
        if "candidate" in f:
            eligible = (fields.get("eligible") or "").lower()
            blocked = (fields.get("deletion_block") or "").lower()
            status = "ACTIVE" if eligible in ("true", "1") and blocked != "true" else "BLOCKED"
            return _emit("BACKUP_ARCHIVE", status, svc, rid, subjects, ts, file_id)
        allowed = (fields.get("restore_allowed") or "").lower()
        if (fields.get("archive_present") or "").lower() == "false":
            return None
        status = "ACTIVE" if allowed in ("true", "1") else "BLOCKED"
        return _emit("BACKUP_ARCHIVE", status, svc, rid, subjects, ts, file_id)
    if svc == "identity_service" or rid.startswith("tombstone_"):
        payload = {k: (None if fields.get(k) in (None, "", "null") else fields.get(k))
                   for k in ("name", "email", "phone", "email_hash")}
        payload["account_status"] = "DELETED"
        status = derive_status("ACCOUNT_TOMBSTONE", payload)
        return _emit("ACCOUNT_TOMBSTONE", status, "identity_service", rid, subjects, ts, file_id)
    # generic fallback for unseen completed events
    if "completed" in f or "scan" in f or "evaluated" in f:
        rtype = record_type_for(rid)
        if rtype is None:
            return None
        status = derive_status(rtype, fields)
        if status is None:
            return None
        # a record surfaced through a person-keyed lookup is attributable to
        # that person even when its stored fields are nulled
        if status == "ANONYMIZED" and subjects and rtype in (
                "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"):
            status = "IDENTIFIABLE"
        return _emit(rtype, status, svc, rid, subjects, ts, file_id,
                     retention=fields.get("retention_until") or fields.get("retained_until"))
    return None
