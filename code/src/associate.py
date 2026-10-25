"""Workflow-claim association: recover resource ids the file evidence never names.

Every deletion request drives a workflow whose claimed actions appear in
system_activity.csv, and all of a case's own deletion-worker claims execute in
the run-up to the workflow's report: the claim times sit within roughly
+/-1.25 hours of the case's reported_at. Those claims name resources (account,
analytics profile, tombstone, ...) that snapshots often show without any
identity attached, so the person <-> resource link can only come from the
workflow itself.

Association therefore runs as a series of global one-to-one matchings (one per
record type) over all cases at once:

- a case participates for a record type only when (a) the case's identity has
  no evidence-anchored fact of that type and (b) the workflow claimed an action
  of that type near the case's reported_at (and, for non-account types, the
  evidence contains anonymous rows of that type the claim can attribute);
- candidate events are deletion-worker claims whose resource is anchored to
  nobody, inside the case's claim window;
- cost is the distance from the claim time to the case's reported_at, so each
  claim naturally goes to the case that reported it; clustered claims (several
  cases reporting at the same moment) are distributed by the minimum-cost
  assignment.
"""
from __future__ import annotations

from .activity import parse_ts

BAND_HOURS = 2.0          # how far before the report a claim may run
BAND_AFTER_HOURS = 0.15   # claims never land meaningfully after the report
HINT_BONUS = 0.0          # ablation showed a small step-F1 improvement
STATUS_MISMATCH_PENALTY = 0.30  # weak signal: claim result vs reported status

# rare "final outcome" phrasings - events worded this way are always genuine
# case-workflow claims in the observed corpus
RARE_PHRASES = (
    "reported the requested operation as completed",
    "stopped after the target system returned an error",
    "completed some internal work but could not confirm the final state",
)

# workflow claim type -> record types it addresses
CLAIM_TYPES = {
    "DELETE_RECORD": ("USER_PROFILE", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
                      "USER_EXPORT", "SUPPORT_ATTACHMENT", "MARKETING_PROFILE"),
    "REDACT_RECORD": ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE", "EMAIL_DELIVERY_LOG"),
    "ANONYMIZE_RECORD": ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"),
    "DISABLE_DATA_FLOW": ("DATA_FLOW",),
    "BLOCK_RESTORE": ("BACKUP_ARCHIVE",),
}

# compliant target status for a workflow-associated resource (the workflow
# addressed it and no evidence shows a violation)
ASSOCIATED_STATUS = {
    "USER_PROFILE": "ABSENT",
    "SEARCH_INDEX_ENTRY": "ABSENT",
    "CACHE_ENTRY": "ABSENT",
    "USER_EXPORT": "ABSENT",
    "SUPPORT_ATTACHMENT": "ABSENT",
    "MARKETING_PROFILE": "ABSENT",
    "SUPPORT_MESSAGE": "REDACTED",
    "ACCOUNT_TOMBSTONE": "REDACTED",
    "EMAIL_DELIVERY_LOG": "REDACTED",
    "ANALYTICS_PROFILE": "ANONYMIZED",
    "PRODUCT_TELEMETRY": "ANONYMIZED",
    "DATA_FLOW": "DISABLED",
    "BACKUP_ARCHIVE": "BLOCKED",
}


def claim_event_types(record_type: str):
    return {et for et, types in CLAIM_TYPES.items() if record_type in types}


def associate_records(cases, activity_index, anchored_resources=None):
    """cases: {case_id: {"needed": {rtype,...}, "reported_dt", "excluded"}}
    Returns {case_id: {record_type: (resource_id, activity_id)}}.
    """
    from .facts import record_type_for

    all_types = set()
    for meta in cases.values():
        all_types |= meta.get("needed", set())

    events_by_type: dict[str, list] = {}
    for ev in activity_index.worker_events():
        if ev.dt is None:
            continue
        rtype = record_type_for(ev.resource_id)
        if rtype is None:
            continue
        events_by_type.setdefault(rtype, []).append(ev)

    assignment: dict[str, dict[str, tuple[str, str]]] = {}
    for rtype in sorted(all_types):
        etypes = claim_event_types(rtype)
        edges = []
        for cid, meta in cases.items():
            if rtype not in meta.get("needed", set()):
                continue
            rep_dt = meta["reported_dt"]
            hints = meta.get("hints") or set()
            excluded = meta.get("excluded") or set()
            for ev in events_by_type.get(rtype, ()):
                if ev.event_type not in etypes:
                    continue
                # A resource evidence ties to a DIFFERENT person through a
                # fact this case could itself have used belongs to that
                # person's workflow. Resources anchored only through facts
                # that are time-ineligible for this case stay available: the
                # case's own claim may be its only path to its resource id.
                if ev.resource_id in excluded:
                    continue
                delta_h = (rep_dt - ev.dt).total_seconds() / 3600.0
                # claims run up to the report; a claim after the report is not
                # this workflow's (one-sided window)
                if delta_h < -BAND_AFTER_HOURS or delta_h > BAND_HOURS:
                    continue
                cost = abs(delta_h)
                if ev.resource_id in hints:
                    cost = max(0.0, cost - HINT_BONUS)
                # A workflow result is not evidence of resource state, but it
                # is a useful weak signal when assigning clustered worker
                # claims to otherwise indistinguishable requests.
                status = meta.get("reported_status", "")
                if status == "COMPLETE" and ev.reported_result != "SUCCESS":
                    cost += STATUS_MISMATCH_PENALTY
                elif status == "PARTIALLY_COMPLETE" and ev.reported_result == "SUCCESS":
                    cost += STATUS_MISMATCH_PENALTY
                edges.append((cost, ev.event_time, ev.activity_id, cid, ev))

        if not edges:
            continue
        edges.sort(key=lambda e: (e[0], e[1], e[2]))
        by_case: dict[str, list[tuple[object, float]]] = {}
        for cost, _t, _aid, cid, ev in edges:
            by_case.setdefault(cid, []).append((ev, cost))

        match_case, _match_event = _min_cost_assignment(
            by_case, rtype, {cid: (meta.get("hints") or set()) for cid, meta in cases.items()})
        for cid, ev in match_case.items():
            assignment.setdefault(cid, {})[rtype] = (ev.resource_id, ev.activity_id)
    return assignment


def _case_num(cid: str) -> int:
    try:
        return int(str(cid).rsplit("_", 1)[1])
    except Exception:
        return 0


def _min_cost_assignment(by_case, claim_record_type="", meta_hints=None):
    """Exact minimum-cost one-to-one assignment per connected component.

    Components are small (a handful of cases clustered around one reporting
    moment), so exhaustive search over each case's top candidates is both
    exact and fast. Cases are visited in numeric id order and candidates by
    cost, which makes the first optimum found the deterministic one. Wide-band
    record types can chain hundreds of cases into one component - those fall
    back to a cheapest-edge-first greedy with the same ordering principles.
    """
    parent: dict[str, str] = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for cid, cands in by_case.items():
        parent.setdefault(cid, cid)
        for ev, _c in cands:
            key = "ev:" + ev.activity_id
            parent.setdefault(key, key)
            union(cid, key)

    components: dict[str, list[str]] = {}
    for cid in sorted(by_case, key=_case_num):
        components.setdefault(find(cid), []).append(cid)

    match_case: dict[str, object] = {}
    match_event: dict[str, str] = {}
    for _root, cids in components.items():
        cids = sorted(cids, key=_case_num)
        if len(cids) > 12:
            match_case.update(_greedy_assignment(cids, by_case))
            continue
        cand_map = {cid: sorted(by_case[cid], key=lambda x: (x[1], x[0].activity_id))[:8]
                    for cid in cids}
        best_key, best_assign = None, None
        used: set = set()

        def rec(i, total, assign):
            nonlocal best_key, best_assign
            if best_key is not None and round(total, 2) > best_key[0]:
                return
            if i == len(cids):
                key = (round(total, 2), _tiebreak_total(assign, cids))
                if best_key is None or key < best_key:
                    best_key, best_assign = key, dict(assign)
                return
            cid = cids[i]
            for ev, c in cand_map[cid]:
                if ev.activity_id in used:
                    continue
                used.add(ev.activity_id)
                assign[cid] = (ev, c)
                rec(i + 1, total + c, assign)
                assign.pop(cid, None)
                used.discard(ev.activity_id)

        rec(0, 0.0, {})
        for cid, entry in (best_assign or {}).items():
            if entry is None:
                continue
            ev = entry[0] if isinstance(entry, tuple) else entry
            match_case[cid] = ev
            match_event[ev.activity_id] = cid
    return match_case, match_event


def _tiebreak_total(assign, cids):
    """Deterministic preference when assignment costs tie: the lower activity
    id goes to the earlier case. Encoded so it only matters between
    assignments whose rounded totals are equal."""
    total = 0.0
    for idx, cid in enumerate(cids):
        entry = assign.get(cid)
        if entry is None:
            continue
        ev = entry[0] if isinstance(entry, tuple) else entry
        try:
            num = int(ev.activity_id.split("_")[1])
        except Exception:
            num = 0
        total += 1e-9 * idx * num
        total += 1e-15 * num
    return total


def _greedy_assignment(cids, by_case):
    """Cheapest-edge-first greedy for components too large to search exactly.

    Edges are ordered by (cost, claim time, activity id, case number) so
    hinted and tightly-timed claims win, and ties resolve toward the lowest
    case number.
    """
    edges = []
    for cid in cids:
        for ev, cost in by_case[cid]:
            edges.append((cost, ev.event_time, ev.activity_id, _case_num(cid), cid, ev))
    edges.sort(key=lambda e: e[:5])
    match_case: dict[str, object] = {}
    taken: set = set()
    for _cost, _t, _aid, _cn, cid, ev in edges:
        if cid in match_case or ev.activity_id in taken:
            continue
        match_case[cid] = ev
        taken.add(ev.activity_id)
    return match_case
