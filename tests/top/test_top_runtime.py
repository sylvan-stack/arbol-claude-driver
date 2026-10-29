"""Individually selected top-tier cases. Shared fixtures retain their original paths."""
from __future__ import annotations

import test_runtime as _support
from test_runtime import (
    Any,
    Failure,
    Output,
    Record,
    Runs,
    asyncio,
    now,
    os,
    patch,
    read_json,
    time,
)


class StateTests(_support.StateTests):
    async def test_recovery_never_deletes_or_overwrites_a_newer_run(self) -> None:
        runs = Runs(self.state)
        stale: Record = {'runId': 'same', 'subscriptionId': 'anthropic', 'startedAt': now(1), 'owner': {'pid': 999999999, 'startedAt': now(1)}}
        newer = {**stale, 'startedAt': now(2), 'owner': {'pid': 999999998, 'startedAt': now(2)}}
        runs.save(stale)
        temporary = self.state.directory / 'tmp/same'
        temporary.mkdir(parents=True)
        evidence = temporary / 'evidence'
        evidence.write_text('preserved')
        async def replace(*args: Any) -> list[int]:
            runs.save(newer)
            return []
        with patch('arbol_claude_driver.continuity.end_run_groups', replace):
            await runs.recover(stale)
        self.assertEqual(read_json(runs.path('same')), newer)
        self.assertTrue(evidence.exists())
        runs.save(stale)
        async def replace_then_fail(*args: Any) -> list[int]:
            runs.save(newer)
            raise Failure('internal', 'cleanup failed')
        with patch('arbol_claude_driver.continuity.end_run_groups', replace_then_fail), self.assertRaises(Failure):
            await runs.recover(stale)
        self.assertEqual(read_json(runs.path('same')), newer)



class TransportTests(_support.TransportTests):
    async def test_output_backpressure_uses_one_absolute_stop_deadline(self) -> None:
        read, write = os.pipe()
        cancel = asyncio.Event()
        output = Output(cancel, write)
        try:
            task = asyncio.create_task(output.write('x' * 1024 * 1024))
            await asyncio.sleep(.02)
            output.stopping(time.monotonic() + .07)
            with self.assertRaises(BrokenPipeError):
                await asyncio.wait_for(task, .5)
            self.assertTrue(cancel.is_set())
        finally:
            os.close(read)
            os.close(write)
