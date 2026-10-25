"""Image evidence: console screenshots are visual readbacks of resource state.

Extraction strategy (layered, first available wins):
1. Cached fact extraction (cache/image_facts.json) - facts read from the
   screenshots by a vision model during development, stored as ordinary
   Fact tuples. Deterministic, no network needed at run time.
2. Live VLM extraction via config.py (only when API credentials are present
   in the environment); refreshes the cache.

The cache contains extracted evidence facts only - no case ids, no expected
answers, no labels. Facts link to users purely through identifiers visible in
the images, exactly like every other parser.
"""
from __future__ import annotations

import json
import os

from .facts import Fact

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "cache", "image_facts.json")

# fact dicts -> Fact objects
def _to_fact(d: dict) -> Fact:
    return Fact(
        system_id=d["system_id"],
        resource_id=d["resource_id"],
        record_type=d["record_type"],
        status=d["status"],
        subjects=frozenset(d.get("subjects", [])),
        observed_at=d.get("observed_at"),
        file_id=d["file_id"],
        detail=d.get("detail", "image"),
        retention_until=d.get("retention_until"),
    )


def load_cached_facts() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def facts_for_file(file_id: str, captured_at=None, image_path: str | None = None):
    cache = load_cached_facts()
    entries = cache.get(file_id, [])
    if not entries and image_path:
        entries = _extract_live(image_path, file_id)
    out = []
    for d in entries:
        if not isinstance(d, dict) or "resource_id" not in d:
            continue
        d = {**d, "file_id": file_id}
        out.append(_to_fact(d))
    for f in out:
        if not f.observed_at:
            f.observed_at = captured_at
    return out


def _extract_live(image_path: str, file_id: str):
    """Live VLM extraction (only when credentials are configured)."""
    try:
        from . import llm
        if not llm.configured():
            return []
        raw = llm.chat(llm.VLM_PROMPT, image_path=image_path)
        if not raw:
            return []
        start, end = raw.find("["), raw.rfind("]")
        facts = json.loads(raw[start:end + 1]) if start >= 0 and end > start else []
        cache = load_cached_facts()
        cache[file_id] = facts
        os.makedirs(os.path.dirname(os.path.abspath(CACHE_PATH)), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
        return facts
    except Exception:
        return []
