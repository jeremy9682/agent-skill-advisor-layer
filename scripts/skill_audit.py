#!/usr/bin/env python3
"""Audit and safely govern local Claude/Codex skills.

This script is intentionally conservative. It can write a manifest baseline and
reports, but it will not overwrite local skill content unless all safe-sync
guards pass.
"""

from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - optional local validator
    yaml = None


HOME = Path.home()
GOV_ROOT = HOME / ".codex" / "skill-governance"
MANIFEST_PATH = GOV_ROOT / "skills-manifest.json"
REPORTS_DIR = GOV_ROOT / "reports"
SKILL_ROOTS = {
    "codex": HOME / ".codex" / "skills",
    "agents": HOME / ".agents" / "skills",
    "claude": HOME / ".claude" / "skills",
}

KNOWN_REMOTES = {
    "huashu-skills": {
        "url": "https://github.com/alchaincyf/huashu-skills.git",
        "branch": "master",
    },
    "huashu-design": {
        "url": "https://github.com/alchaincyf/huashu-design.git",
        "branch": "master",
    },
    "mattpocock-skills": {
        "url": "https://github.com/mattpocock/skills.git",
        "branch": "main",
    },
}

# Published bundle from mattpocock/skills .claude-plugin/plugin.json at the
# registered pin below. These are copied installs (no per-skill .git metadata),
# so name membership is the provenance bridge used by the local audit.
MATTPOCOCK_SKILLS = {
    "ask-matt",
    "code-review",
    "codebase-design",
    "diagnosing-bugs",
    "domain-modeling",
    "grill-with-docs",
    "grill-me",
    "grilling",
    "handoff",
    "implement",
    "improve-codebase-architecture",
    "prototype",
    "research",
    "setup-matt-pocock-skills",
    "tdd",
    "teach",
    "to-spec",
    "to-tickets",
    "triage",
    "wayfinder",
    "writing-great-skills",
}

# Local entry-routing overlays. These do not edit the pinned upstream skill and
# do not block skill-to-skill composition: Matt workflows may still invoke
# /grilling internally. The overlay only says that a top-level user request
# must explicitly name /grill-me, /grilling, or the grilling workflow before
# the runtime should enter the interview loop.
LOCAL_EXPLICIT_ONLY_SKILLS = {
    "grilling",
}

LOCAL_REPO_FALLBACKS = {
    "https://github.com/alchaincyf/huashu-skills.git": GOV_ROOT / "cache" / "huashu-skills",
    "https://github.com/alchaincyf/huashu-design.git": GOV_ROOT / "cache" / "huashu-design",
    "https://github.com/mattpocock/skills.git": GOV_ROOT / "cache" / "mattpocock-skills",
}

SKIP_DIRS = {
    ".git",
    ".DS_Store",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
}

MAX_CODEX_DESCRIPTION_CHARS = 1024

MANUAL_CONFIRM_HINTS = (
    "deploy",
    "deployment",
    "ship",
    "land",
    "github",
    "gmail",
    "trello",
    "discord",
    "notion",
    "plugin-control",
    "swarm",
    "automation",
    "browser-cookies",
)

ROUTER_HINTS = (
    "router",
    "routing",
    "dispatching",
)

SUGGEST_CONFIRM_SKILLS = {
    "code-review",
    "huashu-agent-swarm",
    "gstack-pair-agent",
    "pair-agent",
    "gstack-retro",
    "retro",
    "gstack-setup-gbrain",
    "setup-gbrain",
    "no-mistakes",
    "lfg",
    "ship",
    "gstack-ship",
    "land-and-deploy",
    "overnight-execution",
    "improve-codebase-architecture",
    "research",
    "wayfinder",
}

SUGGEST_CONFIRM_HINTS = (
    "pair agent",
    "share browser",
    "remote browser",
    "agent swarm",
    "multi-agent",
    "多agent",
    "蜂群",
    "retrospective",
    "weekly retro",
    "engineering retrospective",
    "gbrain",
    "no-mistakes",
    "safe push",
    "push safely",
    "release gate",
    "ci watch",
    "autonomous pipeline",
    "hands-off",
    "overnight execution",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # pragma: no cover - defensive reporting
        return 124, "", str(exc)


def parse_frontmatter(path: Path) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception as exc:
        return {}, [f"read_failed:{exc}"]

    if not lines or lines[0].strip() != "---":
        return {}, ["missing_frontmatter"]

    frontmatter_lines: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        frontmatter_lines.append(line)
    else:
        return {}, ["unterminated_frontmatter"]

    if yaml is not None:
        try:
            loaded = yaml.safe_load("\n".join(frontmatter_lines)) or {}
        except Exception as exc:
            return {}, [f"invalid_yaml:{exc.__class__.__name__}"]
        if not isinstance(loaded, dict):
            return {}, ["invalid_yaml:not_mapping"]
        data = {str(k): "" if v is None else str(v) for k, v in loaded.items()}
        if not data.get("name"):
            issues.append("missing_name")
        description = data.get("description")
        if not description:
            issues.append("missing_description")
        elif len(description) > MAX_CODEX_DESCRIPTION_CHARS:
            issues.append("description_too_long_for_codex")
        return data, issues

    data: dict[str, Any] = {}
    body_lines = lines[1:]
    i = 0
    while i < len(body_lines):
        line = body_lines[i]
        if line.strip() == "---":
            break
        if ":" not in line:
            i += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            if value in {"|", ">", "|-", ">-"}:
                block: list[str] = []
                i += 1
                while i < len(body_lines):
                    block_line = body_lines[i]
                    if block_line.strip() == "---":
                        i -= 1
                        break
                    if block_line and not block_line.startswith((" ", "\t")) and ":" in block_line:
                        i -= 1
                        break
                    block.append(block_line.strip())
                    i += 1
                data[key] = " ".join(part for part in block if part)
            else:
                data[key] = value
        i += 1
    else:
        issues.append("unterminated_frontmatter")

    if not data.get("name"):
        issues.append("missing_name")
    description = data.get("description")
    if not description:
        issues.append("missing_description")
    elif len(description) > MAX_CODEX_DESCRIPTION_CHARS:
        issues.append("description_too_long_for_codex")
    return data, issues


def iter_files_for_hash(root: Path) -> list[Path]:
    """All files under root, FOLLOWING directory symlinks (a link-farm skill
    may point whole subdirs elsewhere, e.g. bin → ~/gstack/bin — their content
    must participate in the digest or drift there is invisible). A realpath
    visited-set guards against symlink cycles."""
    files: list[Path] = []
    if root.is_file():
        return [root]
    visited: set[str] = set()
    for current, dirs, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(current)
        if real in visited:
            dirs[:] = []
            continue
        visited.add(real)
        # Sort in place so traversal order (hence which alias of a
        # same-realpath dir is visited first) is deterministic across runs and
        # filesystems — the digest must not depend on readdir order.
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".cache"))
        current_path = Path(current)
        for filename in filenames:
            if filename in SKIP_DIRS:
                continue
            p = current_path / filename
            if p.is_file():
                files.append(p)
    return sorted(files)


def tree_hash(root: Path) -> str:
    """Content digest of a skill dir. Symlink-farm aware: the relative name is
    taken from the WALK path (always under ``root``), never from ``resolve()``
    — a symlinked file resolves outside the root and ``relative_to`` raised,
    which the old code swallowed, silently yielding the empty-input sha256 for
    every link-farm skill (no drift detection at all). Content reads follow
    symlinks, so the digest covers the real bytes the runtime sees."""
    h = hashlib.sha256()
    if not root.exists():
        return ""
    for p in iter_files_for_hash(root):
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = p.name
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def line_count(path: Path) -> int:
    try:
        return sum(1 for _ in path.open(errors="ignore"))
    except Exception:
        return 0


def git_info(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    code, top, _ = run(["git", "-C", str(resolved), "rev-parse", "--show-toplevel"], timeout=10)
    if code != 0 or not top:
        return {}
    git_root = Path(top)
    _, head, _ = run(["git", "-C", str(git_root), "rev-parse", "HEAD"], timeout=10)
    _, branch, _ = run(["git", "-C", str(git_root), "branch", "--show-current"], timeout=10)
    _, remote, _ = run(["git", "-C", str(git_root), "remote", "get-url", "origin"], timeout=10)
    _, status, _ = run(["git", "-C", str(git_root), "status", "--short"], timeout=10)
    return {
        "git_root": str(git_root),
        "git_head": head,
        "git_branch": branch,
        "git_remote": remote,
        "git_dirty": bool(status),
        "git_status_lines": len(status.splitlines()) if status else 0,
    }


def source_group(name: str, path: Path, git: dict[str, Any]) -> str:
    remote = git.get("git_remote", "")
    matt_copy_roots = {
        root for key in ("codex", "claude")
        if (root := SKILL_ROOTS.get(key)) is not None
    }
    if (
        "mattpocock/skills" in remote
        or (name in MATTPOCOCK_SKILLS and path.parent in matt_copy_roots)
    ):
        return "mattpocock-skills"
    if name == "huashu-design" or "huashu-design" in remote:
        return "huashu-design"
    if name.startswith("huashu-"):
        return "huashu-skills"
    if name.startswith("gstack") or "gstack" in str(path):
        return "gstack"
    if "frontend-design" in remote or name == "frontend-design":
        return "frontend-design"
    if "superpowers" in str(path):
        return "superpowers"
    if path.is_symlink():
        return "symlink-source"
    return "local-manual"


def update_policy(name: str, group: str, git: dict[str, Any], path: Path) -> str:
    if group == "huashu-skills" and name != "huashu-design":
        return "auto-sync-if-clean"
    if group == "mattpocock-skills":
        return "merge-only"
    if group in {"huashu-design", "gstack", "frontend-design"}:
        return "merge-only" if git.get("git_dirty") else "git-managed"
    if path.is_symlink() or group == "symlink-source":
        return "source-managed"
    return "manual-only"


def call_policy(name: str, description: str, frontmatter: dict[str, Any]) -> str:
    joined = f"{name} {description}".lower()
    if name == "skill-advisor":
        return "router"
    # Local safety overlays outrank an upstream invocation hint. In particular,
    # an upstream explicit wrapper can still be costly enough that natural-
    # language routing must suggest and wait; an exact user invocation is the
    # approval event that releases the gate.
    if name in SUGGEST_CONFIRM_SKILLS or any(h in joined for h in SUGGEST_CONFIRM_HINTS):
        return "suggest-confirm"
    if name in LOCAL_EXPLICIT_ONLY_SKILLS:
        return "explicit-only"
    if str(frontmatter.get("disable-model-invocation", "")).lower() == "true":
        return "explicit-only"
    if any(h in joined for h in ROUTER_HINTS):
        return "router"
    if any(h in joined for h in MANUAL_CONFIRM_HINTS):
        return "manual-confirm"
    return "auto-eligible"


def discover_skills() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for runtime, root in SKILL_ROOTS.items():
        if not root.exists():
            continue
        skill_files: list[Path] = []
        for current, dirs, filenames in os.walk(root):
            dirs[:] = [
                d
                for d in dirs
                if d not in SKIP_DIRS and (not d.startswith(".") or d == ".system")
            ]
            if "SKILL.md" in filenames:
                skill_files.append(Path(current) / "SKILL.md")
        for skill_md in sorted(skill_files):
            skill_dir = skill_md.parent
            fm, issues = parse_frontmatter(skill_md)
            name = fm.get("name") or skill_dir.name
            desc = fm.get("description") or ""
            git = git_info(skill_dir)
            if not git:
                # Link-farm provenance: the dir itself is outside any checkout,
                # but its SKILL.md may be a symlink INTO one (~/.codex/skills/
                # gstack → ~/gstack). The resolved file's home is the honest
                # provenance ONLY if the file is actually TRACKED there — a
                # checkout dir can .gitignore the target (~/gstack ignores
                # .agents/), and HEAD cannot reproduce untracked bytes, so
                # granting git_head for those would be a fake pin.
                resolved_md = skill_md.resolve()
                if resolved_md.parent != skill_dir.resolve():
                    candidate = git_info(resolved_md.parent)
                    if candidate:
                        repo_root = Path(candidate["git_root"])
                        try:
                            rel = str(resolved_md.relative_to(repo_root))
                        except ValueError:
                            rel = ""
                        code, _, _ = run(
                            ["git", "-C", str(repo_root), "ls-files",
                             "--error-unmatch", rel],
                            timeout=10) if rel else (1, "", "")
                        if code == 0:
                            git = candidate
            group = source_group(name, skill_dir, git)
            lcount = line_count(skill_md)
            entry = {
                "runtime": runtime,
                "name": name,
                "dir_name": skill_dir.name,
                "path": str(skill_dir),
                "resolved_path": str(skill_dir.resolve()),
                "skill_md": str(skill_md),
                "is_symlink": skill_dir.is_symlink(),
                "symlink_target": str(skill_dir.resolve()) if skill_dir.is_symlink() else "",
                "frontmatter_ok": not issues,
                "frontmatter_issues": issues,
                "description_length": len(desc),
                "skill_lines": lcount,
                "skill_size_bytes": skill_md.stat().st_size if skill_md.exists() else 0,
                "tree_hash": tree_hash(skill_dir),
                "source_group": group,
                "update_policy": update_policy(name, group, git, skill_dir),
                "call_policy": call_policy(name, desc, fm),
                "warnings": [],
            }
            entry.update(git)
            if lcount > 500:
                entry["warnings"].append("large_skill_md_over_500_lines")
            if len(desc) < 40:
                entry["warnings"].append("short_description_may_not_route_well")
            entries.append(entry)
    return entries


def ls_remote(url: str, branch: str) -> dict[str, str]:
    code, out, err = run(["git", "ls-remote", url, "HEAD", f"refs/heads/{branch}"], timeout=30)
    result = {"url": url, "branch": branch, "head": "", "error": ""}
    if code != 0:
        result["error"] = err or out
        return result
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
            result["head"] = parts[0]
            return result
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "HEAD":
            result["head"] = parts[0]
            return result
    return result


def load_previous_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def entry_key(entry: dict[str, Any]) -> str:
    return f"{entry['runtime']}::{entry['dir_name']}"


def previous_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry_key(e): e for e in manifest.get("entries", []) if "runtime" in e and "dir_name" in e}


def clone_repo(url: str, branch: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="skill-governance-"))
    repo = tmp / "repo"
    code, out, err = run(["git", "clone", "--depth", "1", "--branch", branch, url, str(repo)], timeout=120)
    if code != 0:
        fallback = LOCAL_REPO_FALLBACKS.get(url)
        if fallback and fallback.exists():
            code, out, err = run(["git", "clone", str(fallback), str(repo)], timeout=120)
        if code != 0:
            raise RuntimeError(err or out)
    return repo


def copy_tree_contents(src: Path, dst: Path) -> None:
    for child in dst.iterdir() if dst.exists() else []:
        if child.name in {".git", ".DS_Store"}:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in {".git", ".DS_Store"}:
            continue
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def compare_dirs(a: Path, b: Path) -> list[str]:
    if not a.exists() or not b.exists():
        return ["missing_dir"]
    diff: list[str] = []
    cmp = filecmp.dircmp(a, b, ignore=[".git", ".DS_Store", "__pycache__", "node_modules"])
    diff.extend([f"only_remote:{x}" for x in cmp.left_only[:50]])
    diff.extend([f"only_local:{x}" for x in cmp.right_only[:50]])
    diff.extend([f"changed:{x}" for x in cmp.diff_files[:50]])
    for sub in cmp.common_dirs:
        if len(diff) >= 100:
            break
        diff.extend([f"{sub}/{x}" for x in compare_dirs(a / sub, b / sub)[:20]])
    return diff


def sync_huashu_skills(
    entries: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    dry_run: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    repo: Path | None = None
    try:
        repo = clone_repo(KNOWN_REMOTES["huashu-skills"]["url"], KNOWN_REMOTES["huashu-skills"]["branch"])
        for entry in entries:
            if entry["source_group"] != "huashu-skills" or entry["name"] == "huashu-design":
                continue
            local_dir = Path(entry["path"])
            remote_dir = repo / entry["dir_name"]
            if not remote_dir.exists():
                actions.append({"entry": entry_key(entry), "action": "blocked", "reason": "remote_dir_missing"})
                continue
            remote_hash = tree_hash(remote_dir)
            local_hash = entry["tree_hash"]
            prev = previous.get(entry_key(entry))
            baseline_hash = prev.get("tree_hash") if prev else ""
            if local_hash == remote_hash:
                actions.append({"entry": entry_key(entry), "action": "noop", "reason": "already_current"})
                continue
            if not baseline_hash:
                actions.append({"entry": entry_key(entry), "action": "blocked", "reason": "no_previous_baseline"})
                continue
            if baseline_hash != local_hash:
                actions.append({"entry": entry_key(entry), "action": "blocked", "reason": "local_changed_since_manifest"})
                continue
            if dry_run:
                actions.append({"entry": entry_key(entry), "action": "would_sync", "reason": "clean_baseline"})
            else:
                copy_tree_contents(remote_dir, local_dir)
                actions.append({"entry": entry_key(entry), "action": "synced", "reason": "clean_baseline"})
    except Exception as exc:
        actions.append({"entry": "huashu-skills", "action": "error", "reason": str(exc)})
    finally:
        if repo:
            shutil.rmtree(repo.parent, ignore_errors=True)
    return actions


def check_huashu_design(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        repo = clone_repo(KNOWN_REMOTES["huashu-design"]["url"], KNOWN_REMOTES["huashu-design"]["branch"])
    except Exception as exc:
        return [{"entry": "huashu-design", "action": "error", "reason": str(exc)}]
    try:
        for entry in entries:
            if entry["name"] != "huashu-design":
                continue
            local_dir = Path(entry["path"])
            diff = compare_dirs(repo, local_dir)
            missing_voiceover = [
                rel for rel in [
                    ".env.example",
                    "assets/narration_stage.jsx",
                    "references/voiceover-pipeline.md",
                    "scripts/tts-doubao.mjs",
                    "scripts/narrate-pipeline.mjs",
                    "scripts/mix-voiceover.sh",
                    "scripts/render-narration.sh",
                ]
                if not (local_dir / rel).exists()
            ]
            results.append({
                "entry": entry_key(entry),
                "action": "merge_report",
                "diff_count": len(diff),
                "diff_sample": diff[:40],
                "missing_voiceover_files": missing_voiceover,
            })
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)
    return results


def script_syntax_checks(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen_dirs: set[str] = set()
    for entry in entries:
        root = Path(entry["path"]).resolve()
        if str(root) in seen_dirs:
            continue
        seen_dirs.add(str(root))
        for p in iter_files_for_hash(root):
            suffix = p.suffix
            cmd: list[str] | None = None
            if suffix == ".sh":
                cmd = ["bash", "-n", str(p)]
            elif suffix in {".js", ".mjs"}:
                cmd = ["node", "--check", str(p)]
            elif suffix == ".py":
                cmd = ["python3", "-m", "py_compile", str(p)]
            if not cmd:
                continue
            code, out, err = run(cmd, timeout=20)
            checks.append({
                "path": str(p),
                "ok": code == 0,
                "error": "" if code == 0 else (err or out)[-1000:],
            })
    return checks


def dependency_checks() -> dict[str, str]:
    deps = ["git", "node", "npm", "python3", "ffmpeg", "ffprobe", "tmux", "uv", "yt-dlp"]
    result: dict[str, str] = {}
    for dep in deps:
        code, out, _ = run(["/usr/bin/env", "which", dep], timeout=10)
        result[dep] = out if code == 0 else "MISSING"
    for env_name in ["GEMINI_API_KEY", "DOUBAO_TTS_APP_ID", "DOUBAO_TTS_ACCESS_TOKEN", "DOUBAO_TTS_CLUSTER"]:
        result[f"env:{env_name}"] = "SET" if os.environ.get(env_name) else "UNSET"
    return result


USAGE_KINDS = (
    "actual_skill_invocation",
    "skill_file_read",
    "self_audit_read",
    "gstack_timeline",
    "assistant_announcement",
)

SELF_AUDIT_MARKERS = (
    "skill_audit.py",
    "router_selftune.py",
    "routing_eval.py --doctor",
    "routing_eval.py\", \"--doctor",
    "routing_eval.py', '--doctor",
)


def empty_usage() -> dict[str, int]:
    return {kind: 0 for kind in USAGE_KINDS}


def record_usage(counts: dict[str, dict[str, int]], alias: str, kind: str, valid: set[str]) -> None:
    if alias in valid and kind in USAGE_KINDS:
        counts.setdefault(alias, empty_usage())[kind] += 1


def iter_tool_uses(value: Any):
    if isinstance(value, dict):
        if value.get("type") == "tool_use" and value.get("name"):
            yield value
        for child in value.values():
            yield from iter_tool_uses(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_tool_uses(child)


def skill_alias_from_path(path_text: str, valid: set[str]) -> str | None:
    parts = path_text.split("/")
    try:
        idx = len(parts) - 1 - parts[::-1].index("skills")
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    alias = parts[idx + 1]
    return alias if alias in valid else None


def is_self_audit_read(text: str) -> bool:
    """Return true when one tool call identifies itself as audit/doctor work.

    Session transcripts do not preserve child PID or parent-process metadata, so
    reads issued in a *different* tool call cannot be attributed reliably.  This
    deliberately conservative check catches explicit and batch shell reads whose
    command also names the audit, self-tune, or doctor entry point; ambiguous
    standalone reads remain skill_file_read rather than being silently excluded.
    """
    normalized = re.sub(r"\s+", " ", text).lower()
    return any(marker in normalized for marker in SELF_AUDIT_MARKERS)


def record_skill_paths(text: str, counts: dict[str, dict[str, int]], valid: set[str]) -> None:
    kind = "self_audit_read" if is_self_audit_read(text) else "skill_file_read"
    for match in re.finditer(r"(?:~|/Users/[^\s\"']+)/[^\s\"']*/skills/[^\s\"']+/SKILL\.md", text):
        alias = skill_alias_from_path(match.group(0), valid)
        if alias:
            record_usage(counts, alias, kind, valid)


def record_gstack_commands(text: str, counts: dict[str, dict[str, int]], valid: set[str]) -> None:
    for match in re.finditer(r"(?:^|[\s;&|()])(?:[^\s;&|()]+/)?(gstack-[a-z0-9-]+)\b", text):
        alias = match.group(1)
        if alias in valid:
            record_usage(counts, alias, "actual_skill_invocation", valid)


def record_assistant_announcements(text: str, counts: dict[str, dict[str, int]], valid: set[str]) -> None:
    patterns = [
        r"使用 `([^`]+)` skill",
        r"用到 `([^`]+)`",
        r"using (?:the )?`?([\w:-]+)`? skill",
        r"我会用到 `([^`]+)`",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            record_usage(counts, match.group(1), "assistant_announcement", valid)


def scan_codex_session(path: Path, counts: dict[str, dict[str, int]], valid: set[str], max_bytes: int) -> None:
    if path.stat().st_size > max_bytes:
        return
    with path.open(errors="ignore") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload") or {}
            if payload.get("type") == "function_call":
                args = payload.get("arguments") or ""
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                record_skill_paths(args, counts, valid)
                record_gstack_commands(args, counts, valid)
            elif payload.get("type") == "message" and payload.get("role") == "assistant":
                text = "\n".join(
                    part.get("text", "")
                    for part in payload.get("content", [])
                    if isinstance(part, dict)
                )
                record_assistant_announcements(text, counts, valid)


def scan_claude_session(path: Path, counts: dict[str, dict[str, int]], valid: set[str], max_bytes: int) -> None:
    if path.stat().st_size > max_bytes:
        return
    with path.open(errors="ignore") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            for tool_use in iter_tool_uses(obj):
                name = tool_use.get("name")
                tool_input = tool_use.get("input") or {}
                if name == "Skill" and isinstance(tool_input, dict):
                    alias = tool_input.get("skill") or tool_input.get("name") or tool_input.get("skill_name")
                    if isinstance(alias, str):
                        record_usage(counts, alias, "actual_skill_invocation", valid)
                if name in {"Read", "Bash"} and isinstance(tool_input, dict):
                    text = json.dumps(tool_input, ensure_ascii=False)
                    record_skill_paths(text, counts, valid)
                    if name == "Bash":
                        record_gstack_commands(text, counts, valid)
            if obj.get("type") == "assistant" and isinstance(obj.get("message"), dict):
                text = json.dumps(obj["message"].get("content", ""), ensure_ascii=False)
                record_assistant_announcements(text, counts, valid)


def scan_gstack_timeline(counts: dict[str, dict[str, int]], valid: set[str], cutoff: float) -> None:
    base = HOME / ".gstack" / "projects"
    if not base.exists():
        return
    for path in base.glob("*/timeline.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(errors="ignore") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    alias = obj.get("skill")
                    if isinstance(alias, str):
                        record_usage(counts, alias, "gstack_timeline", valid)
        except Exception:
            continue


def estimate_usage(entries: list[dict[str, Any]], days: int, limit: int, max_bytes: int,
                   health: dict[str, int] | None = None) -> dict[str, dict[str, int]]:
    """Scan recent transcripts for skill-usage evidence.

    Per-file scan errors are swallowed so one corrupt session cannot blank the
    whole report — but that means an all-zero result is ambiguous between "no
    usage" and "every scan failed". Pass a ``health`` dict to disambiguate: it is
    populated with ``files_found`` (recent transcript files in the window) and
    ``files_scanned`` (how many were read without raising). A caller can then
    treat a zero with ``files_scanned == 0`` as a scan gap, not an observed zero.
    """
    names = sorted({e["dir_name"] for e in entries} | {e["name"] for e in entries})
    valid = set(names)
    counts = {name: empty_usage() for name in names}
    cutoff = dt.datetime.now().timestamp() - days * 86400
    codex_files: list[Path] = []
    claude_files: list[Path] = []
    if (HOME / ".codex" / "sessions").exists():
        codex_files = [
            p for p in (HOME / ".codex" / "sessions").rglob("*.jsonl")
            if p.stat().st_mtime >= cutoff
        ]
    if (HOME / ".claude" / "projects").exists():
        claude_files = [
            p for p in (HOME / ".claude" / "projects").rglob("*.jsonl")
            if p.stat().st_mtime >= cutoff
        ]
    files = sorted(codex_files + claude_files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    scanned_ok = 0
    for f in files:
        try:
            if "/.codex/sessions/" in str(f):
                scan_codex_session(f, counts, valid, max_bytes)
            else:
                scan_claude_session(f, counts, valid, max_bytes)
            scanned_ok += 1
        except Exception:
            continue
    scan_gstack_timeline(counts, valid, cutoff)
    if health is not None:
        health["files_found"] = len(files)
        health["files_scanned"] = scanned_ok
    return counts


# The ONLY first-party group: locally-authored skills, exempt from the
# external-source immutable-identifier requirement. Any other value — including
# a missing/blank source_group on a malformed entry — is external and must be
# pinned (fail-closed; a governance gate must not exempt on absence).
FIRST_PARTY_GROUP = "local-manual"

# Registered upstream pins: source group → the specific commit SHA it is pinned
# to. A URL + mutable branch (KNOWN_REMOTES) is NOT a pin — the branch moves.
# These SHAs are the immutable identifier for skills copied from an upstream
# without a local git checkout. Keep in sync with docs/external-skill-sources.md.
REGISTERED_PINS = {
    "mattpocock-skills": "391a2701dd948f94f56a39f7533f8eea9a859c87",
    "emilkowalski-skills": "f76beceb7d3fc8c43309cefad5a095a206103a4e",
    "huashu-skills": "35e7cf31328f6de07e5d125bfd094791f84b2352",
    "huashu-design": "0e7ec8aca0058184c1a9e06e57697e84f68a3f0f",
}

# Frozen legacy copies: real-file copies whose upstream snapshot provenance is
# lost (nothing to re-derive a commit from). Keyed by ABSOLUTE PATH (names
# collide across dirs) → the tree_hash the copy is frozen at. A matching hash
# counts as pinned-by-exception; ANY drift makes it an unpinned violation, so
# the gate's drift-detection purpose is preserved. Documented in
# docs/external-skill-sources.md; upgrading/removing these is a separate,
# user-approved decision — never a silent side effect of this gate.
FROZEN_LEGACY = {
    # copied from claude-plugins-official frontend-design plugin, drifted from
    # the plugin-cache version, original snapshot unknown; frozen 2026-07-12
    str(HOME / ".agents" / "skills" / "frontend-design"):
        "25b18e6ac9b6c0953b013f0795665af958a08eb40b40cc23df27c3f377168575",
    # Link-farm into ~/gstack/.agents, which is intentionally gitignored and
    # therefore cannot inherit the checkout HEAD as reproducible provenance.
    # Freeze the exact installed trees; any regeneration/drift reopens the gate.
    str(HOME / ".codex" / "skills" / "gstack"):
        "55ef5be33c64aab291e168cde00431e34509540d23a6dfda5b5f75ca579be521",
    str(HOME / ".codex" / "skills" / "gstack" / "gstack-upgrade"):
        "0bd74f7c577c1e0ff87dd14ced9ce40e56d586de1fc7913d0cc5384ffef143bb",
}


def _is_sha(value: str) -> bool:
    """True only for a full 40-hex-char git commit sha — not a branch name,
    not 'main', not a partial/garbage value. A pin must be immutable."""
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def pin_check(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Supply-chain pin check (Tier-2 item ⑤).

    Every EXTERNAL skill must carry an immutable identifier so a specific
    installed version is reproducible and drift is detectable:
      - Git-backed source  → a commit sha (``git_head``, a local checkout); or
      - a source group registered in ``REGISTERED_PINS`` with a fixed commit
        SHA (recorded in docs/external-skill-sources.md).

    A registered *URL + branch* (``KNOWN_REMOTES``) is NOT a pin — the branch
    is mutable. A ``tree_hash`` is always present (content digest) but is NOT
    accepted as the sole identifier — it proves integrity, not provenance.
    Only the explicit first-party group is exempt; a missing source_group is
    treated as external+unpinned (fail-closed). An external skill with neither
    a local commit nor a registered pin SHA is an ``unpinned`` violation.

    Reported unconditionally (baseline). The hard red gate is opt-in via
    ``--enforce-pins`` until the baseline reaches zero violations.
    """
    external = [e for e in entries if e.get("source_group") != FIRST_PARTY_GROUP]
    unpinned: list[dict[str, Any]] = []
    by_group: dict[str, dict[str, int]] = {}
    for e in external:
        group = e.get("source_group") or "(missing)"
        slot = by_group.setdefault(group, {"pinned": 0, "unpinned": 0})
        # Both a local HEAD and a registered pin must be a real 40-hex sha —
        # membership or a non-empty string is not enough (a branch name fakes it).
        has_commit = _is_sha(e.get("git_head") or "")
        has_registered_pin = _is_sha(REGISTERED_PINS.get(group, ""))
        frozen_hash = FROZEN_LEGACY.get(e.get("path", ""))
        # Frozen paths are checked FIRST and EXCLUSIVELY: if this exact copy is
        # frozen, only its hash matters — a git_head or a later group
        # registration must not mask drift of the frozen bytes.
        if frozen_hash is not None:
            if e.get("tree_hash") == frozen_hash:
                slot["pinned"] += 1
            else:
                slot["unpinned"] += 1
                unpinned.append({
                    "name": e.get("name", "?"),
                    "runtime": e.get("runtime", "?"),
                    "source_group": group,
                    "path": e.get("path", ""),
                    "is_symlink": e.get("is_symlink", False),
                    "reason": "frozen legacy copy DRIFTED from its frozen tree_hash",
                })
            continue
        if has_commit or has_registered_pin:
            slot["pinned"] += 1
        else:
            slot["unpinned"] += 1
            unpinned.append({
                "name": e.get("name", "?"),
                "runtime": e.get("runtime", "?"),
                "source_group": group,
                "path": e.get("path", ""),
                "is_symlink": e.get("is_symlink", False),
                "reason": "external skill with no local commit sha and no registered pin SHA",
            })
    return {
        "external_count": len(external),
        "pinned_count": len(external) - len(unpinned),
        "unpinned_count": len(unpinned),
        "by_group": by_group,
        "unpinned": sorted(unpinned, key=lambda u: (u["source_group"], u["name"], u["runtime"])),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    previous_manifest = load_previous_manifest(MANIFEST_PATH)
    prev = previous_entries(previous_manifest)
    entries = discover_skills()
    usage = estimate_usage(entries, args.usage_days, args.usage_file_limit, args.usage_size_limit)
    for entry in entries:
        # Entry-level evidence credits every installed copy that matches the
        # entry's aliases. The top-level summary below stays alias-level so
        # duplicate installs do not inflate fleet usage totals.
        combined = empty_usage()
        for alias in {entry["dir_name"], entry["name"]}:
            for kind, value in usage.get(alias, empty_usage()).items():
                combined[kind] += value
        entry["usage_recent"] = combined
        entry["usage_recent_file_hits"] = combined["skill_file_read"]
        entry["self_read_excluded"] = combined["self_audit_read"]
        entry["usage_recent_total_evidence"] = sum(
            value for kind, value in combined.items() if kind != "self_audit_read"
        )

    remotes = {name: ls_remote(meta["url"], meta["branch"]) for name, meta in KNOWN_REMOTES.items()}
    sync_actions = []
    if args.sync_safe or args.dry_run_sync:
        sync_actions = sync_huashu_skills(entries, prev, dry_run=not args.sync_safe)

    checks = script_syntax_checks(entries) if args.syntax_check else []
    report = {
        "generated_at": utc_now(),
        "tool_version": 1,
        "roots": {k: str(v) for k, v in SKILL_ROOTS.items()},
        "remotes": remotes,
        "summary": {
            "entry_count": len(entries),
            "unique_skill_names": len({e["name"] for e in entries}),
            "frontmatter_issue_count": sum(1 for e in entries if not e["frontmatter_ok"]),
            "large_skill_count": sum(1 for e in entries if e["skill_lines"] > 500),
            "merge_only_count": sum(1 for e in entries if e["update_policy"] == "merge-only"),
            "auto_sync_candidate_count": sum(1 for e in entries if e["update_policy"] == "auto-sync-if-clean"),
        },
        "usage_summary": {
            kind: sum(counts[kind] for counts in usage.values())
            for kind in USAGE_KINDS
        },
        "dependency_checks": dependency_checks(),
        "pin_checks": pin_check(entries),
        "sync_actions": sync_actions,
        "huashu_design": check_huashu_design(entries),
        "syntax_checks": {
            "enabled": args.syntax_check,
            "count": len(checks),
            "failures": [c for c in checks if not c["ok"]],
        },
        "entries": entries,
    }
    report["usage_summary"]["self_read_excluded"] = report["usage_summary"]["self_audit_read"]
    return report


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", action="store_true", help="write/update skills-manifest.json")
    parser.add_argument("--report", action="store_true", help="write a timestamped report")
    parser.add_argument("--syntax-check", action="store_true", help="run lightweight script syntax checks")
    parser.add_argument("--dry-run-sync", action="store_true", help="show safe sync actions without writing skill content")
    parser.add_argument("--sync-safe", action="store_true", help="perform safe syncs where manifest baseline proves no local edits")
    parser.add_argument("--usage-days", type=int, default=30)
    parser.add_argument("--usage-file-limit", type=int, default=400)
    parser.add_argument("--usage-size-limit", type=int, default=3_000_000)
    parser.add_argument(
        "--enforce-pins",
        action="store_true",
        help="exit non-zero if any external skill lacks an immutable identifier "
             "(supply-chain pin gate; keep off until the baseline is clean)",
    )
    args = parser.parse_args(argv)

    report = build_report(args)
    if args.write_manifest:
        write_json(MANIFEST_PATH, report)
    if args.report:
        stamp = report["generated_at"].replace(":", "").replace("-", "")
        write_json(REPORTS_DIR / f"{stamp}-skill-audit.json", report)
    pins = report["pin_checks"]
    print(json.dumps({
        "generated_at": report["generated_at"],
        "summary": report["summary"],
        "usage_summary": report["usage_summary"],
        "dependency_checks": report["dependency_checks"],
        "pin_checks": {
            "external_count": pins["external_count"],
            "pinned_count": pins["pinned_count"],
            "unpinned_count": pins["unpinned_count"],
            "unpinned": pins["unpinned"],
        },
        "sync_actions": report["sync_actions"][:20],
        "huashu_design": report["huashu_design"],
        "syntax_failures": report["syntax_checks"]["failures"][:20],
        "manifest": str(MANIFEST_PATH) if args.write_manifest else "",
    }, ensure_ascii=False, indent=2))
    if args.enforce_pins and pins["unpinned_count"] > 0:
        print(
            f"PIN GATE FAILED: {pins['unpinned_count']} external skill(s) lack an "
            f"immutable identifier (commit sha or registered upstream).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
