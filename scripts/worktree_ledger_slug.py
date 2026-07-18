#!/usr/bin/env python3
"""Provision a generated worktree's local checkpoint-ledger slug stamp."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


SLUG_RE = re.compile(r"[A-Za-z0-9._-]+")
EXCLUDE_RULE = "/.agents/ledger-slug"


class LedgerSlugProvisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProvisionedLedgerSlug:
    source_root: Path
    worktree_root: Path
    slug: str
    slug_path: Path
    exclude_path: Path


def git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=False,
            text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerSlugProvisionError(f"git invocation failed: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip() or "unknown Git error"
        raise LedgerSlugProvisionError(detail)
    return completed.stdout.strip()


def git_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel")).resolve()


def tracked(root: Path, relative_path: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative_path],
            capture_output=True, check=False, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerSlugProvisionError(f"cannot inspect tracked slug: {exc}") from exc
    return completed.returncode == 0


def read_slug(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LedgerSlugProvisionError(f"cannot read ledger slug: {path}") from exc
    if not SLUG_RE.fullmatch(value):
        raise LedgerSlugProvisionError(f"invalid ledger slug: {path}")
    return value


def source_canonical_slug(source_root: Path) -> str:
    path = source_root / ".agents" / "ledger-slug"
    return read_slug(path) if path.is_file() else source_root.name


def ensure_exact_exclusion(worktree_root: Path) -> Path:
    exclude_path = Path(git(worktree_root, "rev-parse", "--git-path", "info/exclude"))
    if not exclude_path.is_absolute():
        exclude_path = (worktree_root / exclude_path).resolve()
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if EXCLUDE_RULE not in existing.splitlines():
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            with exclude_path.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(EXCLUDE_RULE + "\n")
        if EXCLUDE_RULE not in exclude_path.read_text(encoding="utf-8").splitlines():
            raise LedgerSlugProvisionError("exact ledger slug exclusion was not persisted")
    except OSError as exc:
        raise LedgerSlugProvisionError(f"cannot provision local Git exclusion: {exc}") from exc
    return exclude_path


def provision(source: Path, worktree: Path) -> ProvisionedLedgerSlug:
    source_root = git_root(source)
    worktree_root = git_root(worktree)
    slug = source_canonical_slug(source_root)
    relative = ".agents/ledger-slug"
    slug_path = worktree_root / relative
    is_tracked = tracked(worktree_root, relative)
    if slug_path.exists():
        actual = read_slug(slug_path)
        if actual != slug:
            kind = "tracked" if is_tracked else "untracked"
            raise LedgerSlugProvisionError(f"{kind} ledger slug conflicts with source canonical slug")
    elif is_tracked:
        raise LedgerSlugProvisionError("tracked ledger slug is absent from worktree")
    else:
        try:
            slug_path.parent.mkdir(parents=True, exist_ok=True)
            slug_path.write_text(slug + "\n", encoding="utf-8")
        except OSError as exc:
            raise LedgerSlugProvisionError(f"cannot write worktree ledger slug: {exc}") from exc
        if read_slug(slug_path) != slug:
            raise LedgerSlugProvisionError("written ledger slug did not validate")
    exclude_path = ensure_exact_exclusion(worktree_root)
    return ProvisionedLedgerSlug(source_root, worktree_root, slug, slug_path, exclude_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--worktree", required=True)
    args = parser.parse_args(argv)
    try:
        result = provision(Path(args.source), Path(args.worktree))
    except LedgerSlugProvisionError as exc:
        parser.error(str(exc))
    print(result.slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
