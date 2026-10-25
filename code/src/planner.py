"""Per-case audit and step planning.

Gather -> reconcile -> classify -> order:

Gather    evidence facts whose subject tokens intersect the user's identifier
          set, filtered to observed_at <= audit_at (file captured_at as
          fallback), plus activity catalog-link facts, plus workflow-associated
          resources the file evidence never names.
Reconcile collapse duplicate (system, resource, record_type) observations;
          if copies disagree (replication drift), the live copy wins.
Classify  a fact is a violation when its observed state conflicts with the
          treatment required by deletion_rules.csv.
Order     FINDING then REMEDIATION then VERIFICATION; within a phase the
          operation order fixed by the problem statement, then system_id and
          resource_id ascending.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .activity import ActivityIndex, parse_ts
from .facts import Fact

FINDING_OP = {
    "DELETE": ("REPORT_REMAINING_RECORD", "PRESENT"),
    "EXPOSED": ("REPORT_EXPOSED_PII", "PRESENT"),
    "FLOW": ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    "RESTORE": ("REPORT_RESTORE_RISK", "ACTIVE"),
}
REMEDIATION_OP = {
    "FLOW": ("DISABLE_DATA_FLOW", "DISABLED"),
    "RESTORE": ("BLOCK_RESTORE", "BLOCKED"),
    "DELETE": ("DELETE_RECORD", "ABSENT"),
    "REDACT": ("REDACT_RECORD", "REDACTED"),
    "ANONYMIZE": ("ANONYMIZE_RECORD", "ANONYMIZED"),
}
VERIFICATION_OP = {
    "ABSENT": "VERIFY_RECORD_ABSENT",
    "PRESENT": "VERIFY_RECORD_PRESENT",
    "REDACTED": "VERIFY_RECORD_REDACTED",
    "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED",
    "DISABLED": "VERIFY_DATA_FLOW_DISABLED",
    "BLOCKED": "VERIFY_RESTORE_BLOCKED",
}
FINDING_ORDER = {op: i for i, (op, _) in enumerate(FINDING_OP.values())}
REMEDIATION_ORDER = {op: i for i, (op, _) in enumerate(REMEDIATION_OP.values())}
VERIFICATION_ORDER = {op: i for i, op in enumerate(VERIFICATION_OP.values())}

WINDOW = timedelta(hours=76)


def _violation_kind(fact: Fact, policy, action: str | None = None) -> str | None:
    if not policy.is_violation(fact, "", action):
        return None
    if fact.record_type == "DATA_FLOW":
        return "FLOW"
    if fact.record_type == "BACKUP_ARCHIVE":
        return "RESTORE"
    if action == "ANONYMIZE":
        return "ANONYMIZE_KIND"  # exposed PII finding, anonymize remediation
    if action == "DELETE":
        return "DELETE"
    if action == "REDACT":
        return "REDACT_KIND"  # exposed PII finding, redact remediation
    return None


class CaseAudit:
    def __init__(self, case, identity, policy, evidence_index: "EvidenceIndex",
                 activity_index: ActivityIndex, llm=None):
        self.case = case
        self.identity = identity
        self.policy = policy
        self.evidence = evidence_index
        self.activity = activity_index
        self.llm = llm
        self.audit_dt = parse_ts(case["audit_at"])
        self.raw_facts: list[Fact] = []
        self.account_pseudo_fact: Fact | None = None
        self._collect()
        self._drop_stale_accounts()
        self.facts = self._reconcile()

    # ---- gathering ---------------------------------------------------------

    def _collect(self):
        toks = self.identity.tokens()
        for fact in self.evidence.facts:
            if not (fact.subjects & toks):
                continue
            if not self._eligible(fact):
                continue
            self.raw_facts.append(fact)
        # a resource proven to belong to this person may also appear in other
        # files without identity fields (anonymous rows); attach those so
        # drift and citations are visible
        anchored_keys = {(f.system_id, f.resource_id, f.record_type)
                         for f in self.raw_facts}
        for fact in self.evidence.facts:
            if fact.subjects or not self._eligible(fact):
                continue
            key = (fact.system_id, fact.resource_id, fact.record_type)
            if key in anchored_keys:
                self.raw_facts.append(fact)
        # activity catalog links
        self.raw_facts.extend(self.activity.link_facts(self.identity, self.case["audit_at"]))

    def _eligible(self, fact: Fact) -> bool:
        # The artifact itself must have existed by the audit cutoff. An
        # embedded timestamp cannot make a later-created export admissible.
        if fact.file_id.startswith("evidence_"):
            meta = self.evidence.manifest.get(fact.file_id)
            captured = parse_ts(meta.get("captured_at") or "") if meta else None
            if captured is None or captured > self.audit_dt:
                return False
        when = fact.observed_at or self.evidence.manifest.get(fact.file_id, {}).get("captured_at")
        if not when:
            return True
        dt = parse_ts(when)
        if dt is None:
            return True
        return dt <= self.audit_dt

    # ---- workflow association for unnamed resources -------------------------

    def needed_record_types(self):
        """Record types whose treatment the workflow claimed near the case's
        reporting moment but for which file evidence names no resource for
        this person.

        A non-account type is only fillable when the evidence contains
        anonymous (subject-less) rows of that type in a file tied to this
        user, or the user has no anchored facts at all - the claim then
        attributes one of those rows. USER_PROFILE is always fillable because
        every subject necessarily has an account record.
        """
        from .associate import CLAIM_TYPES, BAND_HOURS, RARE_PHRASES
        have = {f.record_type for f in self.facts}
        case_files = {f.file_id for f in self.raw_facts if f.subjects}
        anonymous_by_file: dict[str, set] = {}
        for f in self.evidence.facts:
            if not f.subjects and self._eligible(f):
                anonymous_by_file.setdefault(f.file_id, set()).add(f.record_type)
        eligible_anon = set()
        for fid in case_files:
            eligible_anon |= anonymous_by_file.get(fid, set())
        needed = set()
        rep_dt = parse_ts(self.case["reported_at"])
        lo = rep_dt - timedelta(hours=BAND_HOURS)
        hi = rep_dt + timedelta(hours=BAND_HOURS)
        for ev in self.activity.worker_events():
            if ev.dt is None or not (lo <= ev.dt <= hi):
                continue
            rare = any(p in ev.details for p in RARE_PHRASES)
            for rtype in CLAIM_TYPES.get(ev.event_type, ()):
                if rtype in have:
                    continue
                if rtype == "USER_PROFILE" or rtype in eligible_anon or rare:
                    needed.add(rtype)
        return needed

    def claim_actions(self) -> dict:
        """resource_id -> workflow-claimed action (delete/redact/anonymize).

        A resource belongs to exactly one person's workflow, so the lookup is
        keyed by resource id across the case's whole lifetime; the claim then
        pins the required treatment even when the workflow's own timing ran
        past the reported_at moment.
        """
        out = {}
        req_dt = parse_ts(self.case["requested_at"])
        lo = req_dt - timedelta(hours=1)
        hi = self.audit_dt
        for ev in self.activity.worker_events():
            if ev.dt is None or not (lo <= ev.dt <= hi):
                continue
            if ev.event_type == "DELETE_RECORD":
                out.setdefault(ev.resource_id, "DELETE")
            elif ev.event_type == "REDACT_RECORD":
                out.setdefault(ev.resource_id, "REDACT")
            elif ev.event_type == "ANONYMIZE_RECORD":
                out.setdefault(ev.resource_id, "ANONYMIZE")
        return out

    def anchored_resources(self) -> set:
        return {f.resource_id for f in self.evidence.facts if f.subjects}

    def file_resource_hints(self) -> set:
        """Resources enumerated by the files that cover this person - the
        readback that captured the case's own records also queried the rest of
        the case's resources, so a workflow claim naming one of them very
        likely belongs to this case."""
        case_files = {f.file_id for f in self.raw_facts if f.subjects}
        hints = set()
        for f in self.evidence.facts:
            if f.file_id in case_files:
                hints.add(f.resource_id)
        for f in self.raw_facts:
            hints.add(f.resource_id)
        return hints

    def excluded_resources(self) -> set:
        """Resources evidence ties to a DIFFERENT person through a fact this
        case could itself have used (observed at/before its audit cutoff).
        Workflow claims naming those resources belong to the other person's
        workflow and must not be associated here. Resources anchored only
        through facts that are time-ineligible for this case stay available -
        the case's own claim may be its only path to its resource id."""
        toks = self.identity.tokens()
        excluded = set()
        for f in self.evidence.facts:
            if not f.subjects or f.resource_id in excluded:
                continue
            if f.subjects & toks:
                continue  # own resource
            if self._eligible(f):
                excluded.add(f.resource_id)
        return excluded

    # ---- reconciliation ------------------------------------------------------

    def _recency(self, fact: Fact):
        when = fact.observed_at or self.evidence.manifest.get(fact.file_id, {}).get("captured_at") or ""
        return when

    def _drop_stale_accounts(self):
        """A person may re-register over the years, leaving several account
        resources behind in old readbacks. When the workflow's own claim window
        explains one of this person's account resources, the other account
        facts for the same identity are stale noise."""
        from .associate import BAND_HOURS
        rep_dt = parse_ts(self.case["reported_at"])
        lo, hi = rep_dt - timedelta(hours=BAND_HOURS), rep_dt + timedelta(hours=BAND_HOURS)
        claimed = set()
        for ev in self.activity.worker_events():
            if ev.dt is None or not (lo <= ev.dt <= hi):
                continue
            if ev.event_type == "DELETE_RECORD" and ev.target_system == "account_service":
                claimed.add(ev.resource_id)
        if not claimed:
            return
        account_facts = [f for f in self.raw_facts if f.record_type == "USER_PROFILE"]
        if not account_facts or not any(f.resource_id in claimed for f in account_facts):
            return
        self.raw_facts = [f for f in self.raw_facts
                          if f.record_type != "USER_PROFILE" or f.resource_id in claimed]

    def _reconcile(self) -> list[Fact]:
        groups: dict[tuple, list[Fact]] = {}
        for f in self.raw_facts:
            groups.setdefault(f.key(), []).append(f)
        out = []
        for key, group in groups.items():
            # the most recent observation at or before the audit wins; when two
            # observations share a timestamp (replication drift between
            # primary and replica captured together), a live copy wins
            def sort_key(f):
                rec = self._recency(f)
                violation = 1 if self.policy.is_violation(f, self.case["audit_at"]) else 0
                citable = 1 if self._manifest_citable(f) else 0
                return (citable, rec, violation, f.file_id)
            best = max(group, key=sort_key)
            if not self._citable(best):
                same_state = [f for f in group
                              if f.status == best.status and self._citable(f)]
                if same_state:
                    best = max(same_state, key=lambda f: (self._recency(f), f.file_id))
            merged = Fact(**{**best.__dict__})
            merged.subjects = frozenset(set().union(*[g.subjects for g in group]))
            out.append(merged)
        return out

    def _manifest_citable(self, fact: Fact) -> bool:
        """Manifest-level citability: the file itself was captured at or
        before the audit cutoff. Workflow/activity ids always qualify."""
        if not fact.file_id.startswith("evidence_"):
            return True
        meta = self.evidence.manifest.get(fact.file_id)
        if not meta:
            return False
        captured = parse_ts(meta.get("captured_at") or "")
        return captured is not None and captured <= self.audit_dt

    def _citable(self, fact: Fact) -> bool:
        """A fact may be cited when it was observed at or before the audit
        cutoff - for event-carrying files (logs, readbacks) the observation
        time governs; the manifest capture time is only the transport
        fallback. Workflow/activity ids are always citable."""
        if self._manifest_citable(fact):
            return True
        if not fact.file_id.startswith("evidence_"):
            return True
        observed = parse_ts(fact.observed_at or "")
        if observed is not None:
            return observed <= self.audit_dt
        return False

    def citable_facts(self):
        return [f for f in self.facts if self._citable(f)]

    # ---- planning --------------------------------------------------------------

    def plan(self) -> tuple[str, list[dict]]:
        verdict, _reason = self._intent()
        if verdict != "AUTHORIZED":
            return "NOT_APPLICABLE", []

        claims = self.claim_actions()

        def effective_action(fact):
            # the case's own workflow claims decide the treatment when they
            # address the resource explicitly
            return claims.get(fact.resource_id) or self.policy.treatment_for(fact.record_type).action

        violations = []
        verifications = []
        for fact in self.facts:
            action = effective_action(fact)
            kind = _violation_kind(fact, self.policy, action)
            if kind:
                violations.append((fact, kind))
            target = self.policy.compliant_status(fact, action)
            verifications.append((fact, target))

        status = "INCOMPLETE" if violations else "COMPLETE"
        steps = []

        for fact, kind in self._ordered(violations):
            if kind in ("DELETE",):
                fop, fstate = FINDING_OP["DELETE"]
            elif kind in ("REDACT_KIND", "ANONYMIZE_KIND"):
                fop, fstate = FINDING_OP["EXPOSED"]
            elif kind == "FLOW":
                fop, fstate = FINDING_OP["FLOW"]
            else:
                fop, fstate = FINDING_OP["RESTORE"]
            steps.append(self._step(len(steps) + 1, "FINDING", fact, fop, fstate, fact.file_id))

        for fact, kind in self._ordered_remediation(violations):
            if kind == "FLOW":
                rop, rstate = REMEDIATION_OP["FLOW"]
            elif kind == "RESTORE":
                rop, rstate = REMEDIATION_OP["RESTORE"]
            elif kind == "DELETE":
                rop, rstate = REMEDIATION_OP["DELETE"]
            elif kind == "REDACT_KIND":
                rop, rstate = REMEDIATION_OP["REDACT"]
            else:
                rop, rstate = REMEDIATION_OP["ANONYMIZE"]
            steps.append(self._step(len(steps) + 1, "REMEDIATION", fact, rop, rstate, ""))

        for fact, target in self._ordered_verification(verifications):
            op = VERIFICATION_OP[target]
            ev = fact.file_id if status == "COMPLETE" else ""
            steps.append(self._step(len(steps) + 1, "VERIFICATION", fact, op, target, ev))

        return status, steps

    def _intent(self):
        from .intent import classify
        fallback = getattr(self.llm, "deletion_intent", None) if self.llm else None
        return classify(self.case.get("request_text", ""), fallback)

    @staticmethod
    def _step(step_no, phase, fact: Fact, operation, state, evidence_id):
        return {
            "step_no": step_no,
            "phase": phase,
            "system_id": fact.system_id,
            "resource_id": fact.resource_id,
            "operation": operation,
            "state": state,
            "evidence_id": evidence_id or "",
        }

    @staticmethod
    def _ordered(violations):
        """Findings order: FINDING operation table order, then system, resource."""
        def key(item):
            fact, kind = item
            if kind == "FLOW":
                fop = FINDING_OP["FLOW"][0]
            elif kind == "RESTORE":
                fop = FINDING_OP["RESTORE"][0]
            elif kind == "DELETE":
                fop = FINDING_OP["DELETE"][0]
            else:
                fop = FINDING_OP["EXPOSED"][0]
            return (FINDING_ORDER[fop], fact.system_id, fact.resource_id)
        return sorted(violations, key=key)

    @staticmethod
    def _ordered_remediation(violations):
        """Remediation order: REMEDIATION operation table order, then system, resource."""
        def key(item):
            fact, kind = item
            if kind == "FLOW":
                rop = REMEDIATION_OP["FLOW"][0]
            elif kind == "RESTORE":
                rop = REMEDIATION_OP["RESTORE"][0]
            elif kind == "DELETE":
                rop = REMEDIATION_OP["DELETE"][0]
            elif kind == "REDACT_KIND":
                rop = REMEDIATION_OP["REDACT"][0]
            else:
                rop = REMEDIATION_OP["ANONYMIZE"][0]
            return (REMEDIATION_ORDER[rop], fact.system_id, fact.resource_id)
        return sorted(violations, key=key)

    @staticmethod
    def _ordered_verification(verifications):
        def key(item):
            fact, target = item
            return (VERIFICATION_ORDER[VERIFICATION_OP[target]], fact.system_id, fact.resource_id)
        return sorted(verifications, key=key)
