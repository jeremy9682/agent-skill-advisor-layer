#!/usr/bin/env python3
"""Create one local, short-lived operator attestation for a frozen pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.orchestration import benchmark  # noqa: E402
from scripts.orchestration.attestation import (  # noqa: E402
    build_attested_evidence,
    parse_provider_observation,
    write_new_private_evidence,
)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prereg", type=Path, required=True, help="frozen preregistration envelope")
    parser.add_argument("--output", type=Path, required=True, help="new evidence file; existing files are refused")
    parser.add_argument("--attested-by", required=True, help="non-email operator token")
    parser.add_argument("--ttl-seconds", type=int, default=300)
    parser.add_argument("--provider-category", choices=("official", "proxy"), default="official")
    parser.add_argument(
        "--provider-observation",
        action="append",
        required=True,
        metavar="FAMILY:HEADROOM:AUTH:HOST:INCIDENT:COOLDOWN:RETRY_AFTER",
        help="explicit observation; every required family must appear once",
    )
    args = parser.parse_args(argv)
    try:
        envelope = benchmark.load_preregistration(args.prereg)
        observations: dict[str, dict[str, Any]] = {}
        for raw in args.provider_observation:
            family, row = parse_provider_observation(raw)
            if family in observations:
                raise benchmark.BenchmarkProtocolError("provider observation family is duplicated")
            observations[family] = row
        bundle = build_attested_evidence(
            envelope["protocol"],
            checkout_root=_REPO_ROOT,
            observations=observations,
            attested_by=args.attested_by,
            provider_category=args.provider_category,
            ttl_seconds=args.ttl_seconds,
        )
        output = write_new_private_evidence(args.output, bundle)
        _emit(
            {
                "status": "attested",
                "bundle_path": str(output),
                "bundle_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "expires_at": bundle["expires_at"],
                "required_provider_families": bundle["required_provider_families"],
            }
        )
        return 0
    except benchmark.BenchmarkProtocolError as exc:
        _emit({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
