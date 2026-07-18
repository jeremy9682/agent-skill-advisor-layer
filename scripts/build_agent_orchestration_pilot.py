#!/usr/bin/env python3
"""Build a disposable, local-only Agent Run real-pilot fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.orchestration.benchmark_fixture import BenchmarkFixtureError, build_pilot_fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="new local directory for the disposable fixture")
    args = parser.parse_args(argv)
    try:
        fixture = build_pilot_fixture(args.root)
    except BenchmarkFixtureError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "built",
        "root": str(fixture.root),
        "fixture_repo_root": str(fixture.repo_root),
        "evaluator_root": str(fixture.evaluator_root),
        "protocol": str(fixture.protocol_path),
        "preregistration": str(fixture.preregistration_path),
        "base_sha": fixture.base_sha,
        "route_policy_sha256": fixture.route_policy_sha256,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
