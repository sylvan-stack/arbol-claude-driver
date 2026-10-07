# Arbol Claude Driver

Arbol Claude Driver: the Arbol 2 Driver for Claude.

The base command-line program (SYL-75), Subscription commands (SYL-78), fresh inference runs (SYL-80), conversation continuity (SYL-82), stopping/cleanup (SYL-83), and tools/questions/background work (SYL-84) are implemented. `info` reads settings, reports the running package and Build, and discovers the installed Claude tool. Subscriptions support families, list, create, update, sign-in, sign-out, removal, check, usage, models and a one-time z.ai import. `run` accepts request JSON or short options, checks the tool's catalog and applied effort before inference, streams whole blocks and deltas, reports token totals and ends with one final event. `runs list` shows active runs and recovers orphaned ones; `runs stop <runId>` stops another invocation and succeeds for an ended or unknown run.

```sh
bin/arbol-claude-driver.mjs run --subscription test-anthropic --model haiku 'Answer with the single word: ready'
bin/arbol-claude-driver.mjs run --request /tmp/request.json --control
bin/arbol-claude-driver.mjs run --output text --subscription test-anthropic 'What is 2 + 2?'
```

Without a request file tools default to off. Without a working folder, the tool runs in a private temporary folder with session persistence off, and the folder is removed before the final event. A run with tools needs an absolute working folder. Command-line fields replace the corresponding JSON fields. `--request -` reads one JSON line from standard input; `--control` keeps the remaining lines for controls. Caller labels are echoed on every event; a malformed or unsupported control line emits `control_refused` while the run continues. `--output text` writes only whole answer blocks.

A request with history resumes its saved Claude conversation when the chat map matches the messages, images and working folder and no other run reserves it. Otherwise it builds a native session from the history, including tool calls and their results, and reports the rebuild reason in `started`. A changed system prompt resumes with `--system-prompt-snapshot off`. A refused resume retries once with labelled history and a notice; other failures never retry. The map contains identifiers and digests only and is written only after success. A stop line, SIGINT, SIGTERM, control-input EOF or a broken event pipe cancels a run. The Driver asks Claude to interrupt, forces it down after two seconds if needed, closes questions and calls, and preserves partial output. A private shell wrapper records each shell process group; those groups are cleaned before the final event. Later calls recover a killed Driver's tool, shell groups, scratch folder and continued chat entry. Process identities include start times so a reused PID is left alone. A program that creates its own process group or session remains outside cleanup, as accepted in DRV-D33. Questions are answered through `answer` control lines; invalid selections are refused, duplicate answers have no effect, and a decline cancels the run. Background agents and commands keep the input open until all work has finished; their text stays out of the main answer. Each private skills directory links the owner's user skills individually, while account-synced skills stay private. Runs with tools also receive the owner's MCP file as a tool argument. The complete error mapping, retry handling, requested time limits and Subscription limit reports are SYL-85. Requested time limits are explicitly refused for now. These later scopes are named in the shared suite's reports.

The run stand-in derives its envelopes from the recorded Claude 2.1.289 run in `test/run-protocol-2026-10-06.json`, with explicitly synthetic text, counts and failure variants. C14 checks continuity; C15–C17, C33 and C38 check stopping and recovery, C9, C25 and C35–C37 add owner setup, permission requests, answered questions and background completion, bringing the shared suite to 33 scenarios. `test/probe-continuity.mjs` verifies a missing-session refusal and synthetic-session acceptance without inference; `test/live-continuity.mjs` uses four short Haiku turns on `test-anthropic` to verify native recall, rebuilt tool-result recall and changed system prompts. Probe evidence is in `test/continuity-probe-2026-10-07.json`; the live script writes `test/continuity-live-2026-10-07.json` with the result of its checks, including a failed check. The live check passed on 7 October 2026 after renewing the test-account login: native recall, rebuilt tool-result recall and the changed system prompt all passed; the chat map contained no message text and the owner’s configuration was unchanged. `test/probe-run.mjs` spends no inference quota and verifies the installed tool's settings handshake; `test/live-run.mjs` uses `test-anthropic` and Haiku for two short messages, one with tools and one without. Both verify that the owner's configuration is unchanged; the live check verifies usage and temporary-folder cleanup. Run them on demand, outside `npm test`.

`test/probe-stopping.mjs` verifies the installed tool's interrupt acknowledgement without sending a prompt. `test/live-stopping.mjs` runs four short checks on `test-anthropic` with Haiku: partial-answer cancellation, cleanup of a command left with `&`, cancellation while a sub-agent works, and recovery after killing the Driver. All passed on 7 October 2026 with Claude 2.1.289; the owner's configuration was unchanged. Their records are `test/stopping-probe-2026-10-07.json` and `test/stopping-live-2026-10-07.json`. Run these checks on demand, outside `npm test`.

`test/probe-tools.mjs` checks trusted mode and the owner’s user skills and MCP servers without inference. `test/live-tools.mjs` uses two short Haiku messages on `test-anthropic`: an answered question followed by a file write and shell call, then an attributed sub-agent and completed background command. These checks passed on 7 October 2026. The probe verified the entire owner setup was unchanged. Both live behavior checks passed; its final owner snapshot was inconclusive because a separate Claude session was writing its own `Artifacts/mycel` session log. The original snapshot failure and the evaluated behavior record are preserved in `test/tools-live-2026-10-07-snapshot-failure.json` and `test/tools-live-2026-10-07.json`. Run these scripts on demand, outside `npm test`.

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
bin/arbol-claude-driver.mjs subscriptions list
bin/arbol-claude-driver.mjs subscriptions create --family anthropic --label Work
bin/arbol-claude-driver.mjs subscriptions update anthropic-2 --label Personal
bin/arbol-claude-driver.mjs dev stand-in /tmp/scenario-folder -- --version
```

The stand-in reads `scenario` in that folder. C31/C32 cover version probes; C2, C4–C8, C39–C41 cover subscription protocol scripts, with `success`, `by-code`, `waiting`, `rejected` and `unavailable` parts. Its status and initialization records derive from `test/subscriptions-probe-2026-10-06.json`; successful login and failure variants are explicitly synthetic. It records arguments, environment variable names and checks in `received.jsonl`, without credential values. The shared suite passes 33 scenarios; its reports name the portions deferred to later tasks. Tests use temporary homes and loopback vendor listeners. Keychain checks use synthetic values and services derived from their temporary state paths.

Anthropic sign-in uses Claude's own login and its own Keychain credential. `--by-code` emits `enter_code` and accepts one JSON line, `{ "input": "one-time code" }`. z.ai sign-in reads a key from its configured source, or emits `store_secret` naming the Keychain item and waits for the caller to fill it. No key is accepted as an argument or on the Driver's input. Keychain access needs the actual macOS home, so Claude children retain that HOME while CLAUDE_CONFIG_DIR and their working directory point at the private Subscription home. Real probes verify the owner's configuration is unchanged.

Sign-out and removal refuse a live run record; `--force` stops its owner first. Sign-in replacement and cancellation remove their child and progress files. An import reads only its explicitly named file, preserves defaults, imports allowed gateway settings and stores its key through `security` standard input. The owner's real z.ai file is not part of automated tests.

`anthropic` and `zai` are listed from the start. Creating another account takes `--family` and `--label`, with an optional stable `--id`; otherwise its name is `anthropic-2`, `zai-2`, and so on. Each new account has a private tool home and a JSON record, and competing commands reserve distinct identifiers. Updates change only the label given and preserve account details and cached usage. Listing reads settings afresh and contacts no vendor. Unsigned model requests and checks return `not_signed_in`, and unsigned usage returns `needs_sign_in`, before Claude is started.

The separate `test/probe-info.mjs` script checks the installed tool without sending a prompt. It takes the tool's absolute path and an output-record path, creates a temporary home, runs `info` with a clean environment, and removes the home. Its recorded check is `probe.base-info`; evidence lives in `~/Artifacts/arbol-claude-driver/research/base-info-2026-10-06.json`.

`test/probe-subscriptions.mjs` checks fresh status, catalog initialization and the BROWSER handoff, and records the existing test account. `test/live-subscriptions.mjs` confirms the pre-existing test login and checks it through the Driver. Both send no inference request, preserve the owner's configuration and are run on demand, outside `npm test`. Anthropic usage is `unsupported` until a later run supplies limit information; z.ai usage is fetched only on request.

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
