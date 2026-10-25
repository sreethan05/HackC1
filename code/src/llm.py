"""Optional LLM/VLM layer.

The pipeline is fully deterministic; this module exists for two purposes:

1. **Image fact extraction.** The console screenshots are visual readbacks.
   When a vision-model endpoint is configured (environment variables), the
   image facts are extracted live and the cache is refreshed; without
   credentials the committed cache produced during development is used.
2. **Intent fallback.** If a request text matches no deterministic intent
   rule, an LLM can classify it. On this corpus every request is covered by
   the rules, so no call is made.

Every call is recorded in the usage ledger (`evaluation/usage_report.md`
sources its numbers from `usage_ledger.json` next to this file when present).

Configuration (environment variables, never committed):
    LLM_BASE_URL   e.g. https://api.openai.com/v1  (OpenAI-compatible)
    LLM_API_KEY    bearer token
    LLM_MODEL      e.g. gpt-4o-mini / gpt-4.1-mini
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(HERE, "..", "evaluation", "usage_ledger.json")

VLM_PROMPT = """You are reading a screenshot of an internal admin console.
Extract every fact visible in it as a JSON array. Each item is one object:
{"system_id": string, "resource_id": string, "record_type": string,
 "status": string, "subjects": [identifiers]}.
system_id is the owning platform (account_service, search_service,
analytics_platform, integration_service, backup_service, export_service,
security_service, support_platform, identity_service, marketing_platform,
object_storage, cache_service).
record_type is one of USER_PROFILE, SEARCH_INDEX_ENTRY, CACHE_ENTRY, USER_EXPORT,
SUPPORT_ATTACHMENT, SUPPORT_MESSAGE, MARKETING_PROFILE, ANALYTICS_PROFILE,
PRODUCT_TELEMETRY, SECURITY_AUDIT_LOG, LEGAL_HOLD_RECORD, BACKUP_ARCHIVE,
ACCOUNT_TOMBSTONE, DATA_FLOW.
status is one of PRESENT, ABSENT, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE,
DISABLED, BLOCKED - describing what the screen shows about that resource.
subjects are the email addresses / user_xx references visible for the record.
Return only the JSON array."""


class UsageLedger:
    def __init__(self):
        self.calls = []

    def record(self, purpose: str, model: str, input_tokens: int, output_tokens: int):
        self.calls.append({"purpose": purpose, "model": model,
                           "input_tokens": input_tokens,
                           "output_tokens": output_tokens,
                           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self.save()

    def save(self):
        try:
            with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.calls, fh, indent=2)
        except Exception:
            pass


LEDGER = UsageLedger()


def configured() -> bool:
    return bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_BASE_URL")
                and os.environ.get("LLM_MODEL"))


def chat(prompt: str, image_path: str | None = None, max_tokens: int = 2000) -> str | None:
    """OpenAI-compatible chat completion; returns text or None on any failure."""
    if not configured():
        return None
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    model = os.environ["LLM_MODEL"]
    content: list = [{"type": "text", "text": prompt}]
    if image_path:
        import base64
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens, "temperature": 0}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['LLM_API_KEY']}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        usage = data.get("usage", {})
        LEDGER.record("image" if image_path else "text", model,
                      usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def deletion_intent(request_text: str) -> bool:
    """LLM fallback for intent classification: True when the text authorizes
    deletion of the account/personal data."""
    out = chat(
        "Does this customer message request deletion or erasure of their account or "
        "personal data (yes/no)? Requests for access, correction, export, marketing "
        "opt-out, password help or copies of documents do NOT authorize deletion.\n\n"
        f"Message: {request_text}\nAnswer with exactly yes or no.")
    return bool(out) and "yes" in out.lower()
