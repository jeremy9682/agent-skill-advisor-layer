"""Pure checkpoint-ledger validation shared by the CLI and provider wrapper."""

from __future__ import annotations

import re


FIELDS = [
    "intent_ref", "event_id", "from_seat", "to_seat", "worktree",
    "file_scope", "decided_rejected_open", "verification", "next_action", "taint",
]
MARKER_RE = re.compile(r"^(claimed|closed):(evt-\S+?)(?:\s+(?:—|-)\s*.*)?$")


def parse_marker(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = MARKER_RE.fullmatch(value)
    if not match:
        return None
    return match.group(1), match.group(2)


def markers(events: list[dict], intent_ref: str | None = None) -> list[tuple[str, str, dict]]:
    out: list[tuple[str, str, dict]] = []
    for event in events:
        if intent_ref is not None and event.get("intent_ref") != intent_ref:
            continue
        decided = event.get("decided_rejected_open", {}).get("decided", [])
        for value in decided if isinstance(decided, list) else []:
            marker = parse_marker(value)
            if marker:
                out.append((marker[0], marker[1], event))
    return out


def record_violations(event: object) -> list[str]:
    if not isinstance(event, dict):
        return ["record is not an object"]
    violations: list[str] = []
    if set(event) != set(FIELDS):
        return ["wrong field set"]
    for key in (
        "intent_ref", "event_id", "from_seat", "to_seat", "worktree",
        "verification", "next_action",
    ):
        if not isinstance(event[key], str):
            violations.append(f"{key} must be a string")
    if not isinstance(event["taint"], bool):
        violations.append("taint must be true/false")

    scope = event["file_scope"]
    if not isinstance(scope, dict) or set(scope) != {"own", "do_not_touch"}:
        violations.append("file_scope must be {own: [...], do_not_touch: [...]}")
    elif not all(
        isinstance(scope[key], list) and all(isinstance(item, str) for item in scope[key])
        for key in ("own", "do_not_touch")
    ):
        violations.append("file_scope values must be string arrays")

    dro = event["decided_rejected_open"]
    if not isinstance(dro, dict) or set(dro) != {"decided", "rejected", "open"}:
        violations.append(
            "decided_rejected_open must be {decided: [...], rejected: [...], open: [...]}"
        )
        return violations
    if not all(
        isinstance(dro[key], list) and all(isinstance(item, str) for item in dro[key])
        for key in ("decided", "rejected", "open")
    ):
        violations.append("decided_rejected_open values must be string arrays")
        return violations

    parsed = [parse_marker(value) for value in dro["decided"]]
    malformed = [
        value for value, marker in zip(dro["decided"], parsed)
        if value.startswith(("claimed:", "closed:")) and marker is None
    ]
    if malformed:
        violations.append("malformed transition marker")
    transition_count = sum(marker is not None for marker in parsed)
    if transition_count > 1:
        violations.append("multiple transition markers in one record")
    if event["next_action"] == "none" and transition_count == 0:
        violations.append('dead transition: next_action "none" with no claimed:/closed: marker')
    if transition_count and event["next_action"] != "none":
        violations.append("transition record with a real next_action")
    return violations


def ledger_violations(events: list[dict]) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    pending_by_id: dict[str, list[tuple[int, dict]]] = {}
    closed: set[tuple[str, str]] = set()

    for index, event in enumerate(events, start=1):
        row_violations = record_violations(event)
        violations.extend((index, message) for message in row_violations)
        if row_violations:
            continue
        row_markers = markers([event])
        if not row_markers:
            pending_by_id.setdefault(event["event_id"], []).append((index, event))
            continue

        kind, target_id, _record = row_markers[0]
        candidates = [
            (target_index, target)
            for target_index, target in pending_by_id.get(target_id, [])
            if target_index < index and target["intent_ref"] == event["intent_ref"]
        ]
        if not candidates:
            other_intent = any(
                target_index < index and target["intent_ref"] != event["intent_ref"]
                for target_index, target in pending_by_id.get(target_id, [])
            )
            reason = "cross-intent transition target" if other_intent else "transition target is not a prior pending event"
            violations.append((index, reason))
            continue
        key = (event["intent_ref"], target_id)
        if key in closed:
            violations.append((index, "transition occurs after target closure"))
            continue
        if kind == "closed":
            closed.add(key)
    return violations


def checkpoint_state(events: list[dict], event_id: str) -> dict:
    violations = ledger_violations(events)
    if violations:
        index, message = violations[0]
        raise ValueError(f"row {index}: {message}")
    candidates = [
        event for event in events
        if event["event_id"] == event_id and not markers([event])
    ]
    if not candidates:
        raise LookupError(f"checkpoint event not found or not pending: {event_id}")
    target = candidates[-1]
    active = False
    owner = "unknown"
    for kind, target_id, record in markers(events, intent_ref=target["intent_ref"]):
        if target_id != event_id:
            continue
        if kind == "claimed":
            active = True
            owner = str(record.get("from_seat") or record.get("to_seat") or "unknown")
        else:
            active = False
    return {"found": True, "active": active, "owner": owner, "intent_ref": target["intent_ref"]}
