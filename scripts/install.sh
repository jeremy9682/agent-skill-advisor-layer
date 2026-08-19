#!/usr/bin/env bash
# Install this clone as the dispatch kit: agent-run launcher, optional DSH
# plugins from *this* repository, then gateway doctor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
LAUNCHER="${ROOT}/scripts/agent_provider_run.py"
AGENT_RUN="${BIN_DIR}/agent-run"
DISPATCH="${BIN_DIR}/agent-run-dispatch"
ADAPTER_DIR="${ROOT}/plugins/dsh-llm-cursor-acp"
PACK_DIR="${ROOT}/plugins/dsh-dispatch-pack"

log() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

resolve_existing() {
  local path="$1"
  if [[ -L "$path" ]]; then
    readlink "$path" || true
  elif [[ -e "$path" ]]; then
    printf '%s\n' "$path"
  else
    printf '\n'
  fi
}

is_this_clone_launcher() {
  local target="$1"
  [[ -n "$target" && "$target" == "$LAUNCHER" ]]
}

looks_like_foreign_wrapper() {
  local path="$1"
  local target
  target="$(resolve_existing "$path")"
  if [[ -z "$target" ]]; then
    return 1
  fi
  if is_this_clone_launcher "$target"; then
    return 1
  fi
  if grep -q 'Beads dispatch bridge' "$path" 2>/dev/null; then
    return 0
  fi
  if [[ "$target" == *agent_run_beads_bridge.py ]]; then
    return 0
  fi
  return 0
}

install_agent_run() {
  mkdir -p "$BIN_DIR"
  chmod +x "$LAUNCHER"

  if [[ ! -e "$AGENT_RUN" && ! -L "$AGENT_RUN" ]]; then
    ln -s "$LAUNCHER" "$AGENT_RUN"
    log "Linked ${AGENT_RUN} -> ${LAUNCHER}"
    return 0
  fi

  local current
  current="$(resolve_existing "$AGENT_RUN")"
  if is_this_clone_launcher "$current"; then
    log "agent-run already points at this clone."
    return 0
  fi

  if looks_like_foreign_wrapper "$AGENT_RUN"; then
    if [[ -e "$DISPATCH" || -L "$DISPATCH" ]]; then
      local dispatch_target
      dispatch_target="$(resolve_existing "$DISPATCH")"
      if ! is_this_clone_launcher "$dispatch_target"; then
        local bak="${DISPATCH}.bak.$(date +%Y%m%d%H%M%S)"
        mv "$DISPATCH" "$bak"
        log "Backed up existing ${DISPATCH} -> ${bak}"
      fi
    fi
    ln -sfn "$LAUNCHER" "$DISPATCH"
    warn "Existing ${AGENT_RUN} is not this clone (left untouched)."
    warn "  current target: ${current}"
    warn "Installed this clone as ${DISPATCH}"
    warn "Use agent-run-dispatch, or put ${BIN_DIR} first on PATH after you choose to switch."
    return 0
  fi

  local bak="${AGENT_RUN}.bak.$(date +%Y%m%d%H%M%S)"
  mv "$AGENT_RUN" "$bak"
  ln -s "$LAUNCHER" "$AGENT_RUN"
  log "Backed up previous agent-run -> ${bak}"
  log "Linked ${AGENT_RUN} -> ${LAUNCHER}"
}

dsh_plugin_add_both() {
  local spec="$1"
  local profile
  for profile in web headless; do
    log "dsh plugin --profile ${profile} add ${spec}"
    if ! dsh plugin --profile "$profile" add "$spec"; then
      warn "dsh plugin add failed for profile=${profile} spec=${spec}"
    fi
  done
}

install_dsh_plugins() {
  if ! need_cmd dsh; then
    log "dsh not on PATH — skip plugin add. Optional: npm i -g @deepseek-ai/dsh"
    return 0
  fi
  dsh_plugin_add_both "$PACK_DIR"
  dsh_plugin_add_both "$ADAPTER_DIR"
}

print_mcp_snippet() {
  log ""
  log "ZCode MCP snippet (merge into ~/.zcode/cli/config.json). Replace nothing — path is this clone:"
  cat <<EOF
{
  "mcp": {
    "servers": {
      "dsh": {
        "command": "node",
        "args": ["${ROOT}/server/dsh-mcp.mjs"],
        "env": {
          "DSH_HOME": "~/.dsh",
          "DSH_MCP_PROFILE": "headless"
        }
      }
    }
  }
}
EOF
}

run_doctor() {
  if ! need_cmd node; then
    warn "node not on PATH; skipped gateway doctor. Need Node.js >= 22."
    return 0
  fi
  log ""
  log "node ${ROOT}/gateway/local-gateway.mjs doctor"
  node "${ROOT}/gateway/local-gateway.mjs" doctor
}

main() {
  log "REPO_ROOT=${ROOT}"
  install_agent_run
  install_dsh_plugins
  print_mcp_snippet
  run_doctor
  log ""
  log "Done. Third-party runtimes (install yourself): Cursor or Claude or Codex CLI,"
  log "optional official DSH, optional LiteLLM. Do not clone our other repos."
}

main "$@"
