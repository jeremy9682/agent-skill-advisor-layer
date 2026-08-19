# DSH Cursor ACP provider

Native DeepSeek Harness model provider for models available through a signed-in Cursor subscription. Vendored into this clone from our integration kit; see `VENDOR.md`.

The package registers provider ID `cursor-acp`. Models appear in the DSH Models UI with stable IDs such as `cursor-grok-4.6-high`.

## Requirements

- Official DSH (`npm i -g @deepseek-ai/dsh`), typically `0.1.0-rc.6+`
- macOS with `/usr/bin/sandbox-exec`
- Official Cursor Agent CLI installed and signed in

The provider never reads or displays an account email or credential value.

## Install

From the root of **this clone** (`./scripts/install.sh` does this for `web` and `headless`):

```bash
dsh plugin --profile web add ./plugins/dsh-llm-cursor-acp
dsh plugin --profile headless add ./plugins/dsh-llm-cursor-acp
```

A web-only install leaves headless with `NO_ADAPTER`. Do not switch MCP/gateway to `--profile web` to work around that.

Restart the existing `dsh web` process using the same command that originally launched it.

Then: `dsh plugin --profile web exec dsh-cursor-provider doctor`

## License

MIT. Third-party notices are in `THIRD_PARTY_NOTICES.md`.
