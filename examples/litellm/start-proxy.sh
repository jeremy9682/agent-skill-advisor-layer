#!/usr/bin/env bash
# Start a loopback LiteLLM proxy. Does not touch ~/.dsh/settings.yaml.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEFAULT_CONFIG="${HOME}/.config/litellm/config.yaml"
if [[ -f "$DEFAULT_CONFIG" ]]; then
  CONFIG="${1:-$DEFAULT_CONFIG}"
else
  CONFIG="${1:-$ROOT/examples/litellm/config.example.yaml}"
fi
HOST="${LITELLM_HOST:-127.0.0.1}"
PORT="${LITELLM_PORT:-4000}"

if ! command -v litellm >/dev/null 2>&1; then
  echo "litellm not on PATH. Install with: uv tool install litellm" >&2
  echo "Then add proxy deps; see docs/litellm-proxy.md (pin fastapi==0.136.3)." >&2
  exit 127
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "missing config: $CONFIG" >&2
  exit 1
fi

if [[ -z "${CMD_API_KEY:-}" ]]; then
  echo "CMD_API_KEY is unset; upstream Command Code calls will 401." >&2
fi
if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "LITELLM_MASTER_KEY is unset; set a local proxy key before pointing dsh at this port." >&2
fi

echo "LiteLLM $(litellm --version 2>/dev/null | tr '\n' ' ')"
echo "config=$CONFIG listen=http://${HOST}:${PORT} (loopback only; dsh settings unchanged)"
exec litellm --config "$CONFIG" --host "$HOST" --port "$PORT"
