"""Canonical fact model shared by every evidence parser.

A Fact is the atomic unit the audit reasons over:
    (system_id, resource_id, record_type, status, subjects, observed_at, file_id)

`record_type` mirrors the data categories used by deletion_rules.csv.
`status` is the observed state, normalized to one of:
    PRESENT, ABSENT, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, BLOCKED
`subjects` holds lowercased identifier tokens (emails, user ids, email hashes,
normalized phones) that link the fact to an identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


RECORD_TYPES = {
    "USER_PROFILE",
    "SEARCH_INDEX_ENTRY",
    "CACHE_ENTRY",
    "USER_EXPORT",
    "SUPPORT_ATTACHMENT",
    "MARKETING_PROFILE",
    "SUPPORT_MESSAGE",
    "EMAIL_DELIVERY_LOG",
    "ANALYTICS_PROFILE",
    "PRODUCT_TELEMETRY",
    "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD",
    "BACKUP_ARCHIVE",
    "ACCOUNT_TOMBSTONE",
    "DATA_FLOW",
}

# Canonical collection / payload names -> record types. Parsers use this map so
# unknown-but-similar names degrade gracefully instead of being invented.
COLLECTION_TYPES = {
    "customer_profiles": "USER_PROFILE",
    "profile_lookup": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "support_records": "SUPPORT_MESSAGE",
    "support_tickets": "SUPPORT_MESSAGE",
    "customer_exports": "USER_EXPORT",
    "customer_export_jobs": "USER_EXPORT",
    "connector_subscriptions": "DATA_FLOW",
    "connectors": "DATA_FLOW",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "restore_entries": "BACKUP_ARCHIVE",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "legal_holds": "LEGAL_HOLD_RECORD",
    "profile_cache": "CACHE_ENTRY",
    "cache_entries": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "email_delivery_log": "EMAIL_DELIVERY_LOG",
    "marketing_profiles": "MARKETING_PROFILE",
    "product_telemetry": "PRODUCT_TELEMETRY",
    "customer_support_attachments": "SUPPORT_ATTACHMENT",
}

# resource-id prefix -> record type (fallback when a payload has no collection name)
PREFIX_TYPES = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "cache": "CACHE_ENTRY",
    "export": "USER_EXPORT",
    "analytics": "ANALYTICS_PROFILE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "backup": "BACKUP_ARCHIVE",
    "data_flow": "DATA_FLOW",
    "support": "SUPPORT_MESSAGE",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "marketing": "MARKETING_PROFILE",
    "object": "SUPPORT_ATTACHMENT",
    "telemetry": "PRODUCT_TELEMETRY",
}


def record_type_for(resource_id: str, collection: Optional[str] = None) -> Optional[str]:
    if collection:
        key = collection.strip().lower()
        if key in COLLECTION_TYPES:
            return COLLECTION_TYPES[key]
    rid = (resource_id or "").lower()
    for prefix, rtype in sorted(PREFIX_TYPES.items(), key=lambda kv: -len(kv[0])):
        if rid.startswith(prefix + "_"):
            return rtype
    return None


SYSTEM_FOR_TYPE = {
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
}


@dataclass
class Fact:
    system_id: str
    resource_id: str
    record_type: str
    status: str
    subjects: frozenset = field(default_factory=frozenset)
    observed_at: Optional[str] = None
    file_id: str = ""
    detail: str = ""
    retention_until: Optional[str] = None

    def key(self) -> tuple:
        return (self.system_id, self.resource_id, self.record_type)

    def with_subjects(self, subjects) -> "Fact":
        return replace(self, subjects=frozenset(set(self.subjects) | set(subjects)))


def normalize_phone(raw: str) -> Optional[str]:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits if len(digits) >= 8 else None


def identifier_tokens(raw: str) -> set:
    """Lowercased identifier tokens extracted from a free-form value."""
    out = set()
    if not raw:
        return out
    val = str(raw).strip().lower()
    if "@" in val:
        out.add(val)
    if val.startswith("user_"):
        out.add(val)
    if len(val) >= 32 and all(ch in "0123456789abcdef" for ch in val):
        out.add(val)  # sha-like hash
        return out
    # phone-shaped values only: separators or a leading +, 8-15 digits
    digits = "".join(ch for ch in val if ch.isdigit())
    looks_phone = ("+" in val or any(sep in val for sep in " -().")
                   or (val.isdigit() and 8 <= len(val) <= 15))
    if looks_phone and 8 <= len(digits) <= 15:
        out.add("tel:" + digits)
    return out
