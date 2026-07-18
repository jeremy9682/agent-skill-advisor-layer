#!/usr/bin/env python3
"""Offline functional smoke test; it never invokes an external provider."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agent-orchestrate-qa-") as temp:
        repo = Path(temp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "user.email",
                "qa@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "QA"], check=True
        )
        (repo / "prompt.txt").write_text("read only verification\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "prompt.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        plan = {
            "version": 1,
            "run_id": "qa-functional",
            "repo_root": str(repo),
            "tasks": [
                {
                    "id": "probe",
                    "task_shape": "mechanical",
                    "input_ref": "prompt.txt",
                }
            ],
        }
        plan_path = Path(temp) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        runtime = Path(temp) / "runs"
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "agent_orchestrate.py"),
                "--runtime-root",
                str(runtime),
                "start",
                str(plan_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=root,
        )
        if completed.returncode != 0:
            raise RuntimeError("offline start failed")
        state = json.loads(completed.stdout)
        if state.get("status") != "completed":
            raise RuntimeError("offline run did not complete")
    print("functional QA passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
