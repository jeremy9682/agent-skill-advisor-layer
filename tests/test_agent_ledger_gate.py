"""Mechanical `open` field gate for agent_ledger.py (added 2026-07-13).

The tool enforces the checkpoint schema at write time so a low-tier session
cannot silently write an event a cold-start receiver can't act on. These tests
pin the drill-discovered violation classes (a producer seat wrote every one of
them and then over-claimed the event as compliant) and the escape hatch.
"""
import importlib.util
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


def test_family_role_seats_accepted():
    for s in ("claude-direction", "codex-final-review", "codex-review", "human",
              "founder", "fable-review", "codex-landing"):
        AL._validate_open(_ev(from_seat=s))  # must not raise


# --- intent_ref is one path, not a narrative ------------------------------- #
def test_narrative_intent_ref_rejected():
    _expect_die(_ev(intent_ref="docs/a.md §M2 + docs/b.md（@ SHA）"))
    _expect_die(_ev(intent_ref="two words"))


def test_single_path_intent_ref_accepted():
    AL._validate_open(_ev(intent_ref="docs/plans/active/045.md"))
    AL._validate_open(_ev(intent_ref="docs/plans/045.md#m2"))


# --- worktree must be path @ branch @ commit ------------------------------- #
def test_worktree_without_shape_rejected():
    _expect_die(_ev(worktree="从 origin/main@abc 新建 worktree"))  # no ' @ '
    _expect_die(_ev(worktree="/p @ main"))  # only one separator


def test_worktree_shape_accepted():
    AL._validate_open(_ev(worktree="origin/main @ main @ 1395037bd8b2 (note)"))


# --- next_action ambiguity warns (does not block) -------------------------- #
def test_multi_action_next_warns_not_blocks(capsys):
    AL._validate_open(_ev(next_action="出 spec 或 按指示"))  # must not raise
    assert "MULTIPLE actions" in capsys.readouterr().err


# --- escape hatch ---------------------------------------------------------- #
def test_escape_hatch_skips(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LEDGER_SKIP_VALIDATION", "1")
    AL._validate_open(_ev(from_seat="anything", worktree="junk"))  # must not raise
    assert "SKIPPED" in capsys.readouterr().err


# --- end-to-end: the CLI `open` subcommand actually blocks a bad write ------ #
def test_cli_open_blocks_bad_event_end_to_end():
    ledger = Path(os.path.expanduser("~/.agent-ledger")) / f"{SLUG}.jsonl"
    if ledger.exists():
        ledger.unlink()
    r = subprocess.run(
        [sys.executable, str(CLI), "open", SLUG,
         "--intent-ref", "narrative + string", "--from-seat", "judgment-claude",
         "--to-seat", "human", "--worktree", "bad", "--verification", "v",
         "--next-action", "n"],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "seat vocabulary" in r.stderr
    assert not ledger.exists(), "a rejected open must not write to the ledger"
