# Arbol Claude Driver

Arbol Claude Driver: the Arbol 2 Driver for Claude.

The base command-line program is implemented (SYL-75). `info` reads settings, reports the running package and Build, and discovers the installed Claude tool. Subscription operations and runs are the next implementation tasks; their recognised command lines currently return `unsupported`.

## Development

Use Node.js 24 or later (`.nvmrc` names the version checked here).

```sh
nvm use
npm ci
npm test
npm start -- info
```

After `npm run build`, `bin/arbol-claude-driver.mjs` is the executable. It is also the package's `arbol-claude-driver` bin entry. There is no public library interface and no runtime dependency. Numbered Builds and the search-path launcher are implemented by the separate `arbol-builds` task.

Settings are read afresh from `~/.arbol/arbol-claude-driver.json`. `--state-dir <path>` works before or after the command; otherwise state is kept in `~/Library/Application Support/Arbol/drivers/claude/`. `info` succeeds when Claude is missing or too old and reports `supported: false`. Commands needing the tool receive `tool_missing` or `tool_unsupported` instead.

```sh
bin/arbol-claude-driver.mjs info --state-dir /tmp/claude-driver-state
bin/arbol-claude-driver.mjs dev stand-in /tmp/scenario-folder -- --version
```

The stand-in reads `scenario` in that folder: `C31 supported`, `C31 old`, or `C31 missing`. It appends arguments, environment variable names and the real working directory to `received.jsonl`. It currently answers only the version probe. The unit tests use temporary homes and this stand-in, without network or account access.

The separate `test/probe-info.mjs` script checks the installed tool without sending a prompt. It takes the tool's absolute path and an output-record path, creates a temporary home, runs `info` with a clean environment, and removes the home. Its recorded check is `probe.base-info`; evidence lives in `~/Artifacts/arbol-claude-driver/research/base-info-2026-10-06.json`.

| | |
|---|---|
| Package | `@sylvan-stack/arbol-claude-driver` 1.0.0 |
| Artifact corpus | `~/Artifacts/arbol-claude-driver` (GitHub `sylvan-stack/arbol-claude-driver-artifacts`) |

## Sylvan Stack 2

| Repository | Package | Version | Artifact corpus | Artifact repository |
|---|---|---|---|---|
| `arbol` | `@sylvan-stack/arbol` | 2.0.0 | `~/Artifacts/arbol` | `sylvan-stack/arbol-artifacts` |
| `arbol-root` | `@sylvan-stack/arbol-root` | 1.0.0 | `~/Artifacts/arbol-root` | `sylvan-stack/arbol-root-artifacts` |
| `arbol-trunk` | `@sylvan-stack/arbol-trunk` | 1.0.0 | `~/Artifacts/arbol-trunk` | `sylvan-stack/arbol-trunk-artifacts` |
| `arbol-claude-driver` | `@sylvan-stack/arbol-claude-driver` | 1.0.0 | `~/Artifacts/arbol-claude-driver` | `sylvan-stack/arbol-claude-driver-artifacts` |
| `arbol-codex-driver` | `@sylvan-stack/arbol-codex-driver` | 1.0.0 | `~/Artifacts/arbol-codex-driver` | `sylvan-stack/arbol-codex-driver-artifacts` |
| `mycel` | `@sylvan-stack/mycel` | 2.0.0 | `~/Artifacts/mycel` | `sylvan-stack/mycel-artifacts` |
