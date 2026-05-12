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
}

LOCAL_REPO_FALLBACKS = {
    "https://github.com/alchaincyf/huashu-skills.git": GOV_ROOT / "cache" / "huashu-skills",
    "https://github.com/alchaincyf/huashu-design.git": GOV_ROOT / "cache" / "huashu-design",
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
    "huashu-agent-swarm",
    "gstack-pair-agent",
    "pair-agent",
    "gstack-retro",
    "retro",
    "gstack-setup-gbrain",
    "setup-gbrain",
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
    files: list[Path] = []
    if root.is_file():
        return [root]
    for current, dirs, filenames in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".cache")]
        current_path = Path(current)
        for filename in filenames:
            if filename in SKIP_DIRS:
                continue
            p = current_path / filename
            if p.is_file():
                files.append(p)
    return sorted(files)


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    if not root.exists():
        return ""
    resolved_root = root.resolve()
    for p in iter_files_for_hash(root):
        try:
            rp = p.resolve()
            rel = str(rp.relative_to(resolved_root))
            h.update(rel.encode("utf-8", "surrogateescape"))
            h.update(b"\0")
            h.update(rp.read_bytes())
            h.update(b"\0")
        except Exception:
            continue
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
    if group in {"huashu-design", "gstack", "frontend-design"}:
        return "merge-only" if git.get("git_dirty") else "git-managed"
    if path.is_symlink() or group == "symlink-source":
        return "source-managed"
    return "manual-only"


def call_policy(name: str, description: str, frontmatter: dict[str, Any]) -> str:
    joined = f"{name} {description}".lower()
    if str(frontmatter.get("disable-model-invocation", "")).lower() == "true":
        return "explicit-only"
    if name == "skill-advisor":
        return "router"
    if name in SUGGEST_CONFIRM_SKILLS or any(h in joined for h in SUGGEST_CONFIRM_HINTS):
        return "suggest-confirm"
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


def estimate_usage(entries: list[dict[str, Any]], days: int, limit: int, max_bytes: int) -> dict[str, int]:
    names = sorted({e["dir_name"] for e in entries} | {e["name"] for e in entries})
    valid = set(names)
    counts = {name: 0 for name in names}
    cutoff = dt.datetime.now().timestamp() - days * 86400
    files: list[Path] = []
    for base in [HOME / ".codex" / "sessions", HOME / ".claude" / "projects"]:
        if base.exists():
            files.extend([p for p in base.rglob("*.jsonl") if p.stat().st_mtime >= cutoff])
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    skill_path_re = re.compile(r"/skills/([^/\s]+)/SKILL\.md")
    dollar_re = re.compile(r"\$([A-Za-z0-9:_-]+)")
    skill_call_re = re.compile(r"Skill\(([A-Za-z0-9:_-]+)")
    markers = ("/skills/", "SKILL.md", "Skill(", "$")
    for f in files:
        try:
            if f.stat().st_size > max_bytes:
                continue
            seen: set[str] = set()
            with f.open(errors="ignore") as fh:
                for line in fh:
                    if not any(marker in line for marker in markers):
                        continue
                    for match in skill_path_re.findall(line):
                        if match in valid:
                            seen.add(match)
                    for match in dollar_re.findall(line):
                        if match in valid:
                            seen.add(match)
                    for match in skill_call_re.findall(line):
                        if match in valid:
                            seen.add(match)
        except Exception:
            continue
        for name in seen:
            counts[name] += 1
    return counts


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    previous_manifest = load_previous_manifest(MANIFEST_PATH)
    prev = previous_entries(previous_manifest)
    entries = discover_skills()
    usage = estimate_usage(entries, args.usage_days, args.usage_file_limit, args.usage_size_limit)
    for entry in entries:
        entry["usage_recent_file_hits"] = usage.get(entry["dir_name"], 0) + usage.get(entry["name"], 0)

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
        "dependency_checks": dependency_checks(),
        "sync_actions": sync_actions,
        "huashu_design": check_huashu_design(entries),
        "syntax_checks": {
            "enabled": args.syntax_check,
            "count": len(checks),
            "failures": [c for c in checks if not c["ok"]],
        },
        "entries": entries,
    }
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
    args = parser.parse_args(argv)

    report = build_report(args)
    if args.write_manifest:
        write_json(MANIFEST_PATH, report)
    if args.report:
        stamp = report["generated_at"].replace(":", "").replace("-", "")
        write_json(REPORTS_DIR / f"{stamp}-skill-audit.json", report)
    print(json.dumps({
        "generated_at": report["generated_at"],
        "summary": report["summary"],
        "dependency_checks": report["dependency_checks"],
        "sync_actions": report["sync_actions"][:20],
        "huashu_design": report["huashu_design"],
        "syntax_failures": report["syntax_checks"]["failures"][:20],
        "manifest": str(MANIFEST_PATH) if args.write_manifest else "",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
