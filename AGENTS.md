# Repository instructions

## Test execution policy

Follow [TESTS.md](TESTS.md), [MID_TIER.md](MID_TIER.md), the linked inventory and
the owner's [Test Strategy](/Users/msix/Artifacts/mycel/operations/test-strategy.md).
**All test work belongs to a separate task explicitly requested by the owner.**
There is no top-tier exception during implementation. Write and polish code
first; only the owner decides when it is ready for tests and requests that work.
Do not add, edit, fix, refactor, classify, retire, restore or run tests, fixtures,
snapshots, runners or inventories as part of implementation. Existing tests may
be read as source reference. Report relevant test impact; do not start testing,
create a testing task yourself or relabel an implementation step as a testing task.

Keep runnable suites small. A new function, branch or option does not need a
matching test automatically. Each retained case must add distinct consequential
information. Routine permutations, copied defaults, mocked success/forwarding
and redundant CRUD are low tier; use the strategy's examples and admission bar.
Before adding a test in an authorized task, read the nearby retained cases and
low-tier comments so the rejected idea is not recreated.

Keep existing rejected cases as literal comments at the end of their test files,
marked `LOW TIER — DO NOT ENABLE OR RECREATE`, with original ID, reason, retained
evidence when applicable and source revision. An entirely low-tier file is
comment-only. Record low/commented status and footer location in the inventory.
Never use skipped tests, disabled suites or executable wrappers as archives.
New evidence and the owner's testing request are required for re-admission.

Keep retained top/mid source in the repository's documented layout and state.
A separately requested testing/Build task runs the whole top tier through the
Build gate; no test-running Build or hook runs during implementation. Mid-tier
execution additionally requires the owner's request for that scope. Do not
bypass a gate or call a reduced selection full qualification. Test-free static
syntax, type and compilation checks remain available. Report checks not run.
This policy supersedes older automatic testing and top-tier exceptions.
