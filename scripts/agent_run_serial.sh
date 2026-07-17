#!/usr/bin/env bash
# Passthrough to agent-run. Serial locking is owned by agent-run (ProviderSerialLock
# on ~/.agent-runs/locks/<serial_group>.lock). Do not wrap with flock here — that
# self-deadlocks because the runner acquires the same lock file on a new fd.
set -euo pipefail
exec agent-run run "$@"
