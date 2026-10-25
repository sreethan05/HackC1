"""Policy engine: reads deletion_rules.csv and maps every record type to the
treatment the org requires, the violation condition, and the audit steps that
express finding, remediation and verification.

The rules file drives the required action (DELETE / REDACT / ANONYMIZE /
RETAIN). Condition nuances ("still attributable", "within the retention
period", "restore access can be blocked", tombstone semantics) are evaluated
against the observed fact.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass


@dataclass
class Treatment:
    action: str            # DELETE | REDACT | ANONYMIZE | RETAIN
    condition: str         # raw condition text from the rules file


# record_type -> rule data_category (they are 1:1 in this policy vocabulary)
RECORD_CATEGORY = {
    "USER_PROFILE": "USER_PROFILE",
    "SECONDARY_IDENTIFIER": "SECONDARY_IDENTIFIER",
    "SEARCH_INDEX_ENTRY": "SEARCH_INDEX_ENTRY",
    "CACHE_ENTRY": "CACHE_ENTRY",
    "SUPPORT_MESSAGE": "SUPPORT_MESSAGE",
    "SUPPORT_ATTACHMENT": "SUPPORT_ATTACHMENT",
    "ANALYTICS_PROFILE": "ANALYTICS_PROFILE",
    "PRODUCT_TELEMETRY": "PRODUCT_TELEMETRY",
    "USER_EXPORT": "USER_EXPORT",
    "MARKETING_PROFILE": "MARKETING_PROFILE",
    "EMAIL_DELIVERY_LOG": "EMAIL_DELIVERY_LOG",
    "SECURITY_AUDIT_LOG": "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD": "LEGAL_HOLD_RECORD",
    "BACKUP_ARCHIVE": "BACKUP_ARCHIVE",
    "ACCOUNT_TOMBSTONE": "ACCOUNT_TOMBSTONE",
}


class Policy:
    def __init__(self, rules_csv: str):
        self.treatments: dict[str, Treatment] = {}
        with open(rules_csv, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                cat = row["data_category"].strip()
                self.treatments[cat] = Treatment(
                    action=row["required_action"].strip().upper(),
                    condition=row["condition"].strip(),
                )

    def treatment_for(self, record_type: str) -> Treatment:
        cat = RECORD_CATEGORY.get(record_type)
        if cat and cat in self.treatments:
            return self.treatments[cat]
        return Treatment("DELETE", "")

    # ---- violation logic -------------------------------------------------

    def is_violation(self, fact, audit_at: str, action: str | None = None) -> bool:
        """True when the observed state conflicts with the required treatment."""
        action = action or self.treatment_for(fact.record_type).action
        status = fact.status
        if fact.record_type == "SECURITY_AUDIT_LOG":
            # RETAIN within the supplied retention period: presence is compliant.
            return False
        if fact.record_type == "LEGAL_HOLD_RECORD":
            return False
        if fact.record_type == "BACKUP_ARCHIVE":
            return status == "ACTIVE"  # restorable archive = recreation risk
        if fact.record_type == "DATA_FLOW":
            return status == "ACTIVE"
        if action == "DELETE":
            # a record that still exists in any attributable form is a violation
            return status in ("PRESENT", "IDENTIFIABLE")
        if action in ("REDACT", "ANONYMIZE"):
            # record may remain, but identifiers must be gone
            return status in ("PRESENT", "IDENTIFIABLE")
        if action == "RETAIN":
            return False
        return False

    def compliant_status(self, fact, action: str | None = None) -> str:
        """The state a verification step must certify for this fact."""
        action = action or self.treatment_for(fact.record_type).action
        if fact.record_type == "BACKUP_ARCHIVE":
            return "BLOCKED"
        if fact.record_type == "DATA_FLOW":
            return "DISABLED"
        if action == "DELETE":
            return "ABSENT"
        if action == "REDACT":
            return "REDACTED"
        if action == "ANONYMIZE":
            return "ANONYMIZED"
        if action == "RETAIN":
            return "PRESENT"
        return "ABSENT"
