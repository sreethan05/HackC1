"""Evidence index: walks dataset/evidence per the manifest and produces every
parseable Fact plus a raw-text map used for identifier-based file selection.
"""
from __future__ import annotations

import csv
import os

from . import parse_json, parse_logs, parse_pdf, parse_sql, parse_images
from .facts import Fact


class EvidenceIndex:
    def __init__(self, dataset_dir: str):
        self.dataset_dir = dataset_dir
        self.manifest = {}          # file_id -> {path, origin, captured_at}
        self.facts: list[Fact] = [] # all parsed facts
        self.raw_text = {}          # file_id -> raw text (small files only)
        self._load_manifest()
        self._parse_all()

    def _load_manifest(self):
        path = os.path.join(self.dataset_dir, "evidence_files.csv")
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                self.manifest[row["file_id"]] = {
                    "path": os.path.join(self.dataset_dir, row["file_path"].replace("/", os.sep)),
                    "origin": row.get("origin", ""),
                    "captured_at": row.get("captured_at", ""),
                }

    def _parse_all(self):
        for file_id, meta in sorted(self.manifest.items()):
            path, captured = meta["path"], meta["captured_at"]
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext == ".sql":
                    self.facts.extend(parse_sql.parse_sql_file(path, file_id, captured))
                elif ext == ".json":
                    self.facts.extend(parse_json.parse_json_file(path, file_id, captured))
                elif ext == ".log":
                    self.facts.extend(parse_logs.facts_from_log(path, file_id, captured))
                elif ext == ".pdf":
                    facts, text = parse_pdf.parse_pdf_file(path, file_id, captured)
                    self.facts.extend(facts)
                    self.raw_text[file_id] = text.lower()
                elif ext == ".png":
                    self.facts.extend(parse_images.facts_for_file(file_id, captured, image_path=path))
                elif ext == ".txt":
                    self.facts.extend(self._parse_plain_table(path, file_id, captured))
            except Exception as exc:  # never let one file kill the run
                self.raw_text.setdefault(file_id, "")
                print(f"[warn] failed to parse {file_id}: {exc}")

    @staticmethod
    def _parse_plain_table(path, file_id, captured):
        """Handover-style pipe tables: ts | system | resource | type | result | subject"""
        from .parse_json import _RESULT_MAP
        out = []
        for line in open(path, encoding="utf-8", errors="ignore"):
            if line.count("|") < 5:
                continue
            parts = [p.strip() for p in line.split("|")]
            ts, system, rid, rtype, result, subject = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            status = _RESULT_MAP.get(result.upper())
            if status is None:
                continue
            from .facts import identifier_tokens
            out.append(Fact(system_id=system, resource_id=rid, record_type=rtype.upper(),
                            status=status, subjects=frozenset(identifier_tokens(subject)),
                            observed_at=ts or captured, file_id=file_id, detail="handover"))
        return out

    def facts_by_file(self) -> dict:
        by_file = {}
        for f in self.facts:
            by_file.setdefault(f.file_id, []).append(f)
        return by_file
