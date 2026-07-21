"""The launcher's pin behaviour: what it runs, and what it refuses to promote."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module(monkeypatch, root: Path):
    spec = importlib.util.spec_from_file_location(
        "orchestrator_adapter", ROOT / "scripts" / "agent_orchestrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "LOCK", root / "orchestrator.lock.json")
    monkeypatch.setattr(module, "PIN_CACHE", root / "pins")
    return module


def _repo(path: Path) -> str:
    """A tiny git repo with one commit on main plus an origin/main ref."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *a], check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("checkout", "-q", "-B", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (path / "entry.py").write_text("print('pinned')\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "initial")
    head = run("rev-parse", "HEAD").stdout.strip()
    run("update-ref", "refs/remotes/origin/main", head)
    return head


def _lock(root: Path, commit: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "orchestrator.lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "repository": "https://example.invalid/repo",
                "commit": commit,
                "entrypoint": "entry.py",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_launch_ignores_where_the_development_checkout_is_parked(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    locked = _repo(checkout)
    # Move the development checkout past the locked commit and dirty it: the old
    # adapter refused to launch in exactly this state, which is the state an
    # actively developed repository is in every ordinary day.
    (checkout / "entry.py").write_text("print('newer')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "commit", "-qam", "later"], check=True)
    (checkout / "scratch.txt").write_text("dirty\n", encoding="utf-8")

    root = tmp_path / "governance"
    _lock(root, locked)
    module = _module(monkeypatch, root)
    # _load_lock binds its default path at definition time, so the caller must
    # pass the fixture's lock explicitly rather than rely on patching LOCK.
    lock = module._load_lock(root / "orchestrator.lock.json")
    entrypoint = module._verified_entrypoint(checkout, lock)

    # What runs is the locked tree, materialised on its own, not the checkout.
    assert entrypoint.read_text(encoding="utf-8") == "print('pinned')\n"
    assert checkout not in entrypoint.parents


def test_launch_says_how_to_fix_a_commit_it_cannot_obtain(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    _repo(checkout)
    root = tmp_path / "governance"
    _lock(root, "0" * 40)
    module = _module(monkeypatch, root)
    lock = module._load_lock(root / "orchestrator.lock.json")
    with pytest.raises(module.OrchestratorAdapterError) as excinfo:
        module._verified_entrypoint(checkout, lock)
    # A refusal has to name the remedy; "differs from lock" taught nobody what
    # to do and cost a live dispatch.
    assert "fetch origin" in str(excinfo.value)


def _promote_module(monkeypatch, root: Path, checkout: Path):
    """Adapter wired to a fixture governance root and checkout."""
    module = _module(monkeypatch, root)
    monkeypatch.setenv(module.CHECKOUT_ENV, str(checkout))
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "lock"], check=True, capture_output=True)
    return module


def _locked(root: Path) -> str:
    return json.loads((root / "orchestrator.lock.json").read_text())["commit"]


EVIDENCE = "full suite green and one cross-family review returning GO"


def test_promote_advances_the_lock_and_commits_it(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    first = _repo(checkout)
    (checkout / "entry.py").write_text("print('second')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "commit", "-qam", "second"], check=True, capture_output=True)
    second = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", second],
        check=True, capture_output=True,
    )
    root = tmp_path / "governance"
    _lock(root, first)
    module = _promote_module(monkeypatch, root, checkout)
    monkeypatch.setattr(module, "_canonical_remote_matches", lambda *_a: True)

    assert module._promote([second, "--evidence", EVIDENCE]) == 0
    assert _locked(root) == second
    log = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%B"], check=True, capture_output=True, text=True
    ).stdout
    # The evidence is the whole point of keeping promotion manual; it has to
    # survive into history, not just into the operator's memory.
    assert EVIDENCE in log


def test_promote_restores_the_lock_when_the_commit_fails(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    head = _repo(checkout)
    root = tmp_path / "governance"
    _lock(root, head)
    module = _promote_module(monkeypatch, root, checkout)
    monkeypatch.setattr(module, "_canonical_remote_matches", lambda *_a: True)
    (checkout / "entry.py").write_text("print('next')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "commit", "-qam", "next"], check=True, capture_output=True)
    nxt = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", nxt],
        check=True, capture_output=True,
    )

    real = subprocess.run

    def failing(command, *args, **kwargs):
        if "commit" in command:
            return subprocess.CompletedProcess(command, 1, "", "hook rejected")
        return real(command, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", failing)
    assert module._promote([nxt, "--evidence", EVIDENCE]) == 2
    # The launcher reads the file, not the commit, so a half-done promotion is
    # live immediately: it must be rolled all the way back.
    assert _locked(root) == head


def test_promote_refuses_a_checkout_that_is_not_the_locked_repository(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    head = _repo(checkout)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "https://example.invalid/fork"],
        check=True, capture_output=True,
    )
    root = tmp_path / "governance"
    _lock(root, head)
    module = _promote_module(monkeypatch, root, checkout)
    # A fork-only commit written into a lock that still names the canonical
    # repository would be unobtainable for anyone else -- unauditable by
    # construction.
    assert module._promote([head, "--evidence", EVIDENCE]) == 2


def test_promote_rejects_placeholder_evidence(tmp_path, monkeypatch):
    root = tmp_path / "governance"
    _lock(root, "0" * 40)
    module = _module(monkeypatch, root)
    assert module._promote(["abc1234", "--evidence", "test"]) == 2


def test_materialise_pin_reuses_a_pin_created_by_a_concurrent_launcher(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    head = _repo(checkout)
    root = tmp_path / "governance"
    _lock(root, head)
    module = _module(monkeypatch, root)
    pin = module._pin_root(head)

    real = subprocess.run

    def racing(command, *args, **kwargs):
        if "worktree" in command and "add" in command:
            # Simulate losing the race: someone else created the pin, so our
            # add fails. Refusing here would kill one of two concurrent live
            # dispatches for no reason.
            real(command, *args, **kwargs)
            return subprocess.CompletedProcess(command, 128, "", "already exists")
        return real(command, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", racing)
    assert module._materialise_pin(checkout, head) == pin


def test_promote_refuses_a_commit_that_is_not_on_origin_main(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    head = _repo(checkout)
    tree = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    dangling = subprocess.run(
        ["git", "-C", str(checkout), "commit-tree", tree, "-p", head, "-m", "dangling"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    root = tmp_path / "governance"
    _lock(root, head)
    module = _module(monkeypatch, root)
    monkeypatch.setenv(module.CHECKOUT_ENV, str(checkout))
    assert module._promote([dangling, "--evidence", "none"]) == 2
    # Pinning to a commit nobody else can fetch would make the runtime
    # unauditable, so the lock must be left untouched.
    assert json.loads((root / "orchestrator.lock.json").read_text())["commit"] == head


def test_promote_requires_evidence(tmp_path, monkeypatch):
    root = tmp_path / "governance"
    _lock(root, "0" * 40)
    module = _module(monkeypatch, root)
    with pytest.raises(SystemExit):
        module._promote(["abc1234"])
