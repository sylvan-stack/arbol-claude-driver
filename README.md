# Arbol Claude Driver

Arbol Claude Driver: the Arbol 2 Driver for Claude.

The base command-line program (SYL-75), Subscription commands (SYL-78), fresh inference runs (SYL-80), conversation continuity (SYL-82), stopping/cleanup (SYL-83), tools/questions/background work (SYL-84), errors/time limits/usage reports (SYL-85), and development checks (SYL-87) are implemented. `info` reads settings, reports the running package and Build, and discovers the installed Claude tool. Subscriptions support families, list, create, update, sign-in, sign-out, removal, check, usage, models and a one-time z.ai import. `run` accepts request JSON or short options, checks the tool's catalog and applied effort before inference, streams whole blocks and deltas, reports token totals and ends with one final event. `runs list` shows active runs and recovers orphaned ones; `runs stop <runId>` stops another invocation and succeeds for an ended or unknown run.

```sh
bin/arbol-claude-driver.mjs run --subscription test-anthropic --model haiku 'Answer with the single word: ready'
bin/arbol-claude-driver.mjs run --request /tmp/request.json --control
bin/arbol-claude-driver.mjs run --output text --subscription test-anthropic 'What is 2 + 2?'
```

Without a request file tools default to off. Without a working folder, the tool runs in a private temporary folder with session persistence off, and the folder is removed before the final event. A run with tools needs an absolute working folder. Command-line fields replace the corresponding JSON fields. `--request -` reads one JSON line from standard input; `--control` keeps the remaining lines for controls. Caller labels are echoed on every event; a malformed or unsupported control line emits `control_refused` while the run continues. `--output text` writes only whole answer blocks.

A request with history resumes its saved Claude conversation when the chat map matches the messages, images and working folder and no other run reserves it. Otherwise it builds a native session from the history, including tool calls and their results, and reports the rebuild reason in `started`. A changed system prompt resumes with `--system-prompt-snapshot off`. A refused resume retries once with labelled history and a notice; other failures never retry. The map contains identifiers and digests only and is written only after success. A stop line, SIGINT, SIGTERM, control-input EOF or a broken event pipe cancels a run. The Driver asks Claude to interrupt, forces it down after two seconds if needed, closes questions and calls, and preserves partial output. A private shell wrapper records each shell process group; those groups are cleaned before the final event. Later calls recover a killed Driver's tool, shell groups, scratch folder and continued chat entry. Process identities include start times so a reused PID is left alone. A program that creates its own process group or session remains outside cleanup, as accepted in DRV-D33. Questions are answered through `answer` control lines; invalid selections are refused, duplicate answers have no effect, and a decline cancels the run. Background agents and commands keep the input open until all work has finished; their text stays out of the main answer. Each private skills directory links the owner's user skills individually, while account-synced skills stay private. Runs with tools also receive the owner's MCP file as a tool argument. Run failures use the tool's error fields before any wording fallback, and retry notices report the tool's attempts. A rejected credential stops immediately and marks the Subscription `needs_attention`. The tool may retry three times. Runs have a 60-second start limit and a 15-minute idle limit; `limits.idleSeconds` overrides idle (zero disables it), and `limits.totalSeconds` sets an optional total. Tool calls, questions and background work suspend idle; total keeps counting. The latest Anthropic limit event becomes the Subscription's cached usage report after the run.

The run stand-in derives its envelopes from the recorded Claude 2.1.289 run in `test/run-protocol-2026-10-06.json`, with explicitly synthetic text, counts and failure variants. C14 checks continuity; C15–C17, C33 and C38 check stopping and recovery, C9, C25 and C35–C37 add owner setup, permission requests, answered questions and background completion, C26–C30 add error mapping, waits, retry cancellation, clocks and saved limits, bringing the shared suite to 38 scenarios. `test/probe-continuity.mjs` verifies a missing-session refusal and synthetic-session acceptance without inference; `test/live-continuity.mjs` uses four short Haiku turns on `test-anthropic` to verify native recall, rebuilt tool-result recall and changed system prompts. Probe evidence is in `test/continuity-probe-2026-10-07.json`; the live script writes `test/continuity-live-2026-10-07.json` with the result of its checks, including a failed check. The live check passed on 7 October 2026 after renewing the test-account login: native recall, rebuilt tool-result recall and the changed system prompt all passed; the chat map contained no message text and the owner’s configuration was unchanged. `test/probe-run.mjs` spends no inference quota and verifies the installed tool's settings handshake; `test/live-run.mjs` uses `test-anthropic` and Haiku for two short messages, one with tools and one without. Both verify that the owner's configuration is unchanged; the live check verifies usage and temporary-folder cleanup. Run them on demand, outside `npm test`.

`test/probe-stopping.mjs` verifies the installed tool's interrupt acknowledgement without sending a prompt. `test/live-stopping.mjs` runs four short checks on `test-anthropic` with Haiku: partial-answer cancellation, cleanup of a command left with `&`, cancellation while a sub-agent works, and recovery after killing the Driver. All passed on 7 October 2026 with Claude 2.1.289; the owner's configuration was unchanged. Their records are `test/stopping-probe-2026-10-07.json` and `test/stopping-live-2026-10-07.json`. Run these checks on demand, outside `npm test`.

`test/probe-tools.mjs` checks trusted mode and the owner’s user skills and MCP servers without inference. `test/live-tools.mjs` uses two short Haiku messages on `test-anthropic`: an answered question followed by a file write and shell call, then an attributed sub-agent and completed background command. These checks passed on 7 October 2026. The probe verified the entire owner setup was unchanged. Both live behavior checks passed; its final owner snapshot was inconclusive because a separate Claude session was writing its own `Artifacts/mycel` session log. The original snapshot failure and the evaluated behavior record are preserved in `test/tools-live-2026-10-07-snapshot-failure.json` and `test/tools-live-2026-10-07.json`. Run these scripts on demand, outside `npm test`.

`test/probe-errors.mjs` checks a fresh-home rejection, exactly three retries against an unreachable vendor, and stopping at the first rejected-key retry from a loopback endpoint, without inference quota. `test/live-errors.mjs` sends one short Haiku message on `test-anthropic` and checks that both limit windows were saved and are returned by `subscriptions usage`. Both passed on 7 October 2026 with Claude 2.1.289 and unchanged owner configuration. Their records are `test/errors-probe-2026-10-07.json` and `test/errors-live-2026-10-07.json`. Error and retry envelopes come from the probe record; limit envelopes come from the recorded 6 October run, with explicitly synthetic scenario variants. Real reached-limit, overload and context-exceeded failures remain unobserved. Run these scripts on demand, outside `npm test`.

## Development

Use Node.js 24 or later (`.nvmrc` names the version checked here).

```sh
nvm use
npm ci
npm test
npm start -- info
```

After `npm run build`, `bin/arbol-claude-driver.mjs` is the development executable and reports Build `0`. It is also the package's `arbol-claude-driver` bin entry. There is no public library interface and no runtime dependency.

## Numbered Builds

The repository's `arbol-build.json` builds with `arbol-builds`, installed from the `arbol` repository. Each Build contains its executable, compiled runtime and unit tests, and the recorded protocol fixtures. It needs Node.js 24 and Claude, with no source checkout or npm dependencies to run. Its package version remains `1.0.0`; its separate hexadecimal build number comes from its own `build.json`.

```sh
arbol-builds build arbol-claude-driver
arbol-builds list arbol-claude-driver
arbol-claude-driver info
arbol-claude-driver -v 1 info
```

The launcher in `~/.local/bin` selects the newest working Build. Builds and logs stay under `~/Library/Application Support/Arbol/builds/arbol-claude-driver/`. A failed check preserves its numbered record and log and leaves the previous working Build as the default.

Every build runs the unit tests from the new Build, all 38 shared conformance scenarios against that Build's executable, then exactly one tools-off Haiku message through `dev build-check` on `test-anthropic`. The unit and contract checks use stand-ins and spend no vendor quota. The shared suite comes from `~/repos/sylvan-stack/arbol`; `ARBOL_CONFORMANCE_REPO` selects another checkout. The live check uses a clean environment and the Driver's default state directory, or `ARBOL_BUILD_TEST_STATE_DIR` when the signed-in test account is kept elsewhere. A missing login, exhausted usage, or any failed check prevents a working Build. No test account or credential is copied into a Build.

The on-demand `node test/verify-build.mjs` checks a fresh local clone, the default and named launchers, and a deliberately failed check that stops before inference. It spends one real Haiku message and retains both numbered Build records. Select an existing signed-in test state with `ARBOL_BUILD_TEST_STATE_DIR` before running it. This script is separate from `npm test`.

On 7 October 2026, Build `1` from a clean checkout passed all 81 unit tests, all 38 conformance scenarios and one real Haiku message with Claude 2.1.289. Both PATH launcher forms reported Build `1`, package `1.0.0`. Build `2` deliberately failed its first check, retained its record/log and left Build `1` as the default. The owner's configuration was unchanged. Evidence is in `test/build-verification-2026-10-07.json` and `test/numbered-build-{1,2}-2026-10-07.txt`.

Settings are read afresh from `~/.arbol/arbol-claude-driver.json`. `--state-dir <path>` works before or after the command; otherwise state is kept in `~/Library/Application Support/Arbol/drivers/claude/`. `info` succeeds when Claude is missing or too old and reports `supported: false`. Commands needing the tool receive `tool_missing` or `tool_unsupported` instead.

```sh
bin/arbol-claude-driver.mjs info --state-dir /tmp/claude-driver-state
bin/arbol-claude-driver.mjs subscriptions list
bin/arbol-claude-driver.mjs subscriptions create --family anthropic --label Work
bin/arbol-claude-driver.mjs subscriptions update anthropic-2 --label Personal
bin/arbol-claude-driver.mjs dev stand-in /tmp/scenario-folder -- --version
```

The stand-in reads `scenario` in that folder. C31/C32 cover version probes; C2, C4–C8, C39–C41 cover subscription protocol scripts, with `success`, `by-code`, `waiting`, `rejected` and `unavailable` parts. Its status and initialization records derive from `test/subscriptions-probe-2026-10-06.json`; successful login and failure variants are explicitly synthetic. It records arguments, environment variable names and checks in `received.jsonl`, without credential values. The shared suite passes all 38 scenarios, including full command-line coverage and credential redaction in streamed output, tools, errors and text output. Tests use temporary homes and loopback vendor listeners. Keychain checks use synthetic values and services derived from their temporary state paths.

Anthropic sign-in uses Claude's own login and its own Keychain credential. `--by-code` emits `enter_code` and accepts one JSON line, `{ "input": "one-time code" }`. z.ai sign-in reads a key from its configured source, or emits `store_secret` naming the Keychain item and waits for the caller to fill it. No key is accepted as an argument or on the Driver's input. Keychain access needs the actual macOS home, so Claude children retain that HOME while CLAUDE_CONFIG_DIR and their working directory point at the private Subscription home. Real probes verify the owner's configuration is unchanged.

Sign-out and removal refuse a live run record; `--force` stops its owner first. Sign-in replacement and cancellation remove their child and progress files. An import reads only its explicitly named file, preserves defaults, imports allowed gateway settings and stores its key through `security` standard input. The owner's real z.ai file is not part of automated tests.

`anthropic` and `zai` are listed from the start. Creating another account takes `--family` and `--label`, with an optional stable `--id`; otherwise its name is `anthropic-2`, `zai-2`, and so on. Each new account has a private tool home and a JSON record, and competing commands reserve distinct identifiers. Updates change only the label given and preserve account details and cached usage. Listing reads settings afresh and contacts no vendor. Unsigned model requests and checks return `not_signed_in`, and unsigned usage returns `needs_sign_in`, before Claude is started.

The executable carries three commands for checking an installed Driver, alongside `dev stand-in`:

- `dev probe` sends no user record. It checks the installed tool's version, fresh and optional test-account status/catalog, browser handoff without opening a browser, applied settings, native session acceptance, interrupt, user skills/MCP loading and argument refusals. Records contain shapes, the actual tool version and date, with account values omitted.
- `dev build-check` sends exactly one Haiku message on `test-anthropic`, with tools off: “Answer with the single word: ready”. It requires a completed run whose answer contains that word.
- `dev live-check` runs six small Haiku messages: plain output/usage, tools and answered questions, background attribution, conversation continuation (two messages), and stopping. Run it only when requested. Both live commands require the test Subscription to be signed in and refuse before launching the tool when a usage window is full. They check usage before every message and report attempted turns, started inference runs, tokens, usage before/after and percentage changes, including failures. Anthropic usage is the cached report from its last run, with its timestamp; there is no additional vendor usage API.

Every command writes JSON and saves dated evidence in `research/` under its selected state directory. An existing test account can be selected with `--state-dir`; it must belong to this Driver and have been created and signed in by the owner. The source test harness is not needed by the compiled development commands. Stand-ins retain their recorded protocol JSON fixtures under `test/` when packaged. Historical probe/live scripts remain available as evidence of the earlier tasks; some historical failure scripts submitted messages against an unreachable/local endpoint and are not part of the no-prompt command.

SYL-87 verification: all 38 shared scenarios and 81 Driver tests pass. Installed Claude 2.1.289 passed `dev probe` with zero inference requests and `dev build-check` with one Haiku message. The six-turn `dev live-check` orchestration passed against the stand-in; its real tool/question/background/continuity/stop behaviors retain the live evidence from earlier tasks. A fresh six-turn vendor run was not requested.

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
