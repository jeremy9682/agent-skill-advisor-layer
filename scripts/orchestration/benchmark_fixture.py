"""Create a disposable, local-only real-pilot benchmark fixture.

The builder deliberately creates no remote and contains no provider authority
or credentials.  Its only purpose is to supply a clean, reproducible local git
repository plus the public/private inputs required by the frozen benchmark
compiler.  It refuses an existing target and intentionally leaves a failed
build in place for investigation instead of trying to delete evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping

from .benchmark import (
    canonical_json,
    expected_invalid_trial_rules,
    expected_provider_preflight_policy,
    expected_review_warning_rule,
    expected_thresholds,
    preregister,
    sha256_value,
    write_private_json,
)
from .governance import routing_canon_path


class BenchmarkFixtureError(ValueError):
    """The disposable fixture cannot be safely created."""


@dataclass(frozen=True)
class PilotFixture:
    root: Path
    repo_root: Path
    evaluator_root: Path
    protocol_path: Path
    preregistration_path: Path
    base_sha: str
    route_policy_sha256: str


_REPO_FILES = {
    ".gitignore": "__pycache__/\n.pytest_cache/\n*.py[cod]\n",
    # Providers often run plain ``pytest`` as a diagnostic.  Keep that
    # provider-owned command cache-free as well as controller acceptance.
    "pytest.ini": "[pytest]\naddopts = -p no:cacheprovider\n",
    "pilot_app/__init__.py": "",
    "pilot_app/alpha.py": "def label() -> str:\n    return 'pending-alpha'\n",
    "pilot_app/beta.py": "def label() -> str:\n    return 'pending-beta'\n",
    "pilot_app/negative.py": "def enabled() -> bool:\n    return False\n",
    "pilot_app/report_check.py": (
        "from pathlib import Path\n\n"
        "def main() -> int:\n"
        "    reports = sorted(Path('reports').glob('pilot-readonly-*.md'))\n"
        "    return 0 if reports and all(path.is_file() and path.read_text(encoding='utf-8').strip() for path in reports) else 1\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    ),
    "reports/.gitkeep": "",
    "tests/test_pilot_app.py": (
        "from pilot_app import alpha, beta, negative\n\n"
        "def test_alpha_label():\n    assert alpha.label() == 'alpha-ready'\n\n"
        "def test_beta_label():\n    assert beta.label() == 'beta-ready'\n\n"
        "def test_negative_enabled():\n    assert negative.enabled() is True\n"
    ),
}

_REVIEW_PROMPT = (
    "Review only the integrated diff for the stated benchmark task. Verify its "
    "acceptance commands, ownership boundaries, and whether it meets the intent. "
    "Report concise findings without changing files."
)

# Acceptance is executed as an argv list, not through a shell.  Prefixing the
# command with ``env`` makes the pilot independent of the controller's pytest
# plugin environment and prevents interpreter cache files from becoming an
# unowned candidate diff.
_PYTEST_ACCEPTANCE_PREFIX = [
    "env",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
    "PYTHONDONTWRITEBYTECODE=1",
    "python3",
    "-B",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
]
_REPORT_CHECK_ACCEPTANCE = [
    "env",
    "PYTHONDONTWRITEBYTECODE=1",
    "python3",
    "-B",
    "-m",
    "pilot_app.report_check",
]


def _mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    _mode(path, 0o700)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _mode(path.parent, 0o700)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _mode(path, 0o600)


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkFixtureError(f"git fixture command failed: {type(exc).__name__}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git failure"
        raise BenchmarkFixtureError(f"git fixture command failed: {detail}")
    return result.stdout.strip()


def _lock_down_tree(root: Path) -> None:
    """Make builder-owned outputs private without relying on umask."""

    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        _mode(current_path, 0o700)
        for name in dirs:
            _mode(current_path / name, 0o700)
        for name in files:
            _mode(current_path / name, 0o600)


def _init_repo(root: Path) -> tuple[Path, str]:
    repo = root / "fixture-repo"
    _mkdir(repo)
    # The host's Git may predate ``git init --initial-branch``.  Selecting the
    # unborn branch explicitly keeps the fixture deterministic on both forms.
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "benchmark-fixture@example.invalid")
    _git(repo, "config", "user.name", "Agent Run benchmark fixture")
    for relative, body in _REPO_FILES.items():
        _write(repo / relative, body)
    _write(repo / ".agents" / "ledger-slug", "agent-run-pilot-fixture\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "Create disposable Agent Run benchmark fixture")
    head = _git(repo, "rev-parse", "HEAD")
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise BenchmarkFixtureError("fixture repository did not produce a 40-character HEAD")
    if _git(repo, "status", "--porcelain=v1"):
        raise BenchmarkFixtureError("new fixture repository is unexpectedly dirty")
    if _git(repo, "remote"):
        raise BenchmarkFixtureError("new fixture repository unexpectedly has a remote")
    _lock_down_tree(repo)
    return repo, head


def _argv_text(argv: list[list[str]]) -> list[str]:
    import shlex

    return [shlex.join(row) for row in argv]


def _writer(
    ident: str,
    *,
    task_shape: str,
    prompt: str,
    acceptance: list[list[str]],
    own: Iterable[str] = (),
    workspace_kind: str = "isolated-writer",
) -> dict[str, Any]:
    workspace: dict[str, Any] = {"kind": workspace_kind}
    if workspace_kind == "isolated-writer":
        workspace.update({
            "own": list(own),
            "do_not_touch": ["tests/test_pilot_app.py", ".agents/ledger-slug"],
        })
    return {
        "id": ident,
        "task_shape": task_shape,
        "depends_on": [],
        "workspace": workspace,
        "prompt_body": prompt,
        "acceptance_argv": acceptance,
    }


def _private_task(
    task_id: str,
    task_class: str,
    *,
    repo: Path,
) -> dict[str, Any]:
    if task_class == "separable":
        separable_acceptance = [
            [
                *_PYTEST_ACCEPTANCE_PREFIX,
                "tests/test_pilot_app.py::test_alpha_label",
                "tests/test_pilot_app.py::test_beta_label",
            ]
        ]
        alpha_acceptance = [
            [
                *_PYTEST_ACCEPTANCE_PREFIX,
                "tests/test_pilot_app.py::test_alpha_label",
            ]
        ]
        beta_acceptance = [
            [
                *_PYTEST_ACCEPTANCE_PREFIX,
                "tests/test_pilot_app.py::test_beta_label",
            ]
        ]
        task_input = (
            "Repair the two independent implementation defects in pilot_app/alpha.py "
            "and pilot_app/beta.py so the existing tests pass. Do not modify tests, "
            "ledger metadata, or unrelated files."
        )
        nodes = [
            _writer(
                "writer-alpha",
                task_shape="mechanical",
                prompt="Repair only pilot_app/alpha.py so alpha.label() returns 'alpha-ready'.",
                own=["pilot_app/alpha.py"],
                acceptance=alpha_acceptance,
            ),
            _writer(
                "writer-beta",
                task_shape="mechanical_grok",
                prompt="Repair only pilot_app/beta.py so beta.label() returns 'beta-ready'.",
                own=["pilot_app/beta.py"],
                acceptance=beta_acceptance,
            ),
        ]
        single = _writer(
            "single-producer",
            task_shape="ordinary_bug_fix",
            prompt=task_input,
            own=["pilot_app/alpha.py", "pilot_app/beta.py"],
            acceptance=separable_acceptance,
        )
        integrated_acceptance = separable_acceptance
        runbook = {"ready_sets": [["writer-alpha", "writer-beta"]]}
        intent = {"goal": "Repair two independent defects", "constraints": ["non-overlapping writer ownership", "existing tests are immutable"]}
    elif task_class == "negative_control":
        negative_acceptance = [
            [
                *_PYTEST_ACCEPTANCE_PREFIX,
                "tests/test_pilot_app.py::test_negative_enabled",
            ]
        ]
        task_input = (
            "Repair the single defect in pilot_app/negative.py so negative.enabled() "
            "returns True. Do not modify tests, ledger metadata, or unrelated files."
        )
        nodes = [
            _writer(
                "writer-negative",
                task_shape="mechanical",
                prompt=task_input,
                own=["pilot_app/negative.py"],
                acceptance=negative_acceptance,
            )
        ]
        single = _writer(
            "single-producer",
            task_shape="ordinary_bug_fix",
            prompt=task_input,
            own=["pilot_app/negative.py"],
            acceptance=negative_acceptance,
        )
        integrated_acceptance = negative_acceptance
        runbook = {"ready_sets": [["writer-negative"]]}
        intent = {"goal": "Repair one single-module defect", "constraints": ["negative control has one writer"]}
    elif task_class == "read_only":
        acceptance = [[*_REPORT_CHECK_ACCEPTANCE]]
        task_input = (
            "Inspect the repository without changing source code, tests, or metadata. Create "
            "only reports/pilot-readonly-combined.md with a concise analysis of the alpha, "
            "beta, and negative implementations and their test expectations."
        )
        nodes = [
            _writer(
                "reader-composer-alpha",
                task_shape="mechanical",
                prompt=(
                    "Read source and tests without modifying them. Analyse only alpha and its "
                    "test expectation; write only reports/pilot-readonly-alpha.md."
                ),
                own=["reports/pilot-readonly-alpha.md"],
                acceptance=acceptance,
            ),
            _writer(
                "reader-grok-beta-negative",
                task_shape="mechanical_grok",
                prompt=(
                    "Read source and tests without modifying them. Analyse only beta, negative, "
                    "and their test expectations; write only "
                    "reports/pilot-readonly-beta-negative.md."
                ),
                own=["reports/pilot-readonly-beta-negative.md"],
                acceptance=acceptance,
            ),
        ]
        single = _writer(
            "single-producer",
            task_shape="ordinary_bug_fix",
            prompt=task_input,
            own=["reports/pilot-readonly-combined.md"],
            acceptance=acceptance,
        )
        integrated_acceptance = acceptance
        runbook = {"ready_sets": [["reader-composer-alpha", "reader-grok-beta-negative"]]}
        intent = {
            "goal": "Produce bounded source-read-only analyses as reviewable report artifacts",
            "source_code_read_only": True,
            "filesystem_write_scope": "owned report artifacts only",
            "constraints": [
                "source, tests, and repository metadata must remain unchanged",
                "each producer may write only its non-overlapping report path",
            ],
        }
    else:  # pragma: no cover - internal caller has fixed identities
        raise BenchmarkFixtureError(f"unsupported task class: {task_class}")

    graph = {
        "nodes": [
            {
                "id": node["id"],
                "task_shape": node["task_shape"],
                "depends_on": node["depends_on"],
                "prompt_sha256": sha256_value(node["prompt_body"]),
                "acceptance_sha256": sha256_value(node["acceptance_argv"]),
            }
            for node in nodes
        ]
    }
    return {
        "version": 1,
        "task_id": task_id,
        "intent": intent,
        "task_input": task_input,
        "graph": graph,
        "manual_runbook": runbook,
        "hidden_assertions": [
            "The fixture starts from a clean committed HEAD.",
            "No credential, remote, provider, model, or routing authority is present in private task material.",
        ],
        "lifecycle": {
            "fixture_repo_root": str(repo),
            "single_producer": single,
            "nodes": nodes,
            "review": {"id": "review", "prompt_body": _REVIEW_PROMPT},
            "integrated_acceptance": integrated_acceptance,
        },
    }


def _public_task(
    private: Mapping[str, Any],
    task_class: str,
    *,
    base_sha: str,
    route_policy_sha256: str,
    private_task_sha256: str,
) -> dict[str, Any]:
    lifecycle = private["lifecycle"]
    acceptance = lifecycle["integrated_acceptance"]
    return {
        "task_id": private["task_id"],
        "task_class": task_class,
        "base_commit": base_sha,
        "intent_sha256": sha256_value(private["intent"]),
        "prompt_sha256": sha256_value(private["task_input"]),
        "route_policy_sha256": route_policy_sha256,
        "manual_runbook_sha256": sha256_value(private["manual_runbook"]),
        "graph_sha256": sha256_value(private["graph"]),
        "private_task_sha256": private_task_sha256,
        "single_producer_task_shape": "ordinary_bug_fix",
        "acceptance_commands": _argv_text(acceptance),
        "deadline_seconds": 300,
        "writer_limit": 1 if task_class == "negative_control" else 2,
        # A is the Codex baseline while B/C use Cursor's Composer/Grok routes.
        # This public set is intentionally the union across the paired block.
        "producer_families": ["openai", "cursor"],
        "reviewer": {
            "route": "claude_final_review",
            "model": "opus",
            "effort": "high",
            "family": "anthropic",
            "independence": "cross_family",
            "prompt_sha256": sha256_value(_REVIEW_PROMPT),
            "timeout_seconds": 900,
        },
    }


def build_pilot_fixture(root: Path) -> PilotFixture:
    """Build exactly one disposable 9-cell pilot input set below ``root``.

    ``root`` must not exist.  A failure deliberately preserves the partial
    output to make the cause inspectable; callers choose any cleanup later.
    """

    target = root.expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise BenchmarkFixtureError("fixture root must not already exist")
    _mkdir(target)
    repo, base_sha = _init_repo(target)
    evaluator = target / "evaluator"
    _mkdir(evaluator)
    _mkdir(evaluator / "tasks")
    canon = routing_canon_path()
    if not canon.is_file():
        raise BenchmarkFixtureError("routing policy is unavailable")
    route_policy_sha256 = hashlib.sha256(canon.read_bytes()).hexdigest()

    active = [("sep-1", "separable"), ("neg-1", "negative_control"), ("read-1", "read_only")]
    reserve = [("reserve-sep", "separable"), ("reserve-neg", "negative_control"), ("reserve-read", "read_only")]
    manifest: dict[str, dict[str, str]] = {}
    public_active: list[dict[str, Any]] = []
    public_reserve: list[dict[str, Any]] = []
    for task_id, task_class in [*active, *reserve]:
        private = _private_task(task_id, task_class, repo=repo)
        encoded = (canonical_json(private) + "\n").encode("utf-8")
        task_path = evaluator / "tasks" / f"{task_id}.json"
        _write(task_path, encoded.decode("utf-8"))
        digest = hashlib.sha256(encoded).hexdigest()
        manifest[task_id] = {"path": f"tasks/{task_id}.json", "sha256": digest}
        public = _public_task(
            private,
            task_class,
            base_sha=base_sha,
            route_policy_sha256=route_policy_sha256,
            private_task_sha256=digest,
        )
        (public_active if (task_id, task_class) in active else public_reserve).append(public)
    manifest_bytes = (canonical_json({"version": 1, "tasks": manifest}) + "\n").encode("utf-8")
    _write(evaluator / "private-manifest.json", manifest_bytes.decode("utf-8"))
    protocol = {
        "version": 1,
        "stage": "pilot",
        "order_seed": 20260718,
        "hidden_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "thresholds": expected_thresholds(),
        "arm_contract_version": 1,
        "arm_order_strategy": "seeded-balanced-latin-square-v1",
        "invalid_trial_rules": expected_invalid_trial_rules(),
        "review_warning_rule": expected_review_warning_rule(),
        "required_provider_families": ["openai", "cursor", "anthropic"],
        # A live pilot gates only on observable execution safety. It neither
        # collects nor infers subscription/quota state; a rate limit reached
        # while an arm runs is a measured treatment outcome.
        "provider_preflight_policy": expected_provider_preflight_policy(),
        "tasks": public_active,
        "reserve_tasks": public_reserve,
    }
    protocol_path = target / "protocol.json"
    write_private_json(protocol_path, protocol)
    preregistration_path = target / "preregistration.json"
    preregister(protocol, preregistration_path)
    _lock_down_tree(target)
    return PilotFixture(
        root=target,
        repo_root=repo,
        evaluator_root=evaluator,
        protocol_path=protocol_path,
        preregistration_path=preregistration_path,
        base_sha=base_sha,
        route_policy_sha256=route_policy_sha256,
    )
