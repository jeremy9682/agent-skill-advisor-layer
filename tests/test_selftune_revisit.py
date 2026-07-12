from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_selftune():
    path = ROOT / "scripts" / "router_selftune.py"
    spec = importlib.util.spec_from_file_location("router_selftune", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revisit_counts_consecutive_iso_weeks(tmp_path, monkeypatch):
    m = load_selftune()
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.jsonl")
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 4)
    # four clean runs on Mondays of four consecutive ISO weeks → met on the 4th
    for d in ["2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27"]:
        r = m.revisit_tracker(d, green=True, attractor_count=0, thin=False)
    assert r["streak"] == 4 and r["met"]


def test_revisit_daily_runs_do_not_fake_weeks(tmp_path, monkeypatch):
    # THE MAJOR FIX: running clean four days in a row is ONE ISO week, not four.
    m = load_selftune()
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.jsonl")
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 4)
    for d in ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]:  # all week W28
        r = m.revisit_tracker(d, green=True, attractor_count=0, thin=False)
    assert r["weeks_recorded"] == 1
    assert r["streak"] == 1 and not r["met"]


def test_revisit_calendar_gap_breaks_streak(tmp_path, monkeypatch):
    # A missing week (watchdog didn't run) must break the consecutive streak.
    m = load_selftune()
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.jsonl")
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 3)
    m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)   # W28
    m.revisit_tracker("2026-07-13", green=True, attractor_count=0, thin=False)   # W29
    # skip W30 entirely, jump to W31
    r = m.revisit_tracker("2026-07-27", green=True, attractor_count=0, thin=False)  # W31
    assert r["streak"] == 1  # gap reset it; only W31 counts


def test_revisit_not_clean_week_resets(tmp_path, monkeypatch):
    m = load_selftune()
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.jsonl")
    m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)
    r = m.revisit_tracker("2026-07-13", green=True, attractor_count=2, thin=False)  # attractors
    assert r["streak"] == 0


def test_revisit_thin_data_is_not_clean(tmp_path, monkeypatch):
    m = load_selftune()
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.jsonl")
    r = m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=True)
    assert r["clean"] is False and r["streak"] == 0


def test_revisit_same_week_rerun_dedupes(tmp_path, monkeypatch):
    m = load_selftune()
    monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.jsonl")
    m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)  # Mon W28
    m.revisit_tracker("2026-07-09", green=True, attractor_count=0, thin=False)  # Thu W28
    lines = [l for l in (tmp_path / "status.jsonl").read_text().splitlines() if l.strip()]
    assert len(lines) == 1  # same ISO week rewritten, not appended


def test_revisit_corrupt_line_fails_closed(tmp_path, monkeypatch):
    # THE MAJOR FIX: an unreadable status file must not let 'met' be claimed.
    m = load_selftune()
    status = tmp_path / "status.jsonl"
    monkeypatch.setattr(m, "STATUS_PATH", status)
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 1)
    status.write_text("{ not valid json\n")
    r = m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)
    assert r["error"] is not None
    assert r["met"] is False  # would be streak>=1 but error forces not-met


def test_revisit_write_failure_fails_closed(tmp_path, monkeypatch):
    # THE MAJOR FIX (persist side): if the status file cannot be written, the
    # streak cannot be trusted — even a would-be-met run must report met=False.
    m = load_selftune()
    status = tmp_path / "status.jsonl"
    monkeypatch.setattr(m, "STATUS_PATH", status)
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 1)

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(m.Path, "write_text", boom)

    r = m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)
    assert r["error"] is not None
    assert r["met"] is False   # streak would be 1 >= 1, but write failed → not trusted


def test_revisit_read_failure_fails_closed(tmp_path, monkeypatch):
    # ROUND-4 FIX: a status file that EXISTS but is unreadable (permissions)
    # must not crash the report — it fails closed (error set, met=False).
    m = load_selftune()
    status = tmp_path / "status.jsonl"
    status.write_text('{"week":"2026-W28","clean":true}\n')
    monkeypatch.setattr(m, "STATUS_PATH", status)
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 1)

    real_read = m.Path.read_text
    def guarded(self, *a, **k):
        if self == status:
            raise OSError("permission denied")
        return real_read(self, *a, **k)
    monkeypatch.setattr(m.Path, "read_text", guarded)

    # must not raise; must fail closed
    r = m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)
    assert r["error"] is not None
    assert r["met"] is False


def test_revisit_non_dict_json_line_fails_closed(tmp_path, monkeypatch):
    # ROUND-5 FIX: a line that is valid JSON but not an object (null, [], 42)
    # must fail closed, not crash on rec.get(). Completes the parse fail-closed
    # contract alongside corrupt-JSON, read-failure, and write-failure.
    m = load_selftune()
    status = tmp_path / "status.jsonl"
    status.write_text("null\n[]\n42\n")
    monkeypatch.setattr(m, "STATUS_PATH", status)
    monkeypatch.setattr(m, "REVISIT_CLEAN_WEEKS", 1)
    r = m.revisit_tracker("2026-07-06", green=True, attractor_count=0, thin=False)  # must not raise
    assert r["error"] is not None
    assert r["met"] is False
