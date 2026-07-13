#!/usr/bin/env python3
"""agent-ledger — checkpoint-event ledger helper (schema: schemas/checkpoint-event.md).

Subcommands:
  open   append a new pending event (10 fields, validated)
  claim  claim a pending event: from_seat becomes owner, target stays open
  close  close a pending event (requires prior same-seat claim unless --instant)
  fold   list open events with current owner

Rules enforced (2026-07-11 dual-model convergence, Fable 5 x gpt-5.6-sol):
  - exactly the 10 schema fields, no more, no fewer
  - exactly ONE transition marker (claimed:/closed:) per record
  - transition records carry next_action "none" and are never themselves open
  - close rejects: missing target, already-closed target, and (without --instant)
    closing with no prior claim by the same seat; a stray closed: marker under
    another intent_ref is ignored by fold, not error-rejected
  - --instant is for immediate read/verify/consume work only: closed: implies claim
  - append is flock-locked; malformed input exits nonzero

Fallback if this helper is unavailable: append one JSON object per line by hand,
following schemas/checkpoint-event.md exactly.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import sys

LEDGER_DIR = os.path.expanduser("~/.agent-ledger")
FIELDS = ["intent_ref", "event_id", "from_seat", "to_seat", "worktree",
          "file_scope", "decided_rejected_open", "verification", "next_action", "taint"]
MARKER_RE = re.compile(r"^(claimed|closed):(evt-\S+?)(\s+—|\s+-|$)")


def die(msg):
    print(f"agent-ledger: error: {msg}", file=sys.stderr)
    sys.exit(1)


# Seat vocabulary: {family}-{role} or a bare principal. Catches the real drill
# failures (e.g. "judgment-claude" — wrong order; "codex-final-review" is fine).
SEAT_RE = re.compile(r"^(?:claude|codex|fable|opus|sonnet|human|founder)"
                     r"(?:-[a-z]+(?:-[a-z]+)*)?$")
# A frozen-intent path is ONE repo-relative path (+optional non-empty #anchor):
# not absolute, not `..`-escaping, no whitespace / '+' / bare-anchor / empty or
# doubled anchor. A narrative string like "docs/a.md §M2 + docs/b.md（@ SHA）" is
# not a contract pointer.
INTENT_RE = re.compile(
    r"^(?![\\/])"                        # not absolute (POSIX)
    r"(?![A-Za-z]:[\\/])"               # not absolute (Windows drive)
    r"(?!\.{1,2}(?:[\\/]|#|$))"         # not a leading ./ ../ . ..
    r"(?!.*[\\/]\.{1,2}(?:[\\/]|#|$))"  # no /../ or /./ segment
    r"[^#\s+\\/]+(?:[\\/][^#\s+\\/]+)*"  # path segments, no whitespace/+/#
    r"(?:#[^#\s+]+)?$"                   # optional single non-empty anchor
)


def _validate_seat(key, val):
    """A transition's seat is caller-supplied input, so claim/close validate it
    too (not just open) — otherwise a bad seat is written by a normal command
    and owner/closure provenance is corrupted (Codex PR#5 review)."""
    if not SEAT_RE.fullmatch(val or ""):
        die(f"{key} {val!r} not in seat vocabulary "
            f"({{claude,codex,fable,opus,sonnet,human,founder}}[-role]); "
            f"e.g. claude-direction / codex-final-review / human")


def _validate_open(ev):
    """Mechanical schema gate on `open` (2026-07-13): the CLI, not prose
    discipline, must reject events that a cold-start receiver cannot act on.
    Returns False (and the caller records a persistent marker) when the escape
    hatch AGENT_LEDGER_SKIP_VALIDATION=1 is set — genuine emergencies only."""
    if os.environ.get("AGENT_LEDGER_SKIP_VALIDATION") == "1":
        print("agent-ledger: warning: field validation SKIPPED "
              "(AGENT_LEDGER_SKIP_VALIDATION=1)", file=sys.stderr)
        return False
    for key in ("from_seat", "to_seat"):
        _validate_seat(key, ev.get(key))
    ir = ev.get("intent_ref") or ""
    if not INTENT_RE.fullmatch(ir):
        die(f"intent_ref {ir!r} must be ONE repo-relative path (+optional "
            f"non-empty #anchor), no whitespace/'+', not absolute/`..`-escaping: "
            f"it is the fold grouping key and the frozen contract, not a "
            f"narrative string")
    wt = ev.get("worktree") or ""
    parts = wt.split(" @ ")
    if len(parts) != 3 or any(not p.strip() for p in parts):
        die(f"worktree {wt!r} must be exactly 'path @ branch @ commit' (3 "
            f"non-empty fields); for cross-seat prefer "
            f"'<fresh-worktree-absolute-path> @ <branch> @ <40-char-SHA>' created "
            f"from origin/<branch>, never a shared mutable checkout")
    na = ev.get("next_action") or ""
    if re.search(r"或| or |、然后|; then ", na):
        print(f"agent-ledger: warning: next_action looks like MULTIPLE actions "
              f"({na[:60]!r}...); schema wants THE single first step",
              file=sys.stderr)
    return True


def ledger_path(slug):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", slug):
        die(f"bad slug {slug!r}: use [A-Za-z0-9._-]")
    return os.path.join(LEDGER_DIR, f"{slug}.jsonl")


def now_id(seat):
    # microseconds: same-second same-seat collisions occurred in real use (2026-07-11)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"evt-{ts}-{seat}"


def load(slug):
    path = ledger_path(slug)
    if not os.path.exists(path):
        return []
    events = []
    with open(path) as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                die(f"{path}:{i} malformed JSON ({e}) — fix by hand before writing")
    return events


def markers(events, intent_ref=None):
    """Transition markers, optionally scoped to one intent_ref (cross-intent
    markers never claim/close a target — 2026-07-11 review fix)."""
    out = []
    for e in events:
        if intent_ref is not None and e.get("intent_ref") != intent_ref:
            continue
        for d in e.get("decided_rejected_open", {}).get("decided", []):
            m = MARKER_RE.match(d)
            if m:
                out.append((m.group(1), m.group(2), e))
    return out


def record_violations(ev):
    """Schema violations of a single record (for load-time screening and fold
    reporting). Returns list of strings, empty if clean."""
    v = []
    if sorted(ev.keys()) != sorted(FIELDS):
        v.append("wrong field set")
        return v
    dro = ev.get("decided_rejected_open", {})
    marks = [d for d in dro.get("decided", []) if MARKER_RE.match(d)]
    if len(marks) > 1:
        v.append("multiple transition markers in one record")
    if ev.get("next_action") == "none" and len(marks) == 0:
        v.append('dead transition: next_action "none" with no claimed:/closed: marker')
    if marks and ev.get("next_action") != "none":
        v.append("transition record with a real next_action")
    return v


def validate(ev):
    if sorted(ev.keys()) != sorted(FIELDS):
        extra = set(ev) - set(FIELDS)
        missing = set(FIELDS) - set(ev)
        die(f"event must have exactly the 10 schema fields (extra={sorted(extra)}, missing={sorted(missing)})")
    fs = ev["file_scope"]
    if not isinstance(fs, dict) or sorted(fs.keys()) != ["do_not_touch", "own"]:
        die("file_scope must be {own: [...], do_not_touch: [...]}")
    dro = ev["decided_rejected_open"]
    if not isinstance(dro, dict) or sorted(dro.keys()) != ["decided", "open", "rejected"]:
        die("decided_rejected_open must be {decided: [...], rejected: [...], open: [...]}")
    if not isinstance(ev["taint"], bool):
        die("taint must be true/false")
    for viol in record_violations(ev):
        die(viol)


class ledger_lock:
    """Exclusive lock over the whole load→check→append critical section."""

    def __init__(self, slug):
        os.makedirs(LEDGER_DIR, exist_ok=True)
        self._fh = open(ledger_path(slug) + ".lock", "w")

    def __enter__(self):
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def append(slug, ev):
    validate(ev)
    path = ledger_path(slug)
    with open(path, "a") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print(ev["event_id"])


def find_target(events, event_id):
    tgt = [e for e in events if e.get("event_id") == event_id]
    if not tgt:
        die(f"target {event_id} not found in ledger")
    # legacy same-second id collisions: prefer the pending record over transitions
    pending = [e for e in tgt if e.get("next_action") != "none"]
    tgt = (pending or tgt)[-1]
    if tgt.get("next_action") == "none":
        die(f"target {event_id} is a transition record, not a pending event")
    for kind, tid, _ in markers(events, intent_ref=tgt.get("intent_ref")):
        if kind == "closed" and tid == event_id:
            die(f"target {event_id} is already closed")
    return tgt


def owner_of(events, event_id, default, intent_ref=None):
    own = default
    for kind, tid, rec in markers(events, intent_ref=intent_ref):
        if kind == "claimed" and tid == event_id:
            own = rec.get("from_seat", own)  # append order: latest valid claim wins
    return own


def cmd_open(a):
    with ledger_lock(a.slug):
        _cmd_open_locked(a)


def _cmd_open_locked(a):
    ev = {
        "intent_ref": a.intent_ref,
        "event_id": now_id(a.from_seat),
        "from_seat": a.from_seat,
        "to_seat": a.to_seat,
        "worktree": a.worktree,
        "file_scope": {"own": a.own, "do_not_touch": a.do_not_touch},
        "decided_rejected_open": {"decided": a.decided, "rejected": a.rejected, "open": a.open_q},
        "verification": a.verification,
        "next_action": a.next_action,
        "taint": a.taint,
    }
    if ev["next_action"] == "none":
        die('a pending event needs a real next_action (use claim/close for transitions)')
    if not _validate_open(ev):
        # escape hatch used — leave a persistent, auditable marker in the record
        ev["decided_rejected_open"]["decided"].append(
            "validation-skipped: AGENT_LEDGER_SKIP_VALIDATION=1")
    append(a.slug, ev)


def cmd_claim(a):
    _validate_seat("seat", a.seat)  # transition writes a NEW seat → gate it too
    with ledger_lock(a.slug):
        _cmd_claim_locked(a)


def _cmd_claim_locked(a):
    events = load(a.slug)
    tgt = find_target(events, a.event_id)
    ev = {
        "intent_ref": tgt["intent_ref"],
        "event_id": now_id(a.seat),
        "from_seat": a.seat,
        "to_seat": a.seat,
        "worktree": tgt["worktree"],
        "file_scope": tgt["file_scope"],
        "decided_rejected_open": {
            "decided": [f"claimed:{a.event_id}" + (f" — {a.note}" if a.note else "")],
            "rejected": [], "open": []},
        "verification": tgt["verification"],
        "next_action": "none",
        "taint": a.taint,
    }
    append(a.slug, ev)


def cmd_close(a):
    _validate_seat("seat", a.seat)  # transition writes a NEW seat → gate it too
    with ledger_lock(a.slug):
        _cmd_close_locked(a)


def _cmd_close_locked(a):
    events = load(a.slug)
    tgt = find_target(events, a.event_id)
    if not a.instant:
        claimed_by_me = any(k == "claimed" and t == a.event_id and r.get("from_seat") == a.seat
                            for k, t, r in markers(events, intent_ref=tgt.get("intent_ref")))
        if not claimed_by_me:
            die(f"no prior claim on {a.event_id} by seat {a.seat!r}; being the addressed "
                f"to_seat is not a claim — run `agent-ledger claim` first, or use --instant "
                f"only for immediate read/verify/consume work")
    ev = {
        "intent_ref": tgt["intent_ref"],
        "event_id": now_id(a.seat),
        "from_seat": a.seat,
        "to_seat": tgt.get("from_seat", "human"),
        "worktree": tgt["worktree"],
        "file_scope": tgt["file_scope"],
        "decided_rejected_open": {
            "decided": [f"closed:{a.event_id} — {a.outcome}"]
                        + (["mode:instant"] if a.instant else []),
            "rejected": [], "open": []},
        "verification": tgt["verification"],
        "next_action": "none",
        "taint": a.taint,
    }
    append(a.slug, ev)


def cmd_fold(a):
    events = load(a.slug)
    if not events:
        print(f"(no ledger or empty: {ledger_path(a.slug)})")
        return
    for i, e in enumerate(events):
        for viol in record_violations(e):
            print(f"VIOLATION line {i + 1} ({e.get('event_id', '?')}): {viol}")
    open_events = []
    for e in events:
        if e.get("next_action") == "none":
            continue
        closed = any(k == "closed" and t == e.get("event_id")
                     for k, t, _ in markers(events, intent_ref=e.get("intent_ref")))
        if not closed:
            open_events.append(e)
    if not open_events:
        print("no open events — ledger clean")
        return
    for e in open_events:
        owner = owner_of(events, e["event_id"], e.get("to_seat"),
                         intent_ref=e.get("intent_ref"))
        print(f"OPEN {e['event_id']}  owner={owner}")
        print(f"     next_action: {e.get('next_action')}")
        print(f"     worktree:    {e.get('worktree')}")
        print(f"     verify:      {e.get('verification')}")


def main():
    p = argparse.ArgumentParser(prog="agent-ledger", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("open", help="append a new pending event")
    po.add_argument("slug")
    po.add_argument("--intent-ref", required=True)
    po.add_argument("--from-seat", required=True)
    po.add_argument("--to-seat", required=True)
    po.add_argument("--worktree", required=True)
    po.add_argument("--own", nargs="*", default=[])
    po.add_argument("--do-not-touch", nargs="*", default=[])
    po.add_argument("--decided", nargs="*", default=[])
    po.add_argument("--rejected", nargs="*", default=[])
    po.add_argument("--open-q", nargs="*", default=[])
    po.add_argument("--verification", required=True)
    po.add_argument("--next-action", required=True)
    po.add_argument("--taint", action="store_true")
    po.set_defaults(func=cmd_open)

    pc = sub.add_parser("claim", help="claim a pending event (before mutation/dispatch/handoff)")
    pc.add_argument("slug")
    pc.add_argument("event_id")
    pc.add_argument("--seat", required=True)
    pc.add_argument("--note", default="")
    pc.add_argument("--taint", action="store_true")
    pc.set_defaults(func=cmd_claim)

    px = sub.add_parser("close", help="close a pending event")
    px.add_argument("slug")
    px.add_argument("event_id")
    px.add_argument("--seat", required=True)
    px.add_argument("--outcome", required=True)
    px.add_argument("--instant", action="store_true",
                    help="immediate read/verify/consume work: closed implies claim")
    px.add_argument("--taint", action="store_true")
    px.set_defaults(func=cmd_close)

    pf = sub.add_parser("fold", help="list open events with owner")
    pf.add_argument("slug")
    pf.set_defaults(func=cmd_fold)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
