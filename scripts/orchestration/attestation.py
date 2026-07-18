"""Local, explicit operator attestation for the live benchmark preflight.

This module deliberately has no provider clients.  It neither reads provider
configuration nor guesses capacity: a human supplies every observation and we
only bind that statement to this checkout for a short period.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping

from . import benchmark
from .runtime import BenchmarkLiveRuntimeAdapter


_ATTESTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OBSERVATION_KEYS = frozenset(
    {
        "auth_ok",
        "host_healthy",
        "provider_incident",
    }
)


def _utc_stamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _operator_token(value: str) -> str:
    if not _ATTESTER_RE.fullmatch(value):
        raise benchmark.BenchmarkProtocolError("attested_by must be a non-email operator token")
    return value


def parse_provider_observation(value: str) -> tuple[str, dict[str, Any]]:
    """Parse an explicit, credential-free CLI row.

    Format: ``family:auth_ok:host_healthy:provider_incident``.
    Bool fields must be literal ``true`` or ``false``.  There are intentionally
    no defaults: an operator needs to attest every field they observed.
    """

    pieces = value.split(":")
    if len(pieces) != 4 or not pieces[0]:
        raise benchmark.BenchmarkProtocolError("provider observation has invalid shape")
    family, auth, host, incident = pieces

    def boolean(raw: str, label: str) -> bool:
        if raw not in {"true", "false"}:
            raise benchmark.BenchmarkProtocolError(f"{family}: {label} must be true or false")
        return raw == "true"

    return family, {
        "auth_ok": boolean(auth, "auth_ok"),
        "host_healthy": boolean(host, "host_healthy"),
        "provider_incident": boolean(incident, "provider_incident"),
    }


def build_attested_evidence(
    protocol: Mapping[str, Any],
    *,
    checkout_root: Path,
    observations: Mapping[str, Mapping[str, Any]],
    attested_by: str,
    provider_category: str,
    ttl_seconds: int,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build and validate an exact-schema, current-checkout attestation."""

    frozen = benchmark.validate_executable_protocol(protocol)
    preflight_policy = frozen["provider_preflight_policy"]
    max_freshness = int(preflight_policy["max_freshness_seconds"])
    if provider_category not in {"official", "proxy"}:
        raise benchmark.BenchmarkProtocolError("provider category is invalid")
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or not 1 <= ttl_seconds <= max_freshness
    ):
        raise benchmark.BenchmarkProtocolError(
            f"ttl_seconds must be an integer from 1 to {max_freshness}"
        )
    attester = _operator_token(attested_by)
    families = frozen["required_provider_families"]
    if set(observations) != set(families):
        raise benchmark.BenchmarkProtocolError("provider observations must exactly match required provider families")
    if any(set(row) != _OBSERVATION_KEYS for row in observations.values()):
        raise benchmark.BenchmarkProtocolError("provider observation schema is invalid")

    adapter = BenchmarkLiveRuntimeAdapter(checkout_root)
    observed = now or dt.datetime.now(tz=dt.timezone.utc)
    if observed.tzinfo is None:
        raise benchmark.BenchmarkProtocolError("attestation clock must be timezone-aware")
    observed = observed.astimezone(dt.timezone.utc)
    bundle = {
        "version": preflight_policy["evidence_schema_version"],
        "observed_at": _utc_stamp(observed),
        "expires_at": _utc_stamp(observed + dt.timedelta(seconds=ttl_seconds)),
        "freshness_window_seconds": ttl_seconds,
        "attested_by": attester,
        "required_provider_families": list(families),
        "provider_families": {family: dict(observations[family]) for family in families},
        "config_fingerprint": adapter._config_fingerprint,
        "provider_category": provider_category,
        "host_identity": adapter._host_identity(),
        "checkout_identity": adapter._checkout_identity(),
        "route_policy_sha256": benchmark._frozen_route_policy_digest(frozen),
    }
    benchmark.validate_attested_evidence_bundle(
        bundle,
        frozen,
        now=observed,
        expected_host_identity=adapter._host_identity(),
        expected_checkout_identity=adapter._checkout_identity(),
        expected_config_fingerprint=adapter._config_fingerprint,
    )
    return bundle


def write_new_private_evidence(path: Path, bundle: Mapping[str, Any]) -> Path:
    """Create a new 0600 JSON file atomically; never replace an existing path."""

    raw_path = path.expanduser()
    if raw_path.name in {"", ".", ".."}:
        raise benchmark.BenchmarkProtocolError("evidence output path is invalid")
    if raw_path.is_symlink() or raw_path.exists():
        raise benchmark.BenchmarkProtocolError("evidence output already exists or is a symlink")
    raw_parent = raw_path.parent
    if raw_parent.is_symlink():
        raise benchmark.BenchmarkProtocolError("evidence output parent must not be a symlink")
    try:
        parent = raw_parent.resolve(strict=True)
    except OSError as exc:
        raise benchmark.BenchmarkProtocolError(
            "evidence output parent must already be a private directory"
        ) from exc
    if not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise benchmark.BenchmarkProtocolError(
            "evidence output parent must already have exact mode 0700"
        )
    destination = parent / raw_path.name
    if destination.exists() or destination.is_symlink():
        raise benchmark.BenchmarkProtocolError("evidence output already exists or is a symlink")
    payload = (benchmark.canonical_json(bundle) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, destination)
        except FileExistsError as exc:
            raise benchmark.BenchmarkProtocolError("evidence output already exists or is a symlink") from exc
        os.chmod(destination, 0o600)
        return destination
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
