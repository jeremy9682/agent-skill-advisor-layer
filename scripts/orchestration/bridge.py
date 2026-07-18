"""Thin, evidence-preserving bridge to the governed ``agent-run`` CLI.

The bridge owns the wrapper process lifecycle.  The scheduler owns admission and
is the only journal writer.  Attempt manifests and captured streams are private
recovery evidence; prompt/response bodies and full commands never enter the
journal.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence


class BridgeError(RuntimeError):
    """A task cannot be dispatched without violating the bridge contract."""


FORBIDDEN_TASK_OVERRIDES = {
    "provider", "model", "effort", "seat", "permission_profile",
    "approval_mode", "trust_workspace", "extra_args",
}
FORBIDDEN_ARG_TOKENS = {
    "--allow-all-tools", "--dangerously-skip-permissions", "--force",
    "--permission-mode", "--trust", "--trust-workspace", "--yolo",
}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
REVIEW_VERDICT_PREFIX = "AGENT_RUN_REVIEW_VERDICT:"
REVIEW_VERDICT_PASS = "AGENT_RUN_REVIEW_VERDICT: PASS"
REVIEW_VERDICT_FAIL = "AGENT_RUN_REVIEW_VERDICT: FAIL"
MAX_NATURAL_EXIT_SETTLE_SECONDS = 0.25
PREFLIGHT_REJECTION_KEYS = frozenset(
    {
        "receipt_kind",
        "run_id",
        "provider",
        "seat",
        "model",
        "exit_code",
        "failure_class",
        "session_status",
        "preflight_stage",
        "catalog_status",
        "catalog_attempts",
    }
)
PREFLIGHT_REJECTION_KIND = "preflight-rejection-v1"
PREFLIGHT_REJECTION_FAILURE = "provider-catalog-unavailable"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _private_atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _private_read(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeError("private attempt artifact is missing") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise BridgeError("private attempt artifact type or mode drift")
    return path.read_bytes()


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _start_fingerprint(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=2,
        )
        observed = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        observed = ""
    return _sha256(f"{pid}:{observed or 'unknown'}".encode())[:24]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_strict_catalog_preflight_rejection(receipt: Mapping[str, Any]) -> bool:
    return receipt.get("receipt_kind") == PREFLIGHT_REJECTION_KIND


def _validate_catalog_preflight_rejection(receipt: Mapping[str, Any]) -> None:
    if set(receipt) != PREFLIGHT_REJECTION_KEYS:
        raise BridgeError("agent_run strict preflight rejection has unexpected fields")
    required_strings = ("run_id", "provider", "seat", "model")
    if any(
        not isinstance(receipt.get(field), str) or not receipt[field]
        or not SAFE_ID_RE.fullmatch(receipt[field])
        for field in required_strings
    ):
        raise BridgeError("agent_run strict preflight rejection has incomplete identity")
    if receipt.get("exit_code") != 2:
        raise BridgeError("agent_run strict preflight rejection must use exit code 2")
    if receipt.get("failure_class") != PREFLIGHT_REJECTION_FAILURE:
        raise BridgeError("agent_run strict preflight rejection has invalid failure class")
    if receipt.get("session_status") != "not-started":
        raise BridgeError("agent_run strict preflight rejection must be sessionless")
    if receipt.get("preflight_stage") != "model-catalog":
        raise BridgeError("agent_run strict preflight rejection has invalid stage")
    if receipt.get("catalog_status") != "catalog-unavailable":
        raise BridgeError("agent_run strict preflight rejection has invalid catalog status")
    attempts = receipt.get("catalog_attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 3:
        raise BridgeError("agent_run strict preflight rejection has invalid retry count")


def parse_agent_run_output(stdout: str, stderr: str = "") -> tuple[dict[str, Any], str]:
    """Extract exactly one attributed machine receipt and its provider answer."""
    candidates: list[tuple[str, int, Mapping[str, Any]]] = []
    for stream_name, text in (("stdout", stdout), ("stderr", stderr)):
        for index, line in enumerate(text.splitlines()):
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, Mapping) and isinstance(payload.get("agent_run"), Mapping):
                candidates.append((stream_name, index, payload["agent_run"]))
    if len(candidates) != 1:
        raise BridgeError(f"expected exactly one agent_run receipt, found {len(candidates)}")
    stream_name, index, receipt = candidates[0]
    required = ("run_id", "provider", "seat", "exit_code", "failure_class", "model")
    missing = [field for field in required if receipt.get(field) in (None, "")]
    if missing:
        raise BridgeError(f"agent_run receipt missing fields: {', '.join(missing)}")
    if _is_strict_catalog_preflight_rejection(receipt):
        _validate_catalog_preflight_rejection(receipt)
        return dict(receipt), ""
    if receipt.get("session_status") not in {
        "attributed-stream-json", "attributed-single-artifact",
        "attributed-correlated-artifacts",
    }:
        raise BridgeError(
            f"agent_run receipt has unacceptable attribution: {receipt.get('session_status')!r}"
        )
    if not receipt.get("session_id"):
        raise BridgeError("agent_run receipt has no session_id")
    answer_lines = stdout.splitlines()
    if stream_name == "stdout":
        answer_lines = answer_lines[index + 1 :]
    return dict(receipt), "\n".join(answer_lines).strip()


def review_verdict_failure(answer: str) -> str | None:
    """Return a fail-closed reviewer verdict error, or ``None`` for PASS.

    Provider exit status only proves the invocation completed.  A governed
    reviewer must give exactly one machine-readable final verdict; prose after
    a PASS is deliberately rejected because it makes the terminal decision
    non-canonical during recovery.
    """

    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    markers = [line for line in lines if line.startswith(REVIEW_VERDICT_PREFIX)]
    if not markers:
        return "review-verdict-missing"
    if len(markers) != 1 or markers[0] not in {REVIEW_VERDICT_PASS, REVIEW_VERDICT_FAIL}:
        return "review-verdict-ambiguous"
    if not lines or lines[-1] != markers[0]:
        return "review-verdict-missing"
    if markers[0] == REVIEW_VERDICT_FAIL:
        return "review-verdict-fail"
    return None


def orchestration_failure_class(provider_failure_class: Any) -> str:
    """Project provider receipts onto the plan's bounded retry taxonomy."""

    failure_class = str(provider_failure_class or "none")
    if failure_class == "rate-limited":
        return "provider-rate-limit"
    if failure_class == PREFLIGHT_REJECTION_FAILURE:
        return "provider-preflight-transient"
    if failure_class in {
        "upstream-overload",
        "provider-error",
        "provider-start-failed",
        "network-error",
        "connection-error",
        "service-unavailable",
    }:
        return "provider-transient"
    return failure_class


def _unattributed_wrapper_exit(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    generation: int,
    returncode: int | None,
    wall_ms: int,
    metrics: Mapping[str, int],
    wrapper_pid: int | None = None,
    wrapper_start_fingerprint: str | None = None,
    launch_manifest_path: str | None = None,
    process_cleanup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed when a nonzero wrapper cannot prove what it did."""

    result: dict[str, Any] = {
        "task_id": task_id,
        "orchestration_run_id": run_id,
        "attempt_id": attempt_id,
        "generation": generation,
        "status": "failed-unsafe",
        "failure_class": "unattributed-wrapper-exit",
        "wrapper_exit_code": returncode,
        "bridge_wall_ms": int(wall_ms),
        **dict(metrics),
    }
    if wrapper_pid is not None:
        result["wrapper_pid"] = wrapper_pid
    if wrapper_start_fingerprint is not None:
        result["wrapper_start_fingerprint"] = wrapper_start_fingerprint
    if launch_manifest_path is not None:
        result["launch_manifest_path"] = launch_manifest_path
    if process_cleanup is not None:
        result["process_cleanup"] = dict(process_cleanup)
    return result


class BridgeLaunch:
    def __init__(
        self,
        *,
        process: subprocess.Popen[bytes] | None,
        run_id: str,
        task_id: str,
        attempt_id: str,
        generation: int,
        pid: int,
        process_group_id: int,
        start_fingerprint: str,
        deadline_epoch: float,
        manifest_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        metrics: dict[str, int],
        checkpoint_event: str,
        compiled_seat: str,
    ) -> None:
        self.process = process
        self.run_id = run_id
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.generation = generation
        self.pid = pid
        self.process_group_id = process_group_id
        self.start_fingerprint = start_fingerprint
        self.deadline_epoch = deadline_epoch
        self.manifest_path = manifest_path
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.metrics = metrics
        self.checkpoint_event = checkpoint_event
        self.compiled_seat = compiled_seat

    def journal_evidence(self) -> dict[str, Any]:
        return {
            "wrapper_pid": self.pid,
            "wrapper_start_fingerprint": self.start_fingerprint,
            "process_group_id": self.process_group_id,
            "deadline_at": _iso(self.deadline_epoch),
            "launch_manifest_path": str(self.manifest_path),
            "checkpoint_event": self.checkpoint_event,
            "compiled_seat": self.compiled_seat,
        }


class NativeAgentRunBridge:
    """Launch, recover, drain and collect governed ``agent-run`` wrappers."""

    owns_deadline = True
    two_phase_process = True

    def __init__(
        self,
        *,
        artifact_root: Path,
        binary: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        no_skills: bool = False,
        terminate_grace_seconds: float = 2.0,
        poll_seconds: float = 0.05,
        natural_exit_settle_seconds: float = 0.20,
    ) -> None:
        self.artifact_root = artifact_root.expanduser().resolve()
        # Never dispatch through a host-global ``agent-run`` symlink: it may
        # point at another checkout and silently lack the lifecycle contract
        # this bridge was compiled against.
        local_wrapper = Path(__file__).resolve().parents[1] / "agent_provider_run.py"
        self.binary_prefix = [binary] if binary else [sys.executable, str(local_wrapper)]
        self.runner = runner
        self.popen_factory = popen_factory
        self.no_skills = no_skills
        self.terminate_grace_seconds = terminate_grace_seconds
        self.poll_seconds = poll_seconds
        if (
            isinstance(natural_exit_settle_seconds, bool)
            or not isinstance(natural_exit_settle_seconds, (int, float))
            or not 0 < natural_exit_settle_seconds <= MAX_NATURAL_EXIT_SETTLE_SECONDS
        ):
            raise BridgeError(
                "natural_exit_settle_seconds must be a positive bounded duration"
            )
        self.natural_exit_settle_seconds = float(natural_exit_settle_seconds)

    def prepare_prompt(self, task: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
        started = time.perf_counter_ns()
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise BridgeError("task.prompt must be a non-empty string")
        fragments = task.get("context_fragments", [])
        if not isinstance(fragments, Sequence) or isinstance(fragments, (str, bytes)):
            raise BridgeError("task.context_fragments must be a list of strings")
        if not all(isinstance(item, str) for item in fragments):
            raise BridgeError("task.context_fragments must contain only strings")
        rendered = prompt
        if fragments:
            rendered += "\n\nContext supplied by the approved plan:\n" + "\n\n".join(fragments)
        elapsed_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        delivered = rendered.encode("utf-8")
        deduplicated = b"\n\n".join(dict.fromkeys(item.encode() for item in fragments))
        return rendered, {
            "context_construction_ms": int(elapsed_ms),
            "delivered_prompt_bytes": len(delivered),
            "deduplicated_shared_context_bytes": len(deduplicated),
        }

    def build_command(self, task: Mapping[str, Any], prompt: str) -> list[str]:
        forbidden = sorted(FORBIDDEN_TASK_OVERRIDES & set(task))
        if forbidden:
            raise BridgeError(
                "plan task may not override governed routing/authority: " + ", ".join(forbidden)
            )
        task_shape, checkpoint, cwd = task.get("task_shape"), task.get("checkpoint_event"), task.get("cwd")
        mode, deadline = task.get("mode", "read-only"), task.get("deadline_seconds")
        if not isinstance(task_shape, str) or not task_shape:
            raise BridgeError("task.task_shape is required")
        if not isinstance(checkpoint, str) or not checkpoint.startswith("evt-"):
            raise BridgeError("task.checkpoint_event is required for governed dispatch")
        if not isinstance(cwd, str) or not Path(cwd).expanduser().is_dir():
            raise BridgeError("task.cwd must be an existing directory")
        if mode not in {"read-only", "execute"}:
            raise BridgeError("task.mode must be read-only or execute")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0:
            raise BridgeError("task.deadline_seconds must be a positive integer")
        command = [
            *self.binary_prefix, "run", "auto", "--task-shape", task_shape,
            "--checkpoint-event", checkpoint, "--cwd", str(Path(cwd).expanduser().resolve()),
            "--mode", mode, "--timeout-seconds", str(deadline),
        ]
        if mode == "execute":
            command.append("--allow-write")
        if self.no_skills or task.get("disable_managed_skills") is True:
            command.append("--no-skills")
        bundle_path = task.get("producer_review_bundle")
        if bundle_path is not None:
            required = {
                "producer_review_bundle_sha256",
                "orchestration_run_id",
                "orchestration_generation",
                "orchestration_fencing_token",
                "orchestration_reviewer_task_id",
                "orchestration_reviewer_attempt_id",
            }
            missing = sorted(key for key in required if task.get(key) in {None, ""})
            if missing:
                raise BridgeError(
                    "review bundle is missing runtime binding: " + ", ".join(missing)
                )
            path = Path(str(bundle_path)).expanduser()
            try:
                info = path.lstat()
            except OSError as exc:
                raise BridgeError("review bundle is unavailable") from exc
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise BridgeError("review bundle must be a private regular file")
            digest = str(task["producer_review_bundle_sha256"])
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha256(path.read_bytes()) != digest:
                raise BridgeError("review bundle digest drift")
            command.extend(
                [
                    "--producer-review-bundle", str(path.resolve()),
                    "--producer-review-bundle-sha256", digest,
                    "--orchestration-run-id", str(task["orchestration_run_id"]),
                    "--orchestration-generation", str(task["orchestration_generation"]),
                    "--orchestration-fencing-token", str(task["orchestration_fencing_token"]),
                    "--orchestration-reviewer-task-id", str(task["orchestration_reviewer_task_id"]),
                    "--orchestration-reviewer-attempt-id", str(task["orchestration_reviewer_attempt_id"]),
                ]
            )
        command.append(prompt)
        if any(token in FORBIDDEN_ARG_TOKENS for token in command):
            raise BridgeError("bridge attempted to expand native provider authority")
        return command

    def _attempt_paths(self, run_id: str, task_id: str, attempt_id: str) -> tuple[Path, Path, Path, Path]:
        if not all(SAFE_ID_RE.fullmatch(value) for value in (run_id, task_id, attempt_id)):
            raise BridgeError("run/task/attempt identity is unsafe for private artifact paths")
        directory = (self.artifact_root / run_id / task_id / attempt_id).resolve()
        try:
            directory.relative_to(self.artifact_root)
        except ValueError as exc:
            raise BridgeError("attempt artifact path escaped private root") from exc
        return directory, directory / "attempt-manifest.json", directory / "stdout.bin", directory / "stderr.bin"

    def launch_task(
        self, task: Mapping[str, Any], *, run_id: str, attempt_id: str,
        generation: int, deadline_at: str | None = None,
    ) -> BridgeLaunch:
        if not run_id or not attempt_id or generation < 1:
            raise BridgeError("run_id, attempt_id, and positive generation are required")
        prompt, metrics = self.prepare_prompt(task)
        command = self.build_command(task, prompt)
        compiled_seat = task.get("compiled_seat")
        if not isinstance(compiled_seat, str) or not compiled_seat:
            raise BridgeError("task.compiled_seat is required for checkpoint recovery")
        task_id = str(task.get("task_id") or task.get("id") or "unknown")
        directory, manifest, stdout_path, stderr_path = self._attempt_paths(run_id, task_id, attempt_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        if any(path.exists() for path in (manifest, stdout_path, stderr_path)):
            raise BridgeError("attempt artifacts already exist; launch replay refused")
        deadline_epoch = time.time() + int(task["deadline_seconds"])
        if deadline_at:
            try:
                deadline_epoch = dt.datetime.fromisoformat(deadline_at.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError) as exc:
                raise BridgeError("deadline_at is invalid") from exc
        created = {
            "version": 1, "run_id": run_id, "task_id": task_id,
            "attempt_id": attempt_id, "generation": generation, "status": "created",
            "deadline_at": _iso(deadline_epoch), "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "checkpoint_event": str(task["checkpoint_event"]),
            "compiled_seat": str(task.get("compiled_seat") or ""),
            # Persist only the review-role bit: recovery must enforce the same
            # verdict contract without serialising task graph details.
            "reviewer_for": bool(task.get("reviewer_for")),
        }
        if task.get("producer_review_bundle") is not None:
            created.update(
                {
                    "producer_review_bundle": str(task["producer_review_bundle"]),
                    "producer_review_bundle_sha256": str(
                        task["producer_review_bundle_sha256"]
                    ),
                    "orchestration_fencing_token": str(
                        task["orchestration_fencing_token"]
                    ),
                    "orchestration_reviewer_attempt_id": str(
                        task["orchestration_reviewer_attempt_id"]
                    ),
                }
            )
        _private_write(manifest, (json.dumps(created, sort_keys=True, separators=(",", ":")) + "\n").encode())
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(stdout_fd, "wb") as stdout_handle, os.fdopen(stderr_fd, "wb") as stderr_handle:
                process = self.popen_factory(
                    command, cwd=str(Path(str(task["cwd"])).expanduser().resolve()),
                    stdin=subprocess.DEVNULL, stdout=stdout_handle, stderr=stderr_handle,
                    start_new_session=True, close_fds=True,
                )
        except Exception:
            _private_atomic_json(manifest, {**created, "status": "launch-failed"})
            raise
        pid = int(process.pid)
        fingerprint = _start_fingerprint(pid)
        try:
            pgid = os.getpgid(pid)
        except OSError as exc:
            try:
                process.kill()
            except OSError:
                pass
            _private_atomic_json(manifest, {**created, "status": "launch-identity-failed", "wrapper_pid": pid})
            raise BridgeError("could not establish wrapper process-group identity") from exc
        running = {
            **created, "status": "running", "wrapper_pid": pid,
            "wrapper_start_fingerprint": fingerprint, "process_group_id": pgid,
            "launched_at": _iso(time.time()),
        }
        _private_atomic_json(manifest, running)
        return BridgeLaunch(
            process=process, run_id=run_id, task_id=task_id, attempt_id=attempt_id,
            generation=generation, pid=pid, process_group_id=pgid,
            start_fingerprint=fingerprint, deadline_epoch=deadline_epoch,
            manifest_path=manifest, stdout_path=stdout_path, stderr_path=stderr_path,
            metrics=metrics, checkpoint_event=str(task["checkpoint_event"]),
            compiled_seat=compiled_seat,
        )

    def _terminate_group(self, launch: BridgeLaunch) -> dict[str, Any]:
        term_sent = kill_sent = False
        term_permission_denied = kill_permission_denied = False
        if _group_alive(launch.process_group_id):
            try:
                os.killpg(launch.process_group_id, signal.SIGTERM)
                term_sent = True
            except ProcessLookupError:
                pass
            except PermissionError:
                term_permission_denied = True
        until = time.time() + self.terminate_grace_seconds
        while _group_alive(launch.process_group_id) and time.time() < until:
            time.sleep(self.poll_seconds)
        if _group_alive(launch.process_group_id):
            try:
                os.killpg(launch.process_group_id, signal.SIGKILL)
                kill_sent = True
            except ProcessLookupError:
                pass
            except PermissionError:
                kill_permission_denied = True
        until = time.time() + max(0.2, self.terminate_grace_seconds)
        while _group_alive(launch.process_group_id) and time.time() < until:
            time.sleep(self.poll_seconds)
        residual = _group_alive(launch.process_group_id)
        return {
            "status": "failed" if residual else "succeeded",
            "term_sent": term_sent,
            "kill_sent": kill_sent,
            "term_permission_denied": term_permission_denied,
            "kill_permission_denied": kill_permission_denied,
            "residual": residual,
        }

    def _group_persists_after_natural_exit_settle(self, launch: BridgeLaunch) -> bool:
        """Give a just-exited wrapper's descendants a tiny bounded exit window.

        The group identity is the PGID captured from the launched wrapper.  This
        method only delays cleanup; it never turns a still-live group into a
        success and it never widens the process target beyond that recorded PGID.
        """

        if not _group_alive(launch.process_group_id):
            return False
        deadline = time.monotonic() + self.natural_exit_settle_seconds
        while _group_alive(launch.process_group_id):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, max(0.001, self.poll_seconds)))
        return _group_alive(launch.process_group_id)

    def _wait(self, launch: BridgeLaunch) -> tuple[int | None, bool, dict[str, Any]]:
        timed_out = False
        if launch.process is not None:
            remaining = max(0.0, launch.deadline_epoch - time.time())
            try:
                returncode = launch.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                cleanup = self._terminate_group(launch)
                try:
                    returncode = launch.process.wait(timeout=max(0.2, self.terminate_grace_seconds))
                except subprocess.TimeoutExpired:
                    returncode = None
                # A killed wrapper may remain visible as a zombie until the
                # owning controller reaps it.  Residual verification is final
                # only after that wait.
                residual = _group_alive(launch.process_group_id)
                cleanup["residual"] = residual
                cleanup["status"] = "failed" if residual else "succeeded"
                return returncode, timed_out, cleanup
        else:
            while _pid_alive(launch.pid) and time.time() < launch.deadline_epoch:
                if _start_fingerprint(launch.pid) != launch.start_fingerprint:
                    return None, False, {"status": "failed", "residual": True, "identity_drift": True}
                time.sleep(self.poll_seconds)
            if _pid_alive(launch.pid):
                timed_out = True
                return None, timed_out, self._terminate_group(launch)
            returncode = None
        # ``wait``/the PID-fingerprint loop established that this exact wrapper
        # has exited.  Before sending a signal to its recorded PGID, tolerate a
        # short-lived provider child that is already naturally shutting down.
        if self._group_persists_after_natural_exit_settle(launch):
            # Clean up a leaked descendant, but retain failed-unsafe semantics.
            cleanup = self._terminate_group(launch)
            cleanup["unexpected_residual"] = True
            return returncode, timed_out, cleanup
        return returncode, timed_out, {"status": "succeeded", "residual": False}

    def collect_task(self, launch: BridgeLaunch) -> dict[str, Any]:
        started = time.perf_counter_ns()
        returncode, timed_out, cleanup = self._wait(launch)
        wall_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        if cleanup.get("status") != "succeeded" or cleanup.get("unexpected_residual"):
            result = {
                "status": "failed-unsafe", "failure_class": "residual-provider-process",
                "wrapper_pid": launch.pid, "wrapper_start_fingerprint": launch.start_fingerprint,
                "launch_manifest_path": str(launch.manifest_path), "process_cleanup": cleanup,
                **launch.metrics,
            }
            _private_atomic_json(launch.manifest_path, {**self._read_manifest(launch.manifest_path), "status": "failed-unsafe", "process_cleanup": cleanup})
            return result
        if timed_out:
            result = {
                "status": "timed-out", "failure_class": "deadline-exceeded",
                "wrapper_pid": launch.pid, "wrapper_start_fingerprint": launch.start_fingerprint,
                "launch_manifest_path": str(launch.manifest_path), "process_cleanup": cleanup,
                **launch.metrics,
            }
            _private_atomic_json(launch.manifest_path, {**self._read_manifest(launch.manifest_path), "status": "timed-out", "process_cleanup": cleanup})
            return result
        stdout = _private_read(launch.stdout_path).decode("utf-8", errors="replace")
        stderr = _private_read(launch.stderr_path).decode("utf-8", errors="replace")
        try:
            receipt, answer = parse_agent_run_output(stdout, stderr)
        except BridgeError:
            if returncode == 2:
                result = _unattributed_wrapper_exit(
                    run_id=launch.run_id,
                    task_id=launch.task_id,
                    attempt_id=launch.attempt_id,
                    generation=launch.generation,
                    returncode=returncode,
                    wall_ms=wall_ms,
                    metrics=launch.metrics,
                    wrapper_pid=launch.pid,
                    wrapper_start_fingerprint=launch.start_fingerprint,
                    launch_manifest_path=str(launch.manifest_path),
                    process_cleanup=cleanup,
                )
                _private_atomic_json(
                    launch.manifest_path,
                    {
                        **self._read_manifest(launch.manifest_path),
                        "status": "failed-unsafe",
                        "wrapper_exit_code": returncode,
                        "process_cleanup": cleanup,
                    },
                )
                return result
            raise
        receipt_exit = int(receipt["exit_code"])
        if returncode is not None and int(returncode) != receipt_exit:
            raise BridgeError("wrapper exit code does not match machine receipt")
        answer_bytes = answer.encode()
        answer_path = launch.manifest_path.parent / "provider-answer.txt"
        if not answer_path.exists():
            _private_write(answer_path, answer_bytes)
        elif _private_read(answer_path) != answer_bytes:
            raise BridgeError("provider answer artifact drifted during recovery")
        failure_class = orchestration_failure_class(receipt["failure_class"])
        status = "succeeded" if receipt_exit == 0 and failure_class == "none" else "failed"
        if status == "succeeded" and self._read_manifest(launch.manifest_path).get("reviewer_for"):
            verdict_failure = review_verdict_failure(answer)
            if verdict_failure is not None:
                status, failure_class = "failed", verdict_failure
        preflight_rejection = _is_strict_catalog_preflight_rejection(receipt)
        result = {
            "task_id": launch.task_id, "orchestration_run_id": launch.run_id,
            "attempt_id": launch.attempt_id, "generation": launch.generation,
            "status": status, "failure_class": failure_class,
            "provider_run_id": str(receipt["run_id"]), "provider": str(receipt["provider"]),
            "model_observed": str(receipt["model"]),
            "session_status": str(receipt["session_status"]), "exit_code": receipt_exit,
            "artifact_path": str(answer_path), "artifact_sha256": _sha256(answer_bytes),
            "provider_duration_ms": 0 if preflight_rejection else int(receipt.get("duration_ms") or wall_ms),
            "bridge_wall_ms": int(wall_ms), "wrapper_pid": launch.pid,
            "wrapper_start_fingerprint": launch.start_fingerprint,
            "launch_manifest_path": str(launch.manifest_path), "process_cleanup": cleanup,
            **launch.metrics,
        }
        if not preflight_rejection:
            result["session_id"] = str(receipt["session_id"])
        if receipt.get("model_observed"):
            result["model_observed"] = str(receipt["model_observed"])
        if receipt.get("model_family"):
            result["model_family"] = str(receipt["model_family"])
        _private_atomic_json(
            launch.manifest_path,
            {**self._read_manifest(launch.manifest_path), "status": "terminal",
             "wrapper_exit_code": receipt_exit, "receipt_sha256": _sha256(json.dumps(receipt, sort_keys=True).encode()),
             "process_cleanup": cleanup},
        )
        return result

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(_private_read(path))
        except (OSError, ValueError) as exc:
            raise BridgeError("attempt manifest is missing or invalid") from exc
        if not isinstance(value, Mapping) or value.get("version") != 1:
            raise BridgeError("attempt manifest is missing or invalid")
        return dict(value)

    def attempt_manifest(
        self, *, run_id: str, task_id: str, attempt_id: str
    ) -> dict[str, Any]:
        """Return private recovery identity, never stream or command content."""
        _directory, manifest, _stdout, _stderr = self._attempt_paths(
            run_id, task_id, attempt_id
        )
        return self._read_manifest(manifest)

    def reconcile_task(
        self, task: Mapping[str, Any], *, run_id: str, attempt_id: str,
        generation: int, prior_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        task_id = str(task.get("task_id") or task.get("id") or "unknown")
        _directory, manifest_path, stdout_path, stderr_path = self._attempt_paths(run_id, task_id, attempt_id)
        if not manifest_path.is_file():
            return {"status": "failed-unsafe", "failure_class": "attempt-manifest-missing", "replayed": False}
        try:
            manifest = self._read_manifest(manifest_path)
            expected = {"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id}
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise BridgeError("attempt manifest identity drift")
            bundle_path = manifest.get("producer_review_bundle")
            if bundle_path is not None:
                bundle, digest = self._verified_review_bundle(
                    bundle_path,
                    manifest.get("producer_review_bundle_sha256"),
                )
                if (
                    bundle.get("orchestration_run_id") != run_id
                    or bundle.get("reviewer_task_id") != task_id
                    or bundle.get("reviewer_attempt_id") != attempt_id
                    or bundle.get("fencing_token")
                    != manifest.get("orchestration_fencing_token")
                    or digest != manifest.get("producer_review_bundle_sha256")
                ):
                    raise BridgeError("review bundle identity drift during recovery")
            if manifest.get("status") == "created":
                return {
                    "status": "failed-unsafe",
                    "failure_class": "launch-window-unverifiable-orphan-preserved",
                    "replayed": False,
                    "resource_preserved": True,
                }
            if manifest.get("status") in {"launch-failed", "launch-identity-failed"}:
                return {
                    "status": "failed-unsafe",
                    "failure_class": "wrapper-launch-unconfirmed",
                    "replayed": False,
                    "resource_preserved": True,
                }
            pid, pgid = int(manifest["wrapper_pid"]), int(manifest["process_group_id"])
            fingerprint = str(manifest["wrapper_start_fingerprint"])
            deadline_epoch = dt.datetime.fromisoformat(str(manifest["deadline_at"]).replace("Z", "+00:00")).timestamp()
            if _pid_alive(pid) and _start_fingerprint(pid) != fingerprint:
                return {"status": "failed-unsafe", "failure_class": "stale-or-reused-wrapper-pid", "replayed": False}
            launch = BridgeLaunch(
                process=None, run_id=run_id, task_id=task_id, attempt_id=attempt_id,
                generation=generation, pid=pid, process_group_id=pgid,
                start_fingerprint=fingerprint, deadline_epoch=deadline_epoch,
                manifest_path=manifest_path, stdout_path=stdout_path,
                stderr_path=stderr_path, metrics={},
                checkpoint_event=str(manifest.get("checkpoint_event") or ""),
                compiled_seat=str(manifest.get("compiled_seat") or ""),
            )
            result = self.collect_task(launch)
            result["replayed"] = False
            result["reconciled"] = True
            return result
        except (BridgeError, KeyError, TypeError, ValueError, OSError):
            return {"status": "failed-unsafe", "failure_class": "attempt-manifest-unreconciled", "replayed": False}

    @staticmethod
    def _verified_review_bundle(path_raw: Any, digest_raw: Any) -> tuple[dict[str, Any], str]:
        path = Path(str(path_raw)).expanduser()
        try:
            info = path.lstat()
            data = path.read_bytes()
        except OSError as exc:
            raise BridgeError("review bundle is unavailable") from exc
        digest = _sha256(data)
        if (
            path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(info.st_mode) != 0o600
            or digest != digest_raw
        ):
            raise BridgeError("review bundle type, mode, or hash drift")
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BridgeError("review bundle is invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise BridgeError("review bundle is invalid JSON")
        return dict(value), digest

    def _run_legacy(self, task: Mapping[str, Any], *, run_id: str, attempt_id: str, generation: int) -> dict[str, Any]:
        assert self.runner is not None
        prompt, metrics = self.prepare_prompt(task)
        command = self.build_command(task, prompt)
        started = time.perf_counter_ns()
        completed = self.runner(
            command, cwd=str(Path(str(task["cwd"])).expanduser().resolve()),
            capture_output=True, text=True, check=False,
        )
        wall_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        task_id = str(task.get("task_id") or task.get("id") or "unknown")
        try:
            receipt, answer = parse_agent_run_output(
                completed.stdout or "", completed.stderr or ""
            )
        except BridgeError:
            if int(completed.returncode) == 2:
                return _unattributed_wrapper_exit(
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    generation=generation,
                    returncode=int(completed.returncode),
                    wall_ms=wall_ms,
                    metrics=metrics,
                )
            raise
        receipt_exit = int(receipt["exit_code"])
        if int(completed.returncode) != receipt_exit:
            raise BridgeError("wrapper exit code does not match machine receipt")
        directory, _manifest, _stdout, _stderr = self._attempt_paths(run_id, task_id, attempt_id)
        answer_path, answer_bytes = directory / "provider-answer.txt", answer.encode()
        _private_write(answer_path, answer_bytes)
        failure_class = orchestration_failure_class(receipt["failure_class"])
        status = "succeeded" if receipt_exit == 0 and failure_class == "none" else "failed"
        if status == "succeeded" and task.get("reviewer_for"):
            verdict_failure = review_verdict_failure(answer)
            if verdict_failure is not None:
                status, failure_class = "failed", verdict_failure
        preflight_rejection = _is_strict_catalog_preflight_rejection(receipt)
        result = {
            "task_id": task_id, "orchestration_run_id": run_id, "attempt_id": attempt_id,
            "generation": generation, "status": status,
            "failure_class": failure_class, "provider_run_id": str(receipt["run_id"]),
            "provider": str(receipt["provider"]), "model_observed": str(receipt["model"]),
            "session_status": str(receipt["session_status"]),
            "exit_code": receipt_exit, "artifact_path": str(answer_path),
            "artifact_sha256": _sha256(answer_bytes),
            "provider_duration_ms": 0 if preflight_rejection else int(receipt.get("duration_ms") or wall_ms),
            "bridge_wall_ms": int(wall_ms), **metrics,
        }
        if not preflight_rejection:
            result["session_id"] = str(receipt["session_id"])
        return result

    def run_task(self, task: Mapping[str, Any], *, run_id: str, attempt_id: str, generation: int) -> dict[str, Any]:
        if self.runner is not None:
            return self._run_legacy(task, run_id=run_id, attempt_id=attempt_id, generation=generation)
        launch = self.launch_task(task, run_id=run_id, attempt_id=attempt_id, generation=generation)
        return self.collect_task(launch)


__all__ = [
    "BridgeError", "BridgeLaunch", "FORBIDDEN_ARG_TOKENS", "FORBIDDEN_TASK_OVERRIDES",
    "NativeAgentRunBridge", "orchestration_failure_class", "parse_agent_run_output",
    "review_verdict_failure",
]
