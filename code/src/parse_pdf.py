"""PDF evidence: text extraction with pdfplumber when available and a
dependency-free fallback that decompresses FlateDecode streams and reads the
text-show operators. Returns (facts, full_text).

PDFs in this corpus are business artifacts (compliance journal extracts,
legal-hold registers, customer statements, support-attachment scans). Their
facts are recognized generically: references to resource ids (audit_record_*,
support_*, export_*), subject references (user_XX / emails) and retain-until
dates.
"""
from __future__ import annotations

import re
import zlib

from .facts import Fact, identifier_tokens, record_type_for


def extract_text(path: str) -> str:
    try:
        import pdfplumber  # type: ignore
        parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return _fallback_text(path)


def _fallback_text(path: str) -> str:
    raw = open(path, "rb").read()
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        if b"Tj" in data or b"TJ" in data:
            for tm in re.finditer(rb"\((?:[^()\\]|\\.)*\)", data):
                lit = tm.group(0)[1:-1]
                lit = re.sub(rb"\\([()\\])", rb"\1", lit)
                try:
                    chunks.append(lit.decode("latin-1"))
                except Exception:
                    pass
    return " ".join(chunks)


_RID = re.compile(r"\b(?:audit_record|account|search|support|export|analytics|"
                  r"tombstone|backup|data_flow|legal_hold|marketing|cache|object)"
                  r"_[A-Za-z0-9]+\b")
_RETAIN = re.compile(r"retain\s*until[:\s]+([0-9]{1,2}\s+\w+\s+[0-9]{4}|[0-9-]{8,})", re.I)


def parse_pdf_file(path: str, file_id: str, captured_at: str | None):
    text = extract_text(path)
    facts = []
    rids = []
    for m in _RID.finditer(text):
        rid = m.group(0)
        # avoid account_XXXX style refs that are actually part of longer ids
        if rid not in rids:
            rids.append(rid)
    subjects = set()
    for tok in re.findall(r"[\w.+-]+@[\w.-]+", text):
        subjects |= identifier_tokens(tok)
    for tok in re.findall(r"\buser_\d+\b", text):
        subjects |= identifier_tokens(tok)

    retain_until = None
    rm = _RETAIN.search(text)
    if rm:
        retain_until = _normalize_date(rm.group(1))

    for rid in rids:
        rtype = record_type_for(rid)
        if rtype is None:
            continue
        # A PDF that documents a stored file artifact is an attachment
        # (rule: attachment belonging only to the subject -> DELETE), while a
        # scanned conversation would be a retained support message.
        if rtype == "SUPPORT_MESSAGE" and re.search(r"\bfile\s+\S+\.pdf\b", text, re.I):
            rtype = "SUPPORT_ATTACHMENT"
        if rtype in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
            status = "PRESENT"
        elif rtype in ("USER_EXPORT", "USER_PROFILE", "SEARCH_INDEX_ENTRY",
                       "SUPPORT_ATTACHMENT", "SUPPORT_MESSAGE", "ANALYTICS_PROFILE",
                       "MARKETING_PROFILE", "CACHE_ENTRY"):
            # The document shows the artifact still exists and names the subject.
            status = "IDENTIFIABLE" if rtype in ("SUPPORT_MESSAGE", "ANALYTICS_PROFILE") else "PRESENT"
        else:
            status = "PRESENT"
        facts.append(Fact(system_id=_system_for(rtype), resource_id=rid,
                          record_type=rtype, status=status,
                          subjects=frozenset(subjects), observed_at=captured_at,
                          file_id=file_id, detail="pdf", retention_until=retain_until))
    return facts, text


def _normalize_date(raw: str) -> str | None:
    months = {m: i + 1 for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
         "sep", "oct", "nov", "dec"])}
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw.strip())
    if m and m.group(2)[:3].lower() in months:
        return f"{m.group(3)}-{months[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}T23:59:59Z"
    return None


def _system_for(rtype: str) -> str:
    return {
        "SECURITY_AUDIT_LOG": "security_service",
        "LEGAL_HOLD_RECORD": "legal_service",
        "USER_EXPORT": "export_service",
        "USER_PROFILE": "account_service",
        "SEARCH_INDEX_ENTRY": "search_service",
        "SUPPORT_ATTACHMENT": "support_platform",
        "SUPPORT_MESSAGE": "support_platform",
        "ANALYTICS_PROFILE": "analytics_platform",
        "ACCOUNT_TOMBSTONE": "identity_service",
        "BACKUP_ARCHIVE": "backup_service",
        "DATA_FLOW": "integration_service",
        "MARKETING_PROFILE": "marketing_platform",
        "CACHE_ENTRY": "cache_service",
    }.get(rtype, "unknown")
