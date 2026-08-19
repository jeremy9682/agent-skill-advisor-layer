#!/usr/bin/env bash
# Install this clone as the dispatch kit: agent-run launcher, optional DSH
# plugins from *this* repository, then gateway doctor.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
LAUNCHER="${ROOT}/scripts/agent_provider_run.py"
LEDGER_SRC="${ROOT}/scripts/agent_ledger.py"
AGENT_RUN="${BIN_DIR}/agent-run"
DISPATCH="${BIN_DIR}/agent-run-dispatch"
AGENT_LEDGER="${BIN_DIR}/agent-ledger"
LEDGER_DISPATCH="${BIN_DIR}/agent-ledger-dispatch"
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

# Occupied names (Beads agent-run, governance-clean agent-ledger, …) stay
# untouched. This clone is installed as the *-dispatch name instead.
install_cli_symlink() {
  local dest="$1"
  local dispatch="$2"
  local src="$3"
  local name="$4"
  local dispatch_name="$5"

  mkdir -p "$BIN_DIR"
  chmod +x "$src"

  if [[ ! -e "$dest" && ! -L "$dest" ]]; then
    ln -s "$src" "$dest"
    log "Linked ${dest} -> ${src}"
    return 0
  fi

  local current
  current="$(resolve_existing "$dest")"
  if [[ "$current" == "$src" ]]; then
    log "${name} already points at this clone."
    return 0
  fi

  if [[ -e "$dispatch" || -L "$dispatch" ]]; then
    local dispatch_target
    dispatch_target="$(resolve_existing "$dispatch")"
    if [[ "$dispatch_target" != "$src" ]]; then
      local bak="${dispatch}.bak.$(date +%Y%m%d%H%M%S)"
      mv "$dispatch" "$bak"
      log "Backed up existing ${dispatch} -> ${bak}"
    fi
  fi
  ln -sfn "$src" "$dispatch"
  warn "Existing ${dest} is not this clone (left untouched)."
  warn "  current target: ${current}"
  warn "Installed this clone as ${dispatch}"
  warn "Use ${dispatch_name} for this clone. Do not point PATH ${name} at dispatch when Beads already owns it."
}

dsh_pkg_root() {
  local bin
  bin="$(command -v dsh 2>/dev/null || true)"
  [[ -n "$bin" ]] || return 1
  python3 - "$bin" <<'PY'
import json, os, sys

path = os.path.realpath(sys.argv[1])
directory = os.path.dirname(path)
while True:
    pkg = os.path.join(directory, "package.json")
    if os.path.isfile(pkg):
        try:
            name = json.load(open(pkg, encoding="utf-8")).get("name")
        except Exception:
            name = None
        if name == "@deepseek-ai/dsh":
            print(directory)
            raise SystemExit(0)
    parent = os.path.dirname(directory)
    if parent == directory:
        raise SystemExit(1)
    directory = parent
PY
}

find_dsh_dep() {
  local name="$1"
  local root cand
  root="$(dsh_pkg_root)" || return 1
  for cand in \
    "${root}/node_modules/@deepseek-ai/${name}" \
    "$(dirname "$root")/${name}"; do
    if [[ -d "$cand" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

link_nm_pkg() {
  local dest_parent="$1"
  local name="$2"
  local src="$3"
  mkdir -p "${dest_parent}/@deepseek-ai"
  ln -sfn "$src" "${dest_parent}/@deepseek-ai/${name}"
  log "Linked ${dest_parent}/@deepseek-ai/${name} -> ${src}"
}

# Headless plugin boot needs these packages resolvable from the plugin files.
# Prefer a gitignored symlink into this clone from the already-installed DSH
# tree. Do not treat a home-directory manual symlink as the install path.
link_headless_peers() {
  local schema filesystem
  schema="$(find_dsh_dep schemastery || true)"
  filesystem="$(find_dsh_dep dsh-skill-filesystem || true)"
  if [[ -z "$schema" || -z "$filesystem" ]]; then
    warn "Could not find @deepseek-ai/schemastery and/or @deepseek-ai/dsh-skill-filesystem next to the installed dsh package."
    warn "Headless plugin boot may fail. Install official DSH (npm i -g @deepseek-ai/dsh) and re-run this script,"
    warn "or npm install those two packages into this clone (node_modules is gitignored). See README."
    return 0
  fi
  mkdir -p "${ROOT}/node_modules" "${ADAPTER_DIR}/node_modules" "${PACK_DIR}/node_modules"
  link_nm_pkg "${ROOT}/node_modules" schemastery "$schema"
  link_nm_pkg "${ROOT}/node_modules" dsh-skill-filesystem "$filesystem"
  link_nm_pkg "${ADAPTER_DIR}/node_modules" schemastery "$schema"
  link_nm_pkg "${PACK_DIR}/node_modules" dsh-skill-filesystem "$filesystem"
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
  link_headless_peers
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
  install_cli_symlink "$AGENT_RUN" "$DISPATCH" "$LAUNCHER" "agent-run" "agent-run-dispatch"
  install_cli_symlink "$AGENT_LEDGER" "$LEDGER_DISPATCH" "$LEDGER_SRC" "agent-ledger" "agent-ledger-dispatch"
  install_dsh_plugins
  print_mcp_snippet
  run_doctor
  log ""
  log "Done. Daily entry for this clone: agent-run-dispatch / agent-ledger-dispatch"
  log "(or python3 scripts/agent_ledger.py). PATH agent-run may be Beads — do not use it for dispatch."
  log "Third-party runtimes (install yourself): Cursor or Claude or Codex CLI,"
  log "optional official DSH. LiteLLM is not required. Do not clone our other repos."
}

main "$@"
