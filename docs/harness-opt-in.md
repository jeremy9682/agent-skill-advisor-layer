# Official DeepSeek Harness (opt-in)

This clone does **not** vendor [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness). Official DSH is a third-party runtime, like Node.js: install it yourself if you want the `dsh` CLI.

```bash
npm i -g @deepseek-ai/dsh
```

Then run `./scripts/install.sh` so **this clone's** plugins land in both the `web` and `headless` profiles.

## Command Code subagent (optional)

DSH can spawn Command Code as a subagent via the official package
`@deepseek-ai/dsh-subagent-command-code`. We do not ship that package or a
harness fork.

If you already use Command Code (`cmd` on `PATH`, signed in with the product's
own login), enable the provider in a **copy** of an official agent preset by
removing `disabled: true` from the `tool-subagent-command-code` row. Full
presets ship that row disabled on purpose. Do not paste API keys into task
text; Command Code uses its own local auth.
