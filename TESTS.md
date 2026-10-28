# Tests: arbol-claude-driver

Only the top-tier cases below are active. They may be selected when useful during implementation; running tests after every change is not required. Writing, restoring or running mid-tier tests requires a separate testing task explicitly requested by the owner. Do not write or restore low-tier tests.

[Complete inventory and tier justifications](docs/test-inventory.md) · [Machine-readable inventory](test-tiers.json)

Reviewed revision: `fc66624fca03b7f1d7cb395b0f1d445c2fc3030a` (28 October 2026), plus the preceding local pruning changes. No tests were run for this reset.

## Selection and limits

2 active Python definitions and 0 active standalone programs; 38 commented Python definitions. Also disabled: 0 named Node cases, 0 custom scenario functions and 1 standalone check programs. 5 broad entry points explicitly refuse. 2 low-tier cases remain removed from the prior review. These categories overlap in execution and must not be added into a runtime suite size.

Top-tier cases were individually read for the failure they observe. The remaining mid-tier labels are provisional execution decisions, not a claim that thousands of tests have passed a complete semantic review. Reassess value before restoring any of them. A browser program may contain many assertions; it is inventoried as one indivisible program, not as one asserted behavior.

## Active top tier

| Exact test selector | Why this case earns a place |
| --- | --- |
| [`test_runtime.StateTests.test_recovery_never_deletes_or_overwrites_a_newer_run`](tests/test_runtime.py#L275) | Replaces a run while its stale recovery is awaiting cleanup, in both success and failure paths. The newer record and its evidence must survive; a simple sequential recovery test cannot establish this. |
| [`test_runtime.TransportTests.test_output_backpressure_uses_one_absolute_stop_deadline`](tests/test_runtime.py#L426) | Fills a real unread pipe, starts stopping, and requires the blocked write to end under one absolute deadline with cancellation signalled. Exposes shutdown hangs caused by blocked output. |

## Running a selected top-tier case

Select the exact method, never a whole module as a substitute. Retain the original fixture prerequisites. A missing dependency, skipped case or unavailable database is not a pass. No live-provider check was promoted.

Use the repository interpreter and source/test import paths; for example, from the repository root:

```sh
PYTHONPATH=src:tests python -m unittest test_runtime.StateTests.test_recovery_never_deletes_or_overwrites_a_newer_run
```

## Restoring a mid-tier selection in an explicit testing task

1. Read the case and its helpers, reassess its information value, and identify the exact requested scope. Delete low-value cases instead of restoring them.
2. Uncomment only the selected `TEST-TIER-DISABLED-BEGIN` / `END` block by removing one leading `# ` (Python) or `// ` (JavaScript) from each original source line. Remove the marker lines. A bare `#` or `//` line inside a block represents an empty original line. Restore needed names from the complete `TEST-TIER-IMPORTS` statements at the file tail into their original scope and normal import section, rather than uncommenting them at the tail. Do not discard other local changes.
3. For standalone programs or custom scenario functions, remove only their adjacent `TEST-TIER-REFUSAL` stub or terminal refusal after restoring the actual implementation. Restore required shared helpers. The empty-suite `pass` is harmless and can be removed once a class has a real body.
4. Update both inventories to reflect the selection. Run only the requested scope, record actual results, then return mid-tier code to comments when the task finishes unless the owner asked to retain it active. Promotion to top tier needs a concrete, individually reviewed reason.

Broad build/qualification checks refuse while their implementation is commented. Preserve that refusal until the requested qualification has its necessary tests restored. A top-only run does not satisfy an old full-suite gate. Metadata verification, production validation, fixture helpers, Storybook examples and historical result records are not test passes and remain separate.
