# Repository instructions

## Test execution policy

Read [TESTS.md](TESTS.md) and [MID_TIER.md](MID_TIER.md) before changing or running tests. Keep case bodies in the documented top/ or mid/ folder; shared helpers may retain their existing paths. Only the explicitly listed top-tier cases are active. Agents may select one when it resolves a concrete uncertainty; no test run is required after an edit.

Mid-tier tests and broad checks are physically commented out. Restore, write or run them only in a separate testing task explicitly requested by the owner. A request to implement, review or refactor is not such authorization. Low-tier tests should not be added or restored. Update the inventory when a classification changes. Do not bypass a disabled qualification entry point or call a top-only run full qualification.

Static inspection is permitted. Inspect commands and hooks for indirect test execution. This policy supersedes older repository instructions to run suites automatically.
