# LiteLLM proxy (quota routing only)

LiteLLM is the **optional** spend/endpoint gateway. DSH still owns task grading
and seats (`docs/model-dispatch-matrix.md`, `routing-policy.yaml`). This repo
ships an example config and a start script. It does **not** rewrite
`~/.dsh/settings.yaml`.

## Install (this host)

Isolated user tool, no project venv, no git-config changes:

```bash
uv tool install litellm
# Proxy extras: full `litellm[proxy]` pulls polars/azure/mcp (~minutes on this
# host). Minimal set that actually boots 1.97.0:
uv pip install --python ~/.local/share/uv/tools/litellm/bin/python \
  'fastapi==0.136.3' 'starlette==0.48.0' gunicorn 'uvicorn[standard]' \
  websockets redis boto3 backoff orjson apscheduler python-multipart \
  'PyJWT>=2.13' cryptography pynacl rq hiredis fastapi-sso \
  restrictedpython rich InquirerPy expression \
  'litellm-proxy-extras==0.4.84' 'litellm-enterprise==0.1.54'
```

Pin **FastAPI 0.136.3**. 0.141+ removes `get_flat_dependant` and LiteLLM 1.97
will not start. Confirm the tool env (this host: **1.97.0**):

```bash
~/.local/share/uv/tools/litellm/bin/python -c \
  "from importlib.metadata import version; print(version('litellm'))"
```

## Example config (no secrets)

- [`examples/litellm/config.example.yaml`](../examples/litellm/config.example.yaml)
  — OpenAI-compatible Command Code placeholder + budget router keys.
- [`examples/litellm/start-proxy.sh`](../examples/litellm/start-proxy.sh) — bind
  loopback only.

Copy the example **outside git** (recommended: `~/.config/litellm/`), export keys in
the **environment**, never commit them:

```bash
mkdir -p ~/.config/litellm
cp examples/litellm/config.example.yaml ~/.config/litellm/config.yaml
# edit ~/.config/litellm/config.yaml if you need extra models
cat > ~/.config/litellm/env.local <<'EOF'
export CMD_API_KEY='…'                   # Command Code; do not paste into git
export LITELLM_MASTER_KEY='sk-local-…'   # proxy auth; local only
EOF
chmod 600 ~/.config/litellm/env.local
source ~/.config/litellm/env.local
examples/litellm/start-proxy.sh ~/.config/litellm/config.yaml
```

Default listen: `http://127.0.0.1:4000`. Prefer `GET /v1/models` as the smoke
check; `GET /health` can 500 until real upstream keys exist.

## Point dsh `pi-ai` `baseURL` at the proxy (manual, after health)

Current production `~/.dsh/settings.yaml` `llm-pi-ai` providers stay on their
vendor URLs until you **explicitly** switch. Do not do this while sessions are
in-flight.

1. Confirm the proxy is listening: `curl -sS http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"`.
2. Back up settings: `cp ~/.dsh/settings.yaml ~/.dsh/settings.yaml.bak`.
3. Add or clone a **new** `llm-pi-ai.providers.*` entry (keep the old one). Example
   name: `commandcode-litellm`. Shape matches Command Code today:

```yaml
llm-pi-ai:
  providers:
    commandcode-litellm:
      displayName: Command Code via LiteLLM (opt-in)
      apiKeyEnv: LITELLM_MASTER_KEY
      api: openai-completions
      baseURL: http://127.0.0.1:4000/v1
      compat:
        thinkingFormat: openai
      models:
        - id: gpt-5.6-sol
          name: GPT-5.6 Sol via LiteLLM
```

4. Point `agent-default-model` at that provider **only after** a one-shot
   `dsh --profile headless` probe succeeds. Leave `llm-cursor-acp` / vendor
   `commandcode` entries untouched as rollback. Opt-in probes need
   `LITELLM_MASTER_KEY` (e.g. `source ~/.config/litellm/env.local`).

OpenAI-compatible Command Code origin used on this host (do not commit keys):
`https://api.commandcode.ai/provider/v1`. Anthropic-compatible Command Code is
a separate `baseURL` (`…/provider` without `/v1`). LiteLLM `openai/` models
must use the `/v1` origin.

## Verify

```bash
curl -sS http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
# /health may 500 until upstream keys are real; /v1/models is the smoke check.
```

Then a DSH one-shot against a **throwaway** cwd, not a live coding session.

## Rollback

1. Stop the proxy (Ctrl-C / kill the start-script process).
2. Restore `~/.dsh/settings.yaml` from `.bak`, or switch
   `agent-default-model` back to the previous provider (`llm-cursor-acp` /
   vendor `commandcode` / `zcode`).
3. Do not leave `baseURL: http://127.0.0.1:4000` in place if the process is down.

## Host status (2026-08-19, this machine)

- LiteLLM is listening on `127.0.0.1:4000`; `GET /v1/models` returned 200.
- DSH gained an **opt-in** `llm-pi-ai` provider `commandcode-litellm`. Default
  routing is unchanged. This repo still does **not** rewrite
  `~/.dsh/settings.yaml`.

## Out of scope (this batch)

- Switching production default traffic
- ZCode HTTP gateway (see [`zcode-cloud-gateway.md`](zcode-cloud-gateway.md))
- Enabling `subagent-command-code`
