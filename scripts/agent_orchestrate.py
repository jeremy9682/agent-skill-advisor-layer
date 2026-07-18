#!/usr/bin/env python3
"""Public, local-only entry point for the V1 orchestration controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

# Direct execution makes ``scripts/`` the import root rather than the repository.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.orchestration.journal import (  # noqa: E402
    EventJournal,
    fold_events,
    request_cancel_file,
    write_replaceable_manifest,
)
from scripts.orchestration.plan import PlanValidationError, load_plan  # noqa: E402
from scripts.orchestration.runtime import (  # noqa: E402
    AgentLedgerCLI,
    OrchestrationRuntime,
)
from scripts.orchestration.scheduler import (  # noqa: E402
    FakeAdapter,
    Scheduler,
    SchedulerError,
)


DEFAULT_ROOT = Path.home() / ".agent-runs" / "orchestration"
LEDGER_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
EVALUATOR_MANIFEST = "evaluator-root.json"


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _run_dir(root: Path, run_id: str) -> Path:
    candidate = (root.expanduser().resolve() / run_id).resolve()
    if candidate.parent != root.expanduser().resolve():
        raise ValueError("unsafe run id")
    return candidate


def _git_path(repo_root: Path, *args: str) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode or not completed.stdout.strip():
        raise ValueError("cannot resolve canonical Git repository identity")
    value = Path(completed.stdout.strip())
    return value.resolve() if value.is_absolute() else (repo_root / value).resolve()


def _read_ledger_slug(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("cannot read canonical ledger slug") from exc
    if not LEDGER_SLUG_RE.fullmatch(value):
        raise ValueError("canonical ledger slug is invalid")
    return value


def canonical_ledger_slug(repo_root: Path) -> str:
    """Resolve one checkpoint identity across a main checkout and its worktrees."""

    requested = repo_root.expanduser().resolve()
    top = _git_path(requested, "--show-toplevel")
    if top != requested:
        raise ValueError("plan.repo_root must be the Git top-level directory")
    common = _git_path(requested, "--git-common-dir")
    if common.name != ".git" or not common.is_dir():
        raise ValueError("unsupported Git common directory layout")
    source_root = common.parent.resolve()
    source_slug_path = source_root / ".agents" / "ledger-slug"
    source_slug = (
        _read_ledger_slug(source_slug_path)
        if source_slug_path.is_file()
        else source_root.name
    )
    if not LEDGER_SLUG_RE.fullmatch(source_slug):
        raise ValueError("source repository basename is not a valid ledger slug")

    local_slug_path = top / ".agents" / "ledger-slug"
    if local_slug_path.is_file():
        local_slug = _read_ledger_slug(local_slug_path)
        if local_slug != source_slug:
            raise ValueError("worktree ledger slug conflicts with source canonical slug")
        return local_slug
    return source_slug


def _project_ledger_slug(plan: dict) -> dict:
    canonical = canonical_ledger_slug(Path(plan["repo_root"]))
    declared = plan.get("ledger_slug")
    if declared is not None and declared != canonical:
        raise ValueError("plan ledger_slug conflicts with canonical repository identity")
    plan["ledger_slug"] = canonical
    return plan


def _compiled(path: Path, run_id: str | None = None) -> dict:
    plan = _project_ledger_slug(load_plan(path))
    plan["run_id"] = run_id or plan.get("run_id") or f"run-{uuid.uuid4()}"
    return plan


def _persist_plan(directory: Path, plan: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    write_replaceable_manifest(directory / "plan.json", plan)


def _persist_start_bundle(
    directory: Path, plan: dict, evaluator_manifest: dict | None
) -> None:
    """Publish the approved start inputs as one same-filesystem rename."""

    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    with tempfile.TemporaryDirectory(
        prefix=f".{directory.name}.start-", dir=directory.parent
    ) as temporary:
        staging = Path(temporary)
        os.chmod(staging, 0o700)
        _persist_plan(staging, plan)
        if evaluator_manifest is not None:
            write_replaceable_manifest(
                staging / EVALUATOR_MANIFEST, evaluator_manifest
            )
        os.replace(staging, directory)


def _load_persisted(directory: Path) -> dict:
    raw = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("persisted plan is invalid")
    return _project_ledger_slug(raw)


def _has_evaluator_refs(plan: dict) -> bool:
    return any(
        isinstance(task.get("input_ref"), str)
        and task["input_ref"].startswith("evaluator:")
        for task in plan["tasks"]
    )


def _approved_evaluator_root(raw: str | None) -> tuple[Path, dict] | tuple[None, None]:
    if raw is None:
        return None, None
    candidate = Path(raw).expanduser()
    if candidate.is_symlink():
        raise ValueError("evaluator root may not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("evaluator root must be an existing directory")
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ValueError("evaluator root must have mode 0700")
    pointer = str(root)
    manifest = {
        "version": 1,
        "path": pointer,
        "path_sha256": hashlib.sha256(pointer.encode("utf-8")).hexdigest(),
    }
    return root, manifest


def _evaluator_root_for_resume(
    plan: dict, directory: Path, raw: str | None
) -> Path | None:
    root, supplied = _approved_evaluator_root(raw)
    manifest_path = directory / EVALUATOR_MANIFEST
    if _has_evaluator_refs(plan) and root is None:
        raise ValueError("evaluator input refs require explicit --evaluator-root on resume")
    if manifest_path.exists():
        if manifest_path.is_symlink() or stat.S_IMODE(manifest_path.stat().st_mode) != 0o600:
            raise ValueError("persisted evaluator root manifest is not private")
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(persisted, dict) or set(persisted) != {
            "version",
            "path",
            "path_sha256",
        }:
            raise ValueError("persisted evaluator root manifest is invalid")
        digest = hashlib.sha256(str(persisted["path"]).encode("utf-8")).hexdigest()
        if persisted.get("version") != 1 or persisted.get("path_sha256") != digest:
            raise ValueError("persisted evaluator root manifest is corrupt")
        if supplied != persisted:
            raise ValueError("resume evaluator root differs from approved start pointer")
    elif supplied is not None:
        raise ValueError("resume cannot add an evaluator root absent at start")
    return root


def _scheduler(
    plan: dict,
    directory: Path,
    *,
    live: bool,
    evaluator_root: Path | None = None,
) -> Scheduler:
    journal = EventJournal(directory / "events.jsonl", plan["run_id"])
    if not live:
        adapter = FakeAdapter(
            finalize={"status": "succeeded", "integration_head": "offline-fake"}
        )
    else:
        ledger = AgentLedgerCLI(
            slug=str(plan["ledger_slug"]),
            intent_ref="docs/intents/agent-run-orchestration-v1-20260718.md",
            repo_root=Path(plan["repo_root"]),
        )
        adapter = OrchestrationRuntime(
            plan,
            artifact_root=directory / "artifacts",
            worktree_root=Path(plan["repo_root"]).parent / ".agent-run-worktrees",
            evaluator_root=evaluator_root,
            ledger=ledger,
            live=True,
        )
    return Scheduler(plan, adapter, journal, directory / "controller.lock")


def command_validate(args: argparse.Namespace) -> int:
    plan = _project_ledger_slug(load_plan(Path(args.plan)))
    _json(
        {
            "status": "valid",
            "run_id": plan.get("run_id"),
            "tasks": plan["topological_order"],
        }
    )
    return 0


def command_start(args: argparse.Namespace) -> int:
    plan = _compiled(Path(args.plan), args.run_id)
    root = Path(args.runtime_root).expanduser().resolve()
    directory = _run_dir(root, plan["run_id"])
    if directory.exists():
        raise ValueError("run directory already exists; use resume")
    evaluator_root, evaluator_manifest = _approved_evaluator_root(
        args.evaluator_root
    )
    if _has_evaluator_refs(plan) and evaluator_root is None:
        raise ValueError("evaluator input refs require explicit --evaluator-root")
    _persist_start_bundle(directory, plan, evaluator_manifest)
    state = _scheduler(
        plan, directory, live=bool(args.live), evaluator_root=evaluator_root
    ).run()
    _json(state)
    return 0 if state["status"] == "completed" else 2


def command_resume(args: argparse.Namespace) -> int:
    directory = _run_dir(Path(args.runtime_root), args.run_id)
    plan = _load_persisted(directory)
    evaluator_root = _evaluator_root_for_resume(
        plan, directory, args.evaluator_root
    )
    state = _scheduler(
        plan, directory, live=bool(args.live), evaluator_root=evaluator_root
    ).run(resume=True)
    _json(state)
    return 0 if state["status"] == "completed" else 2


def command_status(args: argparse.Namespace) -> int:
    directory = _run_dir(Path(args.runtime_root), args.run_id)
    plan = _load_persisted(directory)
    journal = EventJournal(directory / "events.jsonl", plan["run_id"])
    _json(fold_events(journal.read()))
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    directory = _run_dir(Path(args.runtime_root), args.run_id)
    plan = _load_persisted(directory)
    journal = EventJournal(directory / "events.jsonl", plan["run_id"])
    state = fold_events(journal.read())
    if state["status"] in {"completed", "failed", "failed-unsafe", "canceled"}:
        raise ValueError("terminal runs cannot be canceled")
    controller = journal.current_controller()
    if controller is None:
        raise ValueError("run has no active controller generation")
    generation, fencing_token = controller
    request = request_cancel_file(
        directory / "events.jsonl.cancel-request.json",
        run_id=plan["run_id"],
        generation=generation,
        fencing_token=fencing_token,
    )
    _json(
        {
            "status": "cancel-requested",
            "request_id": request["request_id"],
            "generation": generation,
        }
    )
    return 0


def command_collect(args: argparse.Namespace) -> int:
    directory = _run_dir(Path(args.runtime_root), args.run_id)
    plan = _load_persisted(directory)
    journal = EventJournal(directory / "events.jsonl", plan["run_id"])
    state = fold_events(journal.read())
    _json(
        {
            "run_id": plan["run_id"],
            "status": state["status"],
            "artifacts": str(directory / "artifacts"),
            "tasks": state["tasks"],
        }
    )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime-root", default=str(DEFAULT_ROOT))
    sub = p.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("plan")
    validate.set_defaults(func=command_validate)

    start = sub.add_parser("start")
    start.add_argument("plan")
    start.add_argument("--run-id")
    start.add_argument("--evaluator-root")
    start.add_argument(
        "--live",
        action="store_true",
        help="allow governed provider invocation",
    )
    start.set_defaults(func=command_start)

    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--evaluator-root")
    resume.add_argument("--live", action="store_true")
    resume.set_defaults(func=command_resume)

    status = sub.add_parser("status")
    status.add_argument("run_id")
    status.set_defaults(func=command_status)

    cancel = sub.add_parser("cancel")
    cancel.add_argument("run_id")
    cancel.add_argument("--live", action="store_true")
    cancel.set_defaults(func=command_cancel)

    collect = sub.add_parser("collect")
    collect.add_argument("run_id")
    collect.set_defaults(func=command_collect)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, PlanValidationError, SchedulerError) as exc:
        _json({"status": "error", "error": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
