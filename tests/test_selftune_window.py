import datetime as dt
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_selftune():
    path = ROOT / "scripts" / "router_selftune.py"
    spec = importlib.util.spec_from_file_location("router_selftune_window", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Audit:
    def discover_skills(self):
        return [{"name": "prototype", "dir_name": "prototype"}]

    def estimate_usage(self, entries, days, limit, max_bytes, health=None):
        assert entries == self.discover_skills()
        assert (days, limit, max_bytes) == (7, 400, 3_000_000)
        if health is not None:  # healthy scan: files found and read
            health["files_found"] = 5
            health["files_scanned"] = 5
        return {"prototype": {"actual_skill_invocation": 3}}


class Routing:
    def __init__(self):
        self.audit = Audit()

    def load_audit_module(self):
        return self.audit

    def collect_skills(self, audit, _unused):
        assert audit is self.audit
        return [{"name": "prototype", "policy": "auto-eligible"}]


def test_analyze_log_uses_timestamp_window_and_adoption(tmp_path, monkeypatch):
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "routing-log.jsonl")
    monkeypatch.setattr(m, "ATTRACTOR_DISTINCT_PROMPTS", 2)
    now = dt.datetime.now(dt.timezone.utc)
    records = [
        {"ts": (now - dt.timedelta(days=8)).isoformat(), "fired": True,
         "prompt_sha": "old-1", "candidates": [{"skill": "prototype"}]},
        {"ts": (now - dt.timedelta(days=8)).isoformat(), "fired": True,
         "prompt_sha": "old-2", "candidates": [{"skill": "prototype"}]},
        {"ts": (now - dt.timedelta(days=1)).isoformat(), "fired": True,
         "prompt_sha": "recent", "candidates": [{"skill": "prototype"}]},
    ]
    m.LOG_PATH.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = m.analyze_log(Routing())

    assert result["emissions"] == result["fires"] == 1
    assert result["attractors"] == []
    assert result["window"] == {"kind": "last_days", "days": 7}
    # Adoption is a NON-CAUSAL period ratio, not a conversion rate: 3 invocations
    # over 1 fire is a legitimate 3.0 ratio (>1.0). That it can exceed 1.0 is
    # exactly why it must never be labeled or used as a "% of fires converted".
    assert result["adoption"] == {
        "available": True, "fires": 1, "invocations": 3, "ratio": 3.0}


def test_analyze_log_falls_back_to_final_log_lines_without_timestamps(tmp_path, monkeypatch):
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "routing-log.jsonl")
    monkeypatch.setattr(m, "ATTRACTOR_FALLBACK_LINES", 2)
    records = [
        {"fired": True, "prompt_sha": "discarded", "candidates": []},
        {"fired": True, "prompt_sha": "kept-1", "candidates": []},
        {"fired": False, "prompt_sha": "kept-2", "candidates": []},
    ]
    m.LOG_PATH.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = m.analyze_log(Routing())

    assert result["emissions"] == 2
    assert result["fires"] == 1
    assert result["window"] == {"kind": "last_lines", "lines": 2}


def test_record_time_reads_naive_iso_as_local_not_utc(monkeypatch):
    # Force a non-UTC zone so this assertion has discriminating power even on a
    # UTC CI host: the old treat-as-UTC bug and the treat-as-local fix agree only
    # at offset 0, so pinning UTC+8 makes a regression actually fail here.
    import time
    monkeypatch.setenv("TZ", "Asia/Shanghai")  # UTC+8, no DST
    time.tzset()
    try:
        m = load_selftune()
        naive = dt.datetime(2026, 7, 13, 12, 0, 0)  # naive, like write_log emits
        got = m._record_time({"ts": naive.isoformat()})
        # 12:00 local (UTC+8) == 04:00 UTC; treat-as-UTC would wrongly give 12:00Z.
        assert got == dt.datetime(2026, 7, 13, 4, 0, 0, tzinfo=dt.timezone.utc)
        assert got != naive.replace(tzinfo=dt.timezone.utc)  # rejects the old impl
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_analyze_log_windows_out_untimestamped_rows_when_any_are_dated(tmp_path, monkeypatch):
    # Once ANY record carries a timestamp we trust the dated window and drop the
    # legacy untimestamped rows: they predate the ts field, so they are old and
    # outside a recent window. This is deliberate, not silent data loss.
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "routing-log.jsonl")
    monkeypatch.setattr(m, "ATTRACTOR_DISTINCT_PROMPTS", 2)
    now = dt.datetime.now(dt.timezone.utc)
    records = [
        {"fired": True, "prompt_sha": "legacy-no-ts", "candidates": []},
        {"ts": (now - dt.timedelta(days=1)).isoformat(), "fired": True,
         "prompt_sha": "recent", "candidates": []},
    ]
    m.LOG_PATH.write_text("".join(json.dumps(r) + "\n" for r in records))

    result = m.analyze_log(Routing())

    assert result["window"] == {"kind": "last_days", "days": 7}
    assert result["emissions"] == result["fires"] == 1


class _RaisingAudit(Audit):
    def estimate_usage(self, entries, days, limit, max_bytes, health=None):
        raise RuntimeError("no transcripts available")


class _RaisingRouting(Routing):
    def __init__(self):
        self.audit = _RaisingAudit()


def test_adoption_unavailable_is_not_reported_as_a_zero(tmp_path, monkeypatch):
    # When usage evidence cannot be gathered the report must say "unavailable",
    # never print a bare "0 skill-invocations" that reads as an observed zero.
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "routing-log.jsonl")
    now = dt.datetime.now(dt.timezone.utc)
    m.LOG_PATH.write_text(json.dumps(
        {"ts": (now - dt.timedelta(days=1)).isoformat(), "fired": True,
         "prompt_sha": "x", "candidates": []}) + "\n")

    result = m.analyze_log(_RaisingRouting())

    assert result["adoption"] == {
        "available": False, "fires": 1, "invocations": None, "ratio": None}
    label = m._adoption_label(result["adoption"])
    assert "unavailable" in label
    assert "0 skill-invocations" not in label


def test_adoption_label_marks_ratio_non_causal_and_not_a_percentage():
    m = load_selftune()
    label = m._adoption_label(
        {"available": True, "fires": 1, "invocations": 3, "ratio": 3.0})
    assert "non-causal" in label
    assert "%" not in label       # a >1.0 ratio must never be dressed up as a percent
    assert "3.00" in label


def test_no_routing_log_reports_adoption_unavailable_not_zero(tmp_path, monkeypatch):
    # A fresh install with no routing log scanned no usage, so it must report
    # unavailable — never a bare "0 skill-invocations" bootstrap claim.
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "does-not-exist.jsonl")
    result = m.analyze_log(Routing())
    assert result["adoption"]["available"] is False
    assert "unavailable" in m._adoption_label(result["adoption"])


class _ZeroAudit(Audit):
    """Usage estimator that returns a populated all-zero dict without raising.
    ``scanned`` sets the reported scan health: 0 mimics 'no recent files, or
    every per-file scan swallowed an error'; a positive value mimics a clean
    scan that genuinely found no usage."""
    def __init__(self, scanned):
        self._scanned = scanned

    def estimate_usage(self, entries, days, limit, max_bytes, health=None):
        if health is not None:
            health["files_found"] = 4
            health["files_scanned"] = self._scanned
        return {"prototype": {"actual_skill_invocation": 0}}


class _ZeroRouting(Routing):
    def __init__(self, scanned):
        self.audit = _ZeroAudit(scanned)


def test_adoption_zero_with_no_files_scanned_is_unavailable(tmp_path, monkeypatch):
    # files_scanned == 0 (no recent files, or every scan threw) means a 0 is a
    # scan gap, not data — must degrade to unavailable rather than assert "0".
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "routing-log.jsonl")
    now = dt.datetime.now(dt.timezone.utc)
    m.LOG_PATH.write_text(json.dumps(
        {"ts": (now - dt.timedelta(days=1)).isoformat(), "fired": True,
         "prompt_sha": "x", "candidates": []}) + "\n")
    result = m.analyze_log(_ZeroRouting(scanned=0))
    assert result["adoption"]["available"] is False
    assert result["adoption"]["invocations"] is None


def test_adoption_zero_with_files_scanned_is_a_real_zero(tmp_path, monkeypatch):
    # When at least one file scanned cleanly, a 0 is a genuine observed zero.
    m = load_selftune()
    monkeypatch.setattr(m, "LOG_PATH", tmp_path / "routing-log.jsonl")
    now = dt.datetime.now(dt.timezone.utc)
    m.LOG_PATH.write_text(json.dumps(
        {"ts": (now - dt.timedelta(days=1)).isoformat(), "fired": True,
         "prompt_sha": "x", "candidates": []}) + "\n")
    result = m.analyze_log(_ZeroRouting(scanned=4))
    assert result["adoption"] == {
        "available": True, "fires": 1, "invocations": 0, "ratio": 0.0}
