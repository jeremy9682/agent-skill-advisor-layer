from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.orchestration.benchmark import (
    build_launch_contract,
    load_preregistration,
    verify_evaluator_root,
)
from scripts.orchestration.benchmark_fixture import (
    BenchmarkFixtureError,
    build_pilot_fixture,
)
from scripts.orchestration.benchmark_lifecycle import compile_lifecycle_launch


def test_builds_disposable_clean_fixture_and_compilable_pilot_inputs(tmp_path: Path):
    root = tmp_path / "real-pilot"
    fixture = build_pilot_fixture(root)

    assert fixture.root == root.resolve()
    assert fixture.repo_root.is_dir()
    assert len(fixture.base_sha) == 40
    assert fixture.base_sha == subprocess.run(
        ["git", "-C", str(fixture.repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not subprocess.run(
        ["git", "-C", str(fixture.repo_root), "remote"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not subprocess.run(
        ["git", "-C", str(fixture.repo_root), "status", "--porcelain=v1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert os.stat(root).st_mode & 0o777 == 0o700
    for path in (fixture.evaluator_root, fixture.protocol_path, fixture.preregistration_path):
        assert os.stat(path).st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)

    envelope = load_preregistration(fixture.preregistration_path)
    protocol = envelope["protocol"]
    assert [task["task_class"] for task in protocol["tasks"]] == [
        "separable",
        "negative_control",
        "read_only",
    ]
    assert all(task["base_commit"] == fixture.base_sha for task in protocol["tasks"])
    assert verify_evaluator_root(protocol, fixture.evaluator_root)["tasks"].keys() == {
        "sep-1", "neg-1", "read-1", "reserve-sep", "reserve-neg", "reserve-read"
    }

    assert protocol["required_provider_families"] == ["openai", "cursor", "anthropic"]
    assert protocol["provider_preflight_policy"] == {
        "mode": "auth-host-incident-v1",
        "quota_monitoring": False,
        "inside_block_rate_limit": "treatment-outcome",
        "evidence_schema_version": 2,
        "max_freshness_seconds": 3600,
        "future_skew_seconds": 30.0,
    }
    assert {family for task in protocol["tasks"] for family in task["producer_families"]} == {
        "openai", "cursor"
    }
    for task in protocol["tasks"]:
        expected_prefix = (
            "env PYTHONDONTWRITEBYTECODE=1 python3 "
            if task["task_class"] == "read_only"
            else "env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 python3 "
        )
        assert all(command.startswith(expected_prefix) for command in task["acceptance_commands"])
        assert all("sh -c" not in command for command in task["acceptance_commands"])
    launches: dict[tuple[str, str], object] = {}
    for public in protocol["tasks"]:
        assert public["route_policy_sha256"] == fixture.route_policy_sha256
        assert public["reviewer"]["family"] not in public["producer_families"]
        assert public["reviewer"]["independence"] == "cross_family"
        for arm in ("A", "B", "C"):
            contract = build_launch_contract(protocol, fixture.evaluator_root, public["task_id"], arm)
            launch = compile_lifecycle_launch(
                contract,
                json.loads((fixture.evaluator_root / "tasks" / f"{public['task_id']}.json").read_text()),
                reviewer=public["reviewer"],
                cell_root=tmp_path / f"cell-{public['task_id']}-{arm}",
            )
            assert launch.plan["base_sha"] == fixture.base_sha
            launches[(public["task_id"], arm)] = launch

    for task_id in ("sep-1", "neg-1", "read-1"):
        a = launches[(task_id, "A")]
        assert [task["binding"]["provider"] for task in a.plan["tasks"]] == ["codex", "claude"]
        assert a.plan["tasks"][0]["binding"]["model"] == "gpt-5.6-terra"
        b, c = launches[(task_id, "B")], launches[(task_id, "C")]
        assert b.graph_sha256 == c.graph_sha256
        assert b.manual_runbook is not None and c.manual_runbook is None
        b_producers = [task for task in b.plan["tasks"] if not task.get("reviewer_for")]
        c_producers = [task for task in c.plan["tasks"] if not task.get("reviewer_for")]
        assert [
            (task["id"], task["task_shape"], task["binding"]["provider"], task["binding"]["model"])
            for task in b_producers
        ] == [
            (task["id"], task["task_shape"], task["binding"]["provider"], task["binding"]["model"])
            for task in c_producers
        ]
        assert all(task["binding"]["provider"] == "claude" for task in (b.plan["tasks"][-1], c.plan["tasks"][-1]))

    assert [
        (task["task_shape"], task["binding"]["model"])
        for task in launches[("sep-1", "B")].plan["tasks"][:-1]
    ] == [
        ("mechanical", "composer-2.5-fast"),
        ("mechanical_grok", "cursor-grok-4.5-high-fast"),
    ]
    assert [task["task_shape"] for task in launches[("neg-1", "B")].plan["tasks"][:-1]] == ["mechanical"]

    pytest_command = [
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
    separable_acceptance = [pytest_command + [
        "tests/test_pilot_app.py::test_alpha_label",
        "tests/test_pilot_app.py::test_beta_label",
    ]]
    alpha_acceptance = [pytest_command + ["tests/test_pilot_app.py::test_alpha_label"]]
    beta_acceptance = [pytest_command + ["tests/test_pilot_app.py::test_beta_label"]]
    negative_acceptance = [
        pytest_command + ["tests/test_pilot_app.py::test_negative_enabled"]
    ]

    assert launches[("sep-1", "A")].plan["tasks"][0]["acceptance"] == separable_acceptance
    for arm in ("B", "C"):
        sep_launch = launches[("sep-1", arm)]
        assert [task["acceptance"] for task in sep_launch.plan["tasks"][:-1]] == [
            alpha_acceptance,
            beta_acceptance,
        ]
        assert sep_launch.plan["integrated_acceptance"] == separable_acceptance

    for arm in ("A", "B", "C"):
        negative_launch = launches[("neg-1", arm)]
        assert all(
            task["acceptance"] == negative_acceptance
            for task in negative_launch.plan["tasks"]
            if not task.get("reviewer_for")
        )
        assert negative_launch.plan["integrated_acceptance"] == negative_acceptance

    read_private = json.loads((fixture.evaluator_root / "tasks" / "read-1.json").read_text())
    assert read_private["intent"]["source_code_read_only"] is True
    assert read_private["intent"]["filesystem_write_scope"] == "owned report artifacts only"
    read_b = launches[("read-1", "B")]
    assert [task["workspace"]["kind"] for task in read_b.plan["tasks"][:-1]] == [
        "isolated-writer",
        "isolated-writer",
    ]
    assert [task["task_shape"] for task in read_b.plan["tasks"][:-1]] == [
        "mechanical",
        "mechanical_grok",
    ]
    assert [task["workspace"]["own"] for task in read_b.plan["tasks"][:-1]] == [
        ["reports/pilot-readonly-alpha.md"],
        ["reports/pilot-readonly-beta-negative.md"],
    ]
    expected_acceptance = [[
        "env",
        "PYTHONDONTWRITEBYTECODE=1",
        "python3",
        "-B",
        "-m",
        "pilot_app.report_check",
    ]]
    assert all(task["acceptance"] == expected_acceptance for task in read_b.plan["tasks"][:-1])
    assert read_b.plan["integrated_acceptance"] == expected_acceptance

    # Make each frozen task's intended repair before exercising every compiled
    # acceptance argv under a caller environment that tries to undo it.  The
    # acceptance command itself must restore both hermetic settings.
    (fixture.repo_root / "pilot_app" / "alpha.py").write_text(
        "def label() -> str:\n    return 'alpha-ready'\n", encoding="utf-8"
    )
    (fixture.repo_root / "pilot_app" / "beta.py").write_text(
        "def label() -> str:\n    return 'beta-ready'\n", encoding="utf-8"
    )
    # The separable block must not be poisoned by the independent negative
    # control defect. Its single-producer and integrated acceptance are both
    # constrained to alpha/beta before that control is repaired.
    assert subprocess.run(
        separable_acceptance[0],
        cwd=fixture.repo_root,
        check=False,
        capture_output=True,
    ).returncode == 0
    assert subprocess.run(
        negative_acceptance[0],
        cwd=fixture.repo_root,
        check=False,
        capture_output=True,
    ).returncode != 0
    (fixture.repo_root / "pilot_app" / "negative.py").write_text(
        "def enabled() -> bool:\n    return True\n", encoding="utf-8"
    )
    report_path = fixture.repo_root / "reports" / "pilot-readonly-combined.md"
    report_path.write_text("fixture report\n", encoding="utf-8")
    acceptance_commands = {
        tuple(command)
        for launch in launches.values()
        for task in launch.plan["tasks"]
        if not task.get("reviewer_for")
        for command in task["acceptance"]
    }
    acceptance_commands.update(
        tuple(command)
        for launch in launches.values()
        for command in launch.plan["integrated_acceptance"]
    )
    for command in acceptance_commands:
        result = subprocess.run(
            command,
            cwd=fixture.repo_root,
            check=False,
            capture_output=True,
            # The fixture must be hermetic even when its caller pollutes or
            # removes the relevant environment settings.
            env={
                **os.environ,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0",
                "PYTHONDONTWRITEBYTECODE": "0",
            },
        )
        assert result.returncode == 0, result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
    # Providers may run their own ordinary Python diagnostics before the
    # controller-owned hermetic acceptance commands.  Standard interpreter
    # caches must remain outside the candidate diff in that case as well.
    for command in (
        ("python3", "-m", "pilot_app.report_check"),
        ("python3", "-m", "pytest", "-q"),
    ):
        subprocess.run(
            command,
            cwd=fixture.repo_root,
            check=False,
            capture_output=True,
            env={
                **os.environ,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    report_path.unlink()
    assert not list(fixture.repo_root.rglob("__pycache__"))
    assert not (fixture.repo_root / ".pytest_cache").exists()
    assert not subprocess.run(
        ["git", "-C", str(fixture.repo_root), "ls-files", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_refuses_existing_root_without_overwriting_it(tmp_path: Path):
    root = tmp_path / "already-there"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(BenchmarkFixtureError, match="must not already exist"):
        build_pilot_fixture(root)

    assert marker.read_text(encoding="utf-8") == "do not overwrite\n"
