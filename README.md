# Arbol Claude Driver

The Arbol 2 Driver for Claude implements Anthropic Subscription management, inference, native conversation continuity, tools and questions, background work, cancellation, error and quota reports, and development checks. The public interface is one command-line process per operation. Package version `1.0.0`, contract `2`, data format `1`.

Contract 2 retires Claude z.ai routes. Explicit z.ai selectors, retired records and gateway settings fail before credential resolution or inference, with a remedy to migrate to the Pi Driver. Retired local records, histories and credentials remain available for explicit owner cleanup. Anthropic authentication and execution retain their existing behavior.

## Development

Use `uv` to install the exact CPython version in `.python-version` and the development tools in `uv.lock`. The runtime uses only the Python standard library.

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync python -m unittest discover -s tests -v
bin/arbol-claude-driver info
```

The source executable directly executes `.venv/bin/python` in isolated mode and reports Build `0`. The shell launcher, `bin/entry.py`, and `src/arbol_claude_driver` also form the packaged CLI; there is no public Python library interface. Neither invocation creates a virtual environment or resolves dependencies.

The 35 offline unit tests cover validated request and error boundaries, the recorded Node 24 history and JSON fingerprints, UTF-16 path encoding, Keychain identity, native history, durable state, concurrent ownership, recovery protection, asynchronous transports and bounded cleanup, sign-in replacement, quoted helpers under spaced interpreter paths, protocol questions/usage, and failed development-check spending. The shared contract suite adds all 38 CLI scenarios, using this executable's Python stand-in:

```sh
.venv/bin/python -I -B ../arbol/conformance/bin/arbol-conformance.py --driver ./bin/arbol-claude-driver
```

Shared conformance and `arbol-builds` are external tools from the `arbol` repository. `arbol-builds`, its launchers and the conformance suite are typed Python (SYL-165, SYL-166) and need no Node.js. They are not required to execute the Driver. Offline tests use temporary state and homes, synthetic credentials, and loopback listeners; they submit no live inference request.

## Commands and state

```sh
bin/arbol-claude-driver info --state-dir /tmp/claude-driver-state
bin/arbol-claude-driver subscriptions list
bin/arbol-claude-driver subscriptions create --family anthropic --label Work
bin/arbol-claude-driver subscriptions update anthropic-2 --label Personal
bin/arbol-claude-driver run --request /tmp/request.json --control
bin/arbol-claude-driver run --subscription test-anthropic --model haiku 'Answer with the single word: ready'
bin/arbol-claude-driver run --output text --subscription test-anthropic 'What is 2 + 2?'
bin/arbol-claude-driver runs list
bin/arbol-claude-driver runs stop run-id
```

Settings are read afresh from `~/.arbol/arbol-claude-driver.json`. State defaults to `~/Library/Application Support/Arbol/drivers/claude/`; `--state-dir` works before or after the command. Format-1 paths and records remain interoperable with previous TypeScript Builds, including account homes, Subscription locks, run owners, chat fingerprints and custom-state Keychain service names. Newer state formats are refused before mutation. `info` succeeds when Claude is missing or too old; commands needing it report `tool_missing` or `tool_unsupported`. The oldest supported Claude version is `2.1.289`.

`anthropic` and `zai` are listed from the start. Subscription commands support families, list, create, update, sign-in, sign-out, remove, check, usage, models and importing one explicitly named Z.AI settings file. Named accounts have private configuration homes. Anthropic uses Claude's login; `--by-code` accepts one JSON line containing `input`. Z.AI reads a fresh secret-source command or waits for its named Keychain item. Keys are excluded from command arguments, streamed output and diagnostics. Claude retains the actual macOS HOME for Keychain access while CLAUDE_CONFIG_DIR and its working directory point to the private account home. Signing out or removing an account refuses active runs unless `--force` stops them first.

A short run defaults to tools off. JSON requests specify history, input and tools; CLI fields replace matching JSON fields. `--request -` consumes one JSON line, with later lines reserved for `--control`. Caller labels appear on every event. JSON output includes deltas, whole blocks, tools, questions, usage and exactly one `finished` event; `--output text` writes whole main-answer blocks. Invalid controls emit `control_refused`. Questions accept offered labels, acknowledge duplicate answers harmlessly, and cancel the run on decline.

Matching history, image bytes and working directory resume the saved native conversation. Other histories rebuild native messages and tool results with an ordered reason. An unavailable native session retries once with labelled history. Maps contain identifiers and hashes rather than message text and settle only after successful completion. Adjacent assistant surrogate halves now use the canonical merged fingerprint required by DRV-6; a map carrying the previous defective fingerprint rebuilds safely as `history_differs`.

Stop controls, signals, control-input EOF and a broken output pipe initiate cancellation. Async tasks drain both vendor streams, enforce monotonic start/idle/total clocks, interrupt the vendor and force cleanup after grace. Output backpressure shares an absolute cleanup deadline. Verified process identities and shell ledgers protect against PID reuse and newer run records during recovery. A program that creates its own process group or session remains outside cleanup, as accepted in DRV-D33. Private skill links and MCP arguments expose the owner's user setup while account-synced skills remain private. Background activity suspends idle limits and stays out of the main answer. Error fields take precedence over wording, rejected credentials stop retries, and observed Anthropic limits update cached account usage.

## Numbered Builds

`arbol-build.json` packages the exact CPython `3.14.8` standalone distribution pinned by URL and SHA-256. A Build contains its executable, private interpreter and standard library, Python source, tests and recorded fixtures. It runs from an arbitrary folder using an ordinary PATH, without a checkout, system Python, uv, Node, npm, dependency download or virtual environment. Claude itself remains an external vendor tool. `package-manifest.json` records the runtime archive identity and file integrity.

```sh
arbol-builds build arbol-claude-driver
arbol-builds list arbol-claude-driver
arbol-claude-driver info
arbol-claude-driver -v 1 info
```

The installed launcher selects the newest working Build. Numbered records and logs live under `~/Library/Application Support/Arbol/builds/arbol-claude-driver/`. Every production Build runs its own packaged unit suite, all 38 shared contract scenarios, then exactly one tools-off Haiku message through `dev build-check`. `ARBOL_CONFORMANCE_REPO` selects another shared-suite checkout; The check finds the signed-in `test-anthropic` in its own test state directory, `~/Library/Application Support/Arbol/drivers/arbol-claude-driver/`, not in the default one; `ARBOL_BUILD_TEST_STATE_DIR` names another (DRV-D41). Missing login, exhausted usage or failed checks preserve the numbered record and previous working default. Account state and credentials are never packaged.

`scripts/package_build.py --candidate /absolute/empty/folder` creates an offline artifact for review. `scripts/verify_build.py --candidate <folder> --arbol-repo <arbol-repo> --output <evidence.json>` rehearses relocation, default/named launchers, failed-check retention and withdrawal with synthetic registry checks and zero vendor prompts. These checks supplement the production descriptor's actual gates.

## Development checks and evidence

The executable retains `dev stand-in <scenario-folder> -- <vendor-arguments>`, `dev probe`, `dev build-check` and `dev live-check`.

- `dev probe` sends no user record. It checks version, status/catalog, browser handoff without opening a browser, settings, native history, interrupt, skills/MCP and argument refusals. Evidence records shapes and omits account values.
- `dev build-check` requires signed-in `test-anthropic` and available quota, then sends exactly one Haiku message asking for `ready`.
- `dev live-check` is an on-demand six-message Haiku sequence covering output/usage, tools/questions, background attribution, conversation continuation and stopping. It checks cached usage before every message and reports attempted turns, started inference requests, token/cost totals and before/after limits, including failed checks.

These commands save dated evidence in the chosen state's `research/` folder. Historical protocol/probe/live JSON and text records stay under `test/`; the migrated stand-in uses their original envelopes with explicit synthetic scenario variants. `tests/fixtures/migration-vectors.json` retains both historical driver outputs and canonical fingerprints, including the known Claude surrogate defect; `migration-json-vectors.json` independently records Node 24 serialization behavior. The original TypeScript implementation remains available in Git history.

Historical Build `1` passed 81 TypeScript tests, 38 shared scenarios and one Haiku message with Claude `2.1.289` on 7 October 2026. Build `2` deliberately failed before inference and retained its record while leaving Build `1` as default. Those original results remain in `test/build-verification-2026-10-07.json` and `test/numbered-build-{1,2}-2026-10-07.txt`.

| Repository | Version | Artifact corpus |
|---|---|---|
| `arbol` | 2.0.0 | `~/Artifacts/arbol` |
| `arbol-claude-driver` | 1.0.0 | `~/Artifacts/arbol-claude-driver` |
| `arbol-codex-driver` | 1.0.0 | `~/Artifacts/arbol-codex-driver` |
| `arbol-pi-driver` | 1.0.0 | `~/Artifacts/arbol-pi-driver` |

## Sylvan Stack 2

| Repository | Package | Version | Artifact corpus | Artifact repository |
|---|---|---|---|---|
| `workspace-ledger` | `workspace-ledger` (planned) | 1.0.0 | `~/Artifacts/sylvan-stack/repos/workspace-ledger` | `sylvan-stack/workspace-ledger-artifacts` |
