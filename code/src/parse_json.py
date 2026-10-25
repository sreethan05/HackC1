"""Parsers for the JSON evidence family.

Handled shapes:
- service_resource_export_*: consolidated readback per service (profile
  lookups, analytics profiles, connectors, restore entries, exports, index
  queries, audit records, support tickets).
- customer_data_snapshot_bundle_*: per data_store snapshots of collections.
- unified record manifests (record_type/result/subject_reference tuples):
  connector_configurations, customer_export_manifest, security_audit_ledger.
- customer_analytics_report / customer_export_jobs: single-collection records.
"""
from __future__ import annotations

import json

from .facts import Fact, identifier_tokens
from .parse_sql import derive_status, _subjects

_RESULT_MAP = {
    "NOT_FOUND": "ABSENT",
    "FOUND": "PRESENT",
    "PRESENT": "PRESENT",
    "IDENTIFIABLE": "IDENTIFIABLE",
    "ANONYMIZED": "ANONYMIZED",
    "REDACTED": "REDACTED",
    "ACTIVE": "ACTIVE",
    "DISABLED": "DISABLED",
    "BLOCKED": "BLOCKED",
}


def _time(*vals):
    for v in vals:
        if v:
            return v
    return None


def _flatten(rec: dict) -> dict:
    """Readback rows nest content: search index queries under hits.documents[],
    account profile lookups under profile. Lift those fields to the top level
    so identity extraction and status derivation see them."""
    hits = rec.get("hits")
    if isinstance(hits, dict):
        docs = hits.get("documents") or []
        if docs and isinstance(docs[0], dict):
            merged = {k: v for k, v in rec.items() if k != "hits"}
            merged.update(docs[0])
            merged["_total_hits"] = hits.get("total")
            return merged
    profile = rec.get("profile")
    if isinstance(profile, dict):
        merged = {k: v for k, v in rec.items() if k != "profile"}
        merged.update({k: v for k, v in profile.items() if v is not None})
        return merged
    return rec


def _subjects_of(record: dict) -> frozenset:
    return frozenset(_subjects(record))


def parse_json_file(path: str, file_id: str, captured_at: str | None):
    data = json.load(open(path, encoding="utf-8"))
    out = []
    exported_at = data.get("exported_at") or data.get("captured_at") or captured_at

    if "service_exports" in data:
        for system, body in data["service_exports"].items():
            for collection, records in body.items():
                for rec in records:
                    flat = _flatten(rec)
                    out.extend(_fact_from_generic_record(
                        flat, system, collection, file_id, exported_at))
        return out

    if "snapshots" in data:
        for snap in data["snapshots"]:
            store = snap.get("data_store") or "unknown"
            collection = snap.get("collection") or ""
            for rec in snap.get("records", []):
                out.extend(_fact_from_generic_record(
                    rec, store, collection, file_id,
                    snap.get("captured_at") or exported_at))
        return out

    if "records" in data and isinstance(data["records"], list):
        for rec in data["records"]:
            system = rec.get("system") or data.get("origin") or "unknown"
            out.extend(_fact_from_generic_record(
                rec, system, rec.get("collection"), file_id,
                _time(rec.get("recorded_at"), exported_at)))
        return out

    return out


def _fact_from_generic_record(rec: dict, system: str, collection: str,
                              file_id: str, default_time):
    from .facts import record_type_for, SYSTEM_FOR_TYPE
    rid = rec.get("resource_id") or ""
    if not rid:
        return []
    rtype = (rec.get("record_type") or record_type_for(rid, collection) or "")
    rtype = rtype.strip().upper()
    if rtype not in {t for t in _RESULT_MAP} | {
        "USER_PROFILE", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "USER_EXPORT",
        "SUPPORT_ATTACHMENT", "MARKETING_PROFILE", "SUPPORT_MESSAGE",
        "EMAIL_DELIVERY_LOG", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
        "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE",
        "ACCOUNT_TOMBSTONE", "DATA_FLOW",
    }:
        # Unknown record type: fall back to the resource-prefix mapping.
        from .facts import record_type_for as _rtf
        rtype = _rtf(rid, collection) or ""
        if not rtype:
            return []

    explicit = rec.get("result")
    if explicit:
        status = _RESULT_MAP.get(str(explicit).strip().upper())
        if status is None:
            return []
    elif rtype == "SEARCH_INDEX_ENTRY" and "_total_hits" in rec:
        total = rec.get("_total_hits")
        status = "PRESENT" if total not in (0, "0", None) else "ABSENT"
    else:
        status = derive_status(rtype, rec)
        if status is None:
            return []

    if not system or system in ("unknown", "customer_export_service",
                                "analytics_identity_store", "user_export_store",
                                "integration_management_service",
                                "security_audit_service"):
        system = SYSTEM_FOR_TYPE.get(rtype, system or "unknown")

    return [Fact(
        system_id=system,
        resource_id=rid,
        record_type=rtype,
        status=status,
        subjects=_subjects_of(rec),
        observed_at=_time(rec.get("observed_at"), rec.get("recorded_at"),
                          rec.get("updated_at"), default_time),
        file_id=file_id,
        detail=collection,
        retention_until=rec.get("retention_until") or rec.get("retained_until"),
    )]
