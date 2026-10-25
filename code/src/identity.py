"""Identity resolution: request email -> stable user and full identifier set.

Current identities come from user_identities.csv; historical identifiers come
from user_audit_log.csv (old and new values of every email/email_hash/phone
change). Matching is case-insensitive for emails and format-insensitive for
phones. Name similarity is deliberately NOT an identity link (the dataset
plants near-duplicate names as decoys).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field

from .facts import identifier_tokens


@dataclass
class Identity:
    user_id: str
    name: str = ""
    current_email: str = ""
    current_phone: str = ""
    current_hash: str = ""
    historical_emails: set = field(default_factory=set)
    historical_phones: set = field(default_factory=set)
    historical_hashes: set = field(default_factory=set)

    def tokens(self) -> set:
        """Every identifier that can link evidence to this person."""
        toks = {self.user_id.lower()}
        toks |= identifier_tokens(self.current_email)
        toks |= identifier_tokens(self.current_phone)
        toks |= identifier_tokens(self.current_hash)
        for e in self.historical_emails:
            toks |= identifier_tokens(e)
        for p in self.historical_phones:
            toks |= identifier_tokens(p)
        for h in self.historical_hashes:
            toks |= identifier_tokens(h)
        return toks

    def emails(self) -> set:
        out = set()
        if self.current_email:
            out.add(self.current_email.lower())
        out |= {e.lower() for e in self.historical_emails}
        return out


class IdentityIndex:
    def __init__(self, identities_csv, audit_log_csv):
        self.by_user: dict[str, Identity] = {}
        self._email_to_user: dict[str, str] = {}
        self._load_identities(identities_csv)
        self._load_history(audit_log_csv)

    def _load_identities(self, path):
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                uid = row["user_id"]
                ident = Identity(
                    user_id=uid,
                    name=row.get("name", ""),
                    current_email=(row.get("email") or "").strip(),
                    current_phone=(row.get("phone") or "").strip(),
                    current_hash=(row.get("email_hash") or "").strip(),
                )
                self.by_user[uid] = ident
                if ident.current_email:
                    self._email_to_user[ident.current_email.lower()] = uid

    def _load_history(self, path):
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ident = self.by_user.get(row["user_id"])
                if ident is None:
                    continue
                field_name = row["field_name"]
                for value in (row.get("old_value"), row.get("new_value")):
                    value = (value or "").strip()
                    if not value:
                        continue
                    if field_name == "email":
                        ident.historical_emails.add(value)
                        self._email_to_user.setdefault(value.lower(), ident.user_id)
                    elif field_name == "phone":
                        ident.historical_phones.add(value)
                    elif field_name == "email_hash":
                        ident.historical_hashes.add(value)

    def resolve(self, email: str):
        """Return (Identity, matched_form) for a request email, or (None, None)."""
        key = (email or "").strip().lower()
        uid = self._email_to_user.get(key)
        if uid is None:
            return None, None
        ident = self.by_user[uid]
        form = "current" if ident.current_email.lower() == key else "historical"
        return ident, form
