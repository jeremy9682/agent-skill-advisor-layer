#!/usr/bin/env python3
"""Fail-closed CLI for the preregistered Agent Run A/B/C benchmark.

The default execution mode is synthetic dry-run.  ``--live`` never falls back
to fake results: it binds only to the matching-checkout governed lifecycle
adapter and does not become a second provider router.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.orchestration.benchmark import (  # noqa: E402
    BenchmarkProtocolError,
    counterbalanced_order,
    evaluate_with_replacements,
    live_launch_preflight,
    load_preregistration,
    preregister,
    run_fake_experiment,
    validate_executable_protocol,
    verify_evaluator_root,
)


def _live_adapter(*, evidence_path: Path | None = None) -> Any | None:
    """Load only the local runtime seam, never a PATH/global `agent-run` shim."""

    try:
        from scripts.orchestration.runtime import benchmark_live_adapter
    except (ImportError, AttributeError):
        return None
    try:
        return benchmark_live_adapter(checkout_root=_REPO_ROOT, evidence_path=evidence_path)
    except (OSError, ValueError, TypeError):
        return None


def _emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _json(path: Path) -> Any:
    try:
        return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkProtocolError(f"cannot read JSON input: {exc}") from exc


def _protocol(path: Path) -> dict[str, Any]:
    raw = _json(path)
    if not isinstance(raw, dict):
        raise BenchmarkProtocolError("protocol must be a JSON object")
    return validate_executable_protocol(raw)


def _redacted_preflight(preflight: Any) -> dict[str, Any]:
    """Expose only gate decisions; never echo evidence content or paths."""

    if not isinstance(preflight, dict):
        return {"eligible": False, "action": "block-live-before-first-cell", "blockers": [{"code": "preflight-invalid"}]}
    blockers = preflight.get("blockers")
    gate = preflight.get("pre_block_gate")
    return {
        "eligible": preflight.get("eligible") is True,
        "action": preflight.get("action") if isinstance(preflight.get("action"), str) else "block-live-before-first-cell",
        "blockers": [
            {"code": row.get("code")}
            for row in blockers if isinstance(row, dict) and isinstance(row.get("code"), str)
        ] if isinstance(blockers, list) else [],
        "pre_block_gate": {
            "eligible": gate.get("eligible") is True,
            "action": gate.get("action") if isinstance(gate.get("action"), str) else "postpone-whole-block",
            "reasons": [
                {key: row[key] for key in ("provider_family", "reason") if isinstance(row.get(key), str)}
                for row in gate.get("reasons", []) if isinstance(row, dict)
            ],
        } if isinstance(gate, dict) else {"eligible": False, "action": "postpone-whole-block", "reasons": []},
        "config_fingerprint": preflight.get("config_fingerprint") if isinstance(preflight.get("config_fingerprint"), str) else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate an executable protocol")
    validate.add_argument("protocol", type=Path)

    freeze = sub.add_parser("preregister", help="freeze a mode-0600 preregistration")
    freeze.add_argument("protocol", type=Path)
    freeze.add_argument("output", type=Path)

    order = sub.add_parser("order", help="print the frozen counterbalanced order")
    order.add_argument("prereg", type=Path)

    verify = sub.add_parser("verify-evaluator", help="verify private evaluator hashes")
    verify.add_argument("prereg", type=Path)
    verify.add_argument("evaluator_root", type=Path)

    run = sub.add_parser("run", help="run synthetic harness or a governed live stage")
    run.add_argument("--prereg", type=Path, required=True)
    run.add_argument("--evaluator-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument(
        "--preflight-evidence",
        type=Path,
        help="optional JSON provider evidence for the whole-block gate",
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="request real execution; never implied and never falls back to fake",
    )

    preflight = sub.add_parser("preflight", help="perform a launch-free governed live readiness check")
    preflight.add_argument("--prereg", type=Path, required=True)
    preflight.add_argument("--evaluator-root", type=Path, required=True)
    preflight.add_argument("--preflight-evidence", type=Path)

    evaluate = sub.add_parser("evaluate", help="evaluate raw observed trial receipts")
    evaluate.add_argument("--prereg", type=Path, required=True)
    evaluate.add_argument("--trials", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            protocol = _protocol(args.protocol)
            _emit({"status": "valid", "stage": protocol["stage"], "tasks": len(protocol["tasks"])})
            return 0
        if args.command == "preregister":
            envelope = preregister(_protocol(args.protocol), args.output)
            _emit({"status": "frozen", "protocol_sha256": envelope["protocol_sha256"]})
            return 0
        if args.command == "order":
            envelope = load_preregistration(args.prereg)
            _emit(counterbalanced_order(envelope["protocol"]))
            return 0
        if args.command == "verify-evaluator":
            envelope = load_preregistration(args.prereg)
            verified = verify_evaluator_root(envelope["protocol"], args.evaluator_root)
            _emit({"status": "verified", **verified})
            return 0
        if args.command == "preflight":
            envelope = load_preregistration(args.prereg)
            verify_evaluator_root(envelope["protocol"], args.evaluator_root)
            adapter = _live_adapter(evidence_path=args.preflight_evidence)
            preflight = live_launch_preflight(
                envelope["protocol"], adapter=adapter, evaluator_root=args.evaluator_root
            )
            redacted = _redacted_preflight(preflight)
            _emit({"status": "ready" if redacted["eligible"] else "blocked", "live": True, **redacted})
            return 0 if redacted["eligible"] else 3
        if args.command == "run":
            envelope = load_preregistration(args.prereg)
            verify_evaluator_root(envelope["protocol"], args.evaluator_root)
            if args.live:
                # Keep the launch-free preflight command structurally separate
                # from the live experiment runner: only the explicit live branch
                # imports the launching function.
                from scripts.orchestration.benchmark import run_live_experiment

                adapter = _live_adapter(evidence_path=args.preflight_evidence)
                preflight = live_launch_preflight(
                    envelope["protocol"], adapter=adapter, evaluator_root=args.evaluator_root
                )
                if not preflight["eligible"]:
                    _emit({"status": "blocked", "live": True, **preflight})
                    return 3
                report = run_live_experiment(
                    envelope, args.evaluator_root, args.output_root, adapter=adapter
                )
                _emit({"status": "completed", **report})
                return 0
            evidence = _json(args.preflight_evidence) if args.preflight_evidence else None
            if evidence is not None and not isinstance(evidence, dict):
                raise BenchmarkProtocolError("preflight evidence must be an object")
            report = run_fake_experiment(
                envelope,
                args.evaluator_root,
                args.output_root,
                preflight_evidence=evidence,
            )
            _emit({"status": "completed", **report})
            return 0
        if args.command == "evaluate":
            envelope = load_preregistration(args.prereg)
            trials = _json(args.trials)
            if not isinstance(trials, list):
                raise BenchmarkProtocolError("trials must be a JSON list")
            _emit(evaluate_with_replacements(envelope["protocol"], trials))
            return 0
        raise BenchmarkProtocolError("unsupported command")
    except BenchmarkProtocolError as exc:
        _emit({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
