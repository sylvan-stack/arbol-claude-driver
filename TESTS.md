# Tests: arbol-claude-driver

Only the top-tier cases below are active. They may be selected when useful during implementation; running tests after every change is not required. Writing, restoring or running mid-tier tests requires a separate testing task explicitly requested by the owner. Do not write or restore low-tier tests.

[Mid-tier descriptions and justifications](MID_TIER.md) · [Complete inventory and tier justifications](docs/test-inventory.md) · [Machine-readable inventory](test-tiers.json)

Reviewed revision: `fc66624fca03b7f1d7cb395b0f1d445c2fc3030a` (28 October 2026), plus the preceding local pruning changes. No tests were run for this reset.

## Selection and limits

2 active Python definitions and 0 active standalone programs; 38 commented Python definitions. Also disabled: 0 named Node cases, 0 custom scenario functions and 1 standalone check programs. 5 broad entry points explicitly refuse. 2 low-tier cases remain removed from the prior review. These categories overlap in execution and must not be added into a runtime suite size.

Top-tier cases were individually read for the failure they observe. The remaining mid-tier labels are provisional execution decisions, not a claim that thousands of tests have passed a complete semantic review. Reassess value before restoring any of them. A browser program may contain many assertions; it is inventoried as one indivisible program, not as one asserted behavior.

## Active top tier

| Exact selector or program | Why this case earns a place |
| --- | --- |
| [`test_top_runtime.StateTests.test_recovery_never_deletes_or_overwrites_a_newer_run`](tests/top/test_top_runtime.py#L21) | Replaces a run while its stale recovery is awaiting cleanup, in both success and failure paths. The newer record and its evidence must survive; a simple sequential recovery test cannot establish this. |
| [`test_top_runtime.TransportTests.test_output_backpressure_uses_one_absolute_stop_deadline`](tests/top/test_top_runtime.py#L48) | Fills a real unread pipe, starts stopping, and requires the blocked write to end under one absolute deadline with cancellation signalled. Exposes shutdown hangs caused by blocked output. |

## Layout and shared support

Case bodies are separated into [`tests/top/`](tests/top/) and [`tests/mid/`](tests/mid/). The former mixed test modules retain shared fixture classes, helper functions and imports at their established paths, with no inventoried test methods. This preserves existing subprocess paths, patch targets and fixture imports without copying helpers into both tiers. Original check entry points retain their refusals.

## Running a selected top-tier case

Select an exact method when it resolves a concrete uncertainty; no run is required after an edit. The new module names begin with test_top_ so they cannot shadow the shared fixture modules. Do not run whole folders or discovery as a substitute. Original prerequisites, skips and environment limitations still apply. A focused result is not full qualification.

From the repository root, using the prepared repository interpreter:

```sh
PYTHONPATH=src:tests:tests/top python -m unittest test_top_runtime.StateTests.test_recovery_never_deletes_or_overwrites_a_newer_run
```

## Mid-tier review and restoration

[MID_TIER.md](MID_TIER.md) describes every retained mid-tier entry and its existing reason, identifies provisional classifications and explains restoration. Only a separately requested testing task may restore or run these sources. Keep the source in the mid folder; restore only the selected imports, class headers and bodies, update the inventories, and return the source to comments afterward unless the owner says otherwise. Broad entry points continue to refuse; repairing a qualification workflow requires its own explicit testing task.

Layout revision: `2bd4e7404271629cbd084b37e71cf874f1afaa4b`. No tests were run for this reorganization. Previous identifiers and source lines are retained in test-tiers.json. Indexed Origin relocation and ingest remain deferred; moving files supplies no new runtime proof.
