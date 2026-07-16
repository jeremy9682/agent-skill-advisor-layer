"""Privacy behavior of the routing-log writer (M-privacy).

Covers the four contract points of the plaintext-minimization change to
scripts/skill_router_hook.py:

  1. default_no_plaintext   -> no prompt_head, hash+len+session pointer only
  2. debug_flag_plaintext   -> plaintext written, carries a +7d ttl_expires
  3. expired_cleanup        -> startup purge strips plaintext from expired debug
  4. legacy_lines_survive   -> old prompt_head lines / non-JSON never crash purge

Each test monkeypatches the module's GOV_DIR/LOG_PATH globals to a tmp dir so
the real ~/.codex/skill-governance log is never touched.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "scripts" / "skill_router_hook.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("skill_router_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    """Module with GOV_DIR/LOG_PATH redirected into an isolated tmp dir."""
    mod = load_hook_module()
    gov = tmp_path / "skill-governance"
    monkeypatch.setattr(mod, "GOV_DIR", gov)
    monkeypatch.setattr(mod, "LOG_PATH", gov / "routing-log.jsonl")
    # Never inherit an ambient debug flag from the runner's environment.
    monkeypatch.delenv(mod.DEBUG_PLAINTEXT_ENV, raising=False)
    return mod


def read_records(mod) -> list[dict]:
    return [
        json.loads(line)
        for line in mod.LOG_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_default_no_plaintext(hook):
    """Default (flag unset): no plaintext; keep hash+len+session pointer."""
    prompt = "帮我 review 这个 PR 的 diff，检查 SQL 注入风险和权限边界"
    hook.write_log(
        prompt,
        "/Users/example/Projects/agent-skill-advisor-layer",
        [{"skill": "review", "score": 4.2}],
        fired=True,
        session_id="sess-abc-123",
        transcript_path="/Users/example/.claude/projects/x/t.jsonl",
    )
    (rec,) = read_records(hook)

    # No plaintext of any kind.
    assert "prompt_head" not in rec
    assert "ttl_expires" not in rec
    assert prompt[:20] not in json.dumps(rec, ensure_ascii=False)

    # Hash + length skeleton retained.
    assert rec["prompt_sha"] == __import__("hashlib").sha256(
        prompt.encode()
    ).hexdigest()[:16]
    assert rec["prompt_len"] == len(prompt)

    # Session pointer threaded through.
    assert rec["session_id"] == "sess-abc-123"
    assert rec["transcript_path"] == "/Users/example/.claude/projects/x/t.jsonl"
    # cwd minimized to basename.
    assert rec["repo"] == "agent-skill-advisor-layer"


def test_default_omits_session_pointer_when_absent(hook):
    """No session_id/transcript_path in stdin -> record omits them entirely."""
    hook.write_log("some prompt text", "/tmp/proj", [], fired=False)
    (rec,) = read_records(hook)
    assert "session_id" not in rec
    assert "transcript_path" not in rec
    assert "prompt_head" not in rec
    assert rec["prompt_len"] == len("some prompt text")


def test_debug_flag_writes_plaintext_with_ttl(hook, monkeypatch):
    """Flag=1: plaintext excerpt present and stamped with a +7d ttl_expires."""
    monkeypatch.setenv(hook.DEBUG_PLAINTEXT_ENV, "1")
    prompt = "x" * 200
    before = datetime.datetime.now()
    hook.write_log(prompt, "/tmp/proj", [], fired=False)
    after = datetime.datetime.now()
    (rec,) = read_records(hook)

    assert rec["prompt_head"] == prompt[:80]
    assert "ttl_expires" in rec
    ttl = datetime.datetime.fromisoformat(rec["ttl_expires"])
    lo = before + datetime.timedelta(days=hook.DEBUG_PLAINTEXT_TTL_DAYS)
    hi = after + datetime.timedelta(days=hook.DEBUG_PLAINTEXT_TTL_DAYS)
    # ttl_expires == write-time + 7 days (allowing a small wall-clock window).
    assert lo - datetime.timedelta(seconds=2) <= ttl <= hi + datetime.timedelta(seconds=2)


def test_debug_flag_other_values_do_not_enable(hook, monkeypatch):
    """Only the exact value "1" enables plaintext; anything else stays off."""
    monkeypatch.setenv(hook.DEBUG_PLAINTEXT_ENV, "true")
    hook.write_log("secret prompt", "/tmp/proj", [], fired=False)
    (rec,) = read_records(hook)
    assert "prompt_head" not in rec
    assert "ttl_expires" not in rec


def test_expired_cleanup_strips_plaintext(hook):
    """Startup purge drops prompt_head/ttl_expires from expired debug records,
    leaves the hash+len skeleton, and never touches non-expired records."""
    hook.GOV_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    expired = {
        "ts": "2026-07-01T00:00:00",
        "prompt_sha": "deadbeef",
        "prompt_len": 42,
        "repo": "proj",
        "fired": True,
        "candidates": [],
        "prompt_head": "SECRET expired prompt text that must be removed",
        "ttl_expires": (now - datetime.timedelta(days=1)).isoformat(timespec="seconds"),
    }
    fresh = {
        "ts": "2026-07-11T00:00:00",
        "prompt_sha": "cafef00d",
        "prompt_len": 7,
        "repo": "proj",
        "fired": False,
        "candidates": [],
        "prompt_head": "still valid plaintext",
        "ttl_expires": (now + datetime.timedelta(days=3)).isoformat(timespec="seconds"),
    }
    default_rec = {"ts": "2026-07-11T01:00:00", "prompt_sha": "abc", "prompt_len": 3}
    hook.LOG_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in (expired, fresh, default_rec)) + "\n",
        encoding="utf-8",
    )

    hook.purge_expired_plaintext()
    recs = read_records(hook)
    assert len(recs) == 3
    exp_rec, fresh_rec, def_rec = recs

    # Expired debug record: plaintext + ttl gone, skeleton intact.
    assert "prompt_head" not in exp_rec
    assert "ttl_expires" not in exp_rec
    assert exp_rec["prompt_sha"] == "deadbeef"
    assert exp_rec["prompt_len"] == 42
    assert "SECRET" not in hook.LOG_PATH.read_text(encoding="utf-8")

    # Non-expired debug record untouched.
    assert fresh_rec["prompt_head"] == "still valid plaintext"
    assert "ttl_expires" in fresh_rec

    # Plain default record untouched.
    assert def_rec == default_rec


def test_legacy_and_malformed_lines_survive_cleanup(hook):
    """Old prompt_head lines (no ttl) and non-JSON lines must not crash the
    purge and must be preserved verbatim; the full parse path is forced by
    including one non-expired ttl record so the cheap early-out is bypassed."""
    hook.GOV_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    legacy = json.dumps(
        {"ts": "2026-01-01T00:00:00", "prompt_sha": "old", "prompt_head": "legacy prompt", "fired": False},
        ensure_ascii=False,
    )
    malformed = "this is not json at all }{"
    fresh_debug = json.dumps(
        {
            "prompt_sha": "new",
            "prompt_len": 4,
            "prompt_head": "keep me",
            "ttl_expires": (now + datetime.timedelta(days=7)).isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )
    original = legacy + "\n" + malformed + "\n" + fresh_debug + "\n"
    hook.LOG_PATH.write_text(original, encoding="utf-8")

    # Must not raise on legacy / malformed content.
    hook.purge_expired_plaintext()

    lines = hook.LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert legacy in lines  # legacy line preserved verbatim
    assert malformed in lines  # malformed line preserved verbatim
    # Non-expired debug line still carries its plaintext + ttl.
    fresh_rec = next(json.loads(l) for l in lines if l.strip().startswith("{") and '"new"' in l)
    assert fresh_rec["prompt_head"] == "keep me"
    assert "ttl_expires" in fresh_rec
