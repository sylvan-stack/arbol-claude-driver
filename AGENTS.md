# Repository instructions

## Test execution policy

Read [TESTS.md](TESTS.md) and [MID_TIER.md](MID_TIER.md) before changing or running tests. Keep case bodies in the documented top/ or mid/ folder; shared helpers may retain their existing paths. Both tiers have uncommented source; only the explicitly listed top-tier cases may be selected during implementation. Agents may select one when it resolves a concrete uncertainty; no test run is required after an edit.

Mid-tier tests and broad checks remain uncommented at the owner's request. Write or run them only in a separate testing task explicitly requested by the owner. A request to implement, review or refactor is not such authorization. Low-tier tests should not be added or restored. Update the inventory when a classification changes. Do not call a top-only run full qualification or bypass a guard for missing test sources. Do not re-comment mid-tier sources after a run.

Static inspection is permitted. Inspect commands and hooks for indirect test execution. This policy supersedes older repository instructions to run suites automatically.

Mid-tier retention follows the raised bar in MID_TIER.md: consequential risk, execution-dependent information, distinct coverage, an independent observation and proportionate cost are all required. Ordinary functional coverage alone does not qualify.
