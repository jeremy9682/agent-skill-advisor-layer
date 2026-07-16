"""Mechanical `open` field gate for agent_ledger.py (added 2026-07-13).

The tool enforces the checkpoint schema at write time so a low-tier session
cannot silently write an event a cold-start receiver can't act on. These tests
pin the drill-discovered violation classes (a producer seat wrote every one of
them and then over-claimed the event as compliant) and the escape hatch.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "scripts" / "agent_ledger.py"
SLUG = "zz-gate-pytest"


def _load():
    spec = importlib.util.spec_from_file_location("agent_ledger", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AL = _load()


def _ev(**over):
    ev = {
        "intent_ref": "docs/x.md",
        "from_seat": "claude-direction",
        "to_seat": "human",
        "worktree": "/p @ main @ abc123",
        "next_action": "do the single thing",
    }
    ev.update(over)
    return ev


def _expect_die(ev):
    with pytest.raises(SystemExit):
        AL._validate_open(ev)


# --- seat vocabulary ------------------------------------------------------- #
def test_out_of_vocab_seat_rejected():
    _expect_die(_ev(from_seat="judgment-claude"))  # wrong order — a real drill bug
    _expect_die(_ev(to_seat="reviewer"))
    _expect_die(_ev(from_seat="codex-final-"))  # trailing hyphen / empty role


def test_family_role_seats_accepted():
    for s in ("claude-direction", "codex-final-review", "codex-review", "human",
              "founder", "fable-review", "codex-landing"):
        AL._validate_open(_ev(from_seat=s))  # must not raise


# --- intent_ref is one path, not a narrative ------------------------------- #
def test_narrative_intent_ref_rejected():
    _expect_die(_ev(intent_ref="docs/a.md §M2 + docs/b.md（@ SHA）"))
    _expect_die(_ev(intent_ref="two words"))


def test_malformed_intent_ref_rejected():
    _expect_die(_ev(intent_ref="/abs/x.md"))       # absolute
    _expect_die(_ev(intent_ref="../outside.md"))    # escapes repo
    _expect_die(_ev(intent_ref="docs/../x.md"))     # embedded ..
    _expect_die(_ev(intent_ref="#anchor"))          # bare anchor, no path
    _expect_die(_ev(intent_ref="docs/x.md#"))       # empty anchor
    _expect_die(_ev(intent_ref="docs/x.md##a"))     # doubled anchor


def test_single_path_intent_ref_accepted():
    AL._validate_open(_ev(intent_ref="docs/plans/active/045.md"))
    AL._validate_open(_ev(intent_ref="docs/plans/045.md#m2"))


# --- worktree must be path @ branch @ commit ------------------------------- #
def test_worktree_without_shape_rejected():
    _expect_die(_ev(worktree="从 origin/main@abc 新建 worktree"))  # no ' @ '
    _expect_die(_ev(worktree="/p @ main"))  # only one separator
    _expect_die(_ev(worktree=" @ feat/x @ deadbeef"))    # empty path segment
    _expect_die(_ev(worktree="/tmp/wt @  @ deadbeef"))   # empty branch segment
    _expect_die(_ev(worktree="/tmp/wt @ feat/x @ "))     # empty commit segment
    _expect_die(_ev(worktree="/w @ b @ c @ extra"))      # 4th segment


def test_worktree_shape_accepted():
    AL._validate_open(_ev(worktree="/private/tmp/wt-x @ main @ 1395037bd8b2 (note)"))


# --- next_action ambiguity warns (does not block) -------------------------- #
def test_multi_action_next_warns_not_blocks(capsys):
    AL._validate_open(_ev(next_action="出 spec 或 按指示"))  # must not raise
    assert "MULTIPLE actions" in capsys.readouterr().err


# --- escape hatch: skips validation but leaves a persistent marker --------- #
def test_escape_hatch_returns_false_and_warns(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LEDGER_SKIP_VALIDATION", "1")
    assert AL._validate_open(_ev(from_seat="anything", worktree="junk")) is False
    assert "SKIPPED" in capsys.readouterr().err


def _run(args, home):
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env)


# --- end-to-end (isolated HOME) -------------------------------------------- #
def test_cli_open_blocks_bad_event_end_to_end(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    ledger = home / ".agent-ledger" / f"{SLUG}.jsonl"
    r = _run(["open", SLUG, "--intent-ref", "narrative + string",
              "--from-seat", "judgment-claude", "--to-seat", "human",
              "--worktree", "bad", "--verification", "v", "--next-action", "n"],
             home)
    assert r.returncode == 1
    assert "seat vocabulary" in r.stderr
    assert not ledger.exists(), "a rejected open must not write to the ledger"


def test_cli_claim_and_close_gate_seat(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # a valid open first
    o = _run(["open", SLUG, "--intent-ref", "docs/x.md", "--from-seat",
              "claude-direction", "--to-seat", "codex-review", "--worktree",
              "/w @ b @ c", "--verification", "v", "--next-action", "step"], home)
    assert o.returncode == 0
    eid = o.stdout.strip()
    # claim with a bad seat must be rejected (the transition writes a NEW seat)
    c = _run(["claim", SLUG, eid, "--seat", "judgment-claude"], home)
    assert c.returncode == 1 and "seat vocabulary" in c.stderr
    # claim with a good seat succeeds
    c2 = _run(["claim", SLUG, eid, "--seat", "codex-review"], home)
    assert c2.returncode == 0
    # close with a bad seat must be rejected
    cl = _run(["close", SLUG, eid, "--seat", "reviewer", "--outcome", "x"], home)
    assert cl.returncode == 1 and "seat vocabulary" in cl.stderr


def test_cli_escape_hatch_writes_persistent_marker(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["AGENT_LEDGER_SKIP_VALIDATION"] = "1"
    r = subprocess.run(
        [sys.executable, str(CLI), "open", SLUG, "--intent-ref", "junk ref",
         "--from-seat", "whatever", "--to-seat", "human", "--worktree", "junk",
         "--verification", "v", "--next-action", "n"],
        capture_output=True, text=True, env=env)
    assert r.returncode == 0  # skipped, so it writes
    line = (home / ".agent-ledger" / f"{SLUG}.jsonl").read_text().strip()
    rec = json.loads(line)
    assert any("validation-skipped" in d
               for d in rec["decided_rejected_open"]["decided"]), \
        "a bypassed write must leave a persistent auditable marker"


def test_cli_close_requires_latest_claimant(tmp_path):
    """A superseded (stale) claimant must not close; only the latest claimant may."""
    home = tmp_path / "home"
    home.mkdir()
    o = _run(["open", SLUG, "--intent-ref", "docs/x.md", "--from-seat",
              "claude-direction", "--to-seat", "codex-review", "--worktree",
              "/w @ b @ c", "--verification", "v", "--next-action", "step"], home)
    assert o.returncode == 0
    eid = o.stdout.strip()
    assert _run(["claim", SLUG, eid, "--seat", "codex-review"], home).returncode == 0
    # takeover: a later claim supersedes the first
    assert _run(["claim", SLUG, eid, "--seat", "claude-landing"], home).returncode == 0
    stale = _run(["close", SLUG, eid, "--seat", "codex-review", "--outcome", "done"], home)
    assert stale.returncode == 1 and "stale claim" in stale.stderr
    ok = _run(["close", SLUG, eid, "--seat", "claude-landing", "--outcome", "done"], home)
    assert ok.returncode == 0
