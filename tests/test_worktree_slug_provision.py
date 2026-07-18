from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "worktree_ledger_slug", ROOT / "scripts" / "worktree_ledger_slug.py"
)
slug_helper = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = slug_helper
SPEC.loader.exec_module(slug_helper)


def git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("base\n")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "base")
    worktree = tmp_path / "linked"
    git(source, "worktree", "add", "-b", "linked-branch", str(worktree))
    return source, worktree


def test_provision_stamps_linked_worktree_and_uses_its_local_exclude(tmp_path):
    source, worktree = linked_worktree(tmp_path)
    (source / ".agents").mkdir()
    (source / ".agents" / "ledger-slug").write_text("source-canon\n")

    result = slug_helper.provision(source, worktree)

    assert result.slug == "source-canon"
    assert (worktree / ".agents" / "ledger-slug").read_text() == "source-canon\n"
    assert slug_helper.EXCLUDE_RULE in result.exclude_path.read_text().splitlines()
    assert git(worktree, "check-ignore", ".agents/ledger-slug").returncode == 0


def test_provision_rejects_untracked_conflict_without_overwrite(tmp_path):
    source, worktree = linked_worktree(tmp_path)
    (source / ".agents").mkdir()
    (source / ".agents" / "ledger-slug").write_text("source-canon\n")
    (worktree / ".agents").mkdir()
    target = worktree / ".agents" / "ledger-slug"
    target.write_text("different\n")

    with pytest.raises(slug_helper.LedgerSlugProvisionError, match="untracked ledger slug conflicts"):
        slug_helper.provision(source, worktree)
    assert target.read_text() == "different\n"
