"""Mid-tier selections. Write or run only in an explicitly requested testing task."""
from __future__ import annotations

import test_runtime as _support
from test_runtime import (Authentication ,Chats ,Failure ,MessageDigest ,Path ,ROOT ,Runs ,Secrets ,Subscriptions ,asyncio ,child_command ,compact ,digest ,kill_group ,loads ,now ,os ,python_launcher ,read_json ,run_failure ,shlex ,signal ,stat ,subprocess ,sys ,tempfile ,time ,write_json)


class CompatibilityTests(_support.CompatibilityTests):
# TEST-TIER-BEGIN tests/test_runtime.py::CompatibilityTests.test_golden_histories_and_every_incremental_snapshot
    def test_golden_histories_and_every_incremental_snapshot(self) -> None:
        vectors = loads((ROOT / 'tests/fixtures/migration-vectors.json').read_text())
        for case in vectors['histories']:
            with self.subTest(history=case['name']):
                value = MessageDigest()
                snapshots = [value.snapshot()]
                for item in case['history']:
                    value.add(item)
                    snapshots.append(value.snapshot())
                self.assertEqual(value.snapshot(), {key: case['canonical'][key] for key in ('messages', 'digest')})
                expected = case['expected']['codex' if case['name'] == 'split-surrogate-pair' else 'claude']
                self.assertEqual(snapshots, expected['snapshots'])
                if case['name'] == 'split-surrogate-pair':
                    self.assertNotEqual(value.snapshot()['digest'], case['expected']['claude']['digest'])
# TEST-TIER-END tests/test_runtime.py::CompatibilityTests.test_golden_histories_and_every_incremental_snapshot


# TEST-TIER-BEGIN tests/test_runtime.py::CompatibilityTests.test_independent_node24_json_golden_vectors
    def test_independent_node24_json_golden_vectors(self) -> None:
        fixture = loads((ROOT / 'tests/fixtures/migration-json-vectors.json').read_text())
        for case in fixture['vectors']:
            with self.subTest(input_json=case['inputJson']):
                self.assertEqual(compact(loads(case['inputJson'])), case['expected'])
# TEST-TIER-END tests/test_runtime.py::CompatibilityTests.test_independent_node24_json_golden_vectors


class ProcessGroupTests(_support.ProcessGroupTests):
# TEST-TIER-BEGIN tests/test_runtime.py::ProcessGroupTests.test_a_group_that_has_exited_but_is_not_reaped_is_ending
    def test_a_group_that_has_exited_but_is_not_reaped_is_ending(self) -> None:
        # macOS answers EPERM for such a group; the run must not fail with internal (C29, 14 October 2026).
        child = subprocess.Popen(['/usr/bin/true'], start_new_session=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(child.pid)], capture_output=True, text=True).stdout.strip()[:1] != 'Z':
            time.sleep(.01)
        try:
            kill_group(child.pid, signal.SIGKILL)
        finally:
            child.wait()
        kill_group(child.pid, signal.SIGKILL)
# TEST-TIER-END tests/test_runtime.py::ProcessGroupTests.test_a_group_that_has_exited_but_is_not_reaped_is_ending


class StateTests(_support.StateTests):
# TEST-TIER-BEGIN tests/test_runtime.py::StateTests.test_private_atomic_state_and_newer_format_is_untouched
    async def test_private_atomic_state_and_newer_format_is_untouched(self) -> None:
        file = self.state.subscription('anthropic') / 'record.json'
        write_json(file, {'unicode': '😀'})
        self.assertEqual(read_json(file), {'unicode': '😀'})
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(file.parent.stat().st_mode), 0o700)
        (self.state.directory / 'format').write_text('2\n')
        before = {str(path.relative_to(self.state.directory)): path.read_bytes() for path in self.state.directory.rglob('*') if path.is_file()}
        with self.assertRaises(Failure) as caught:
            self.state.initialize()
        self.assertEqual(caught.exception.result['code'], 'state_newer')
        self.assertEqual(before, {str(path.relative_to(self.state.directory)): path.read_bytes() for path in self.state.directory.rglob('*') if path.is_file()})
# TEST-TIER-END tests/test_runtime.py::StateTests.test_private_atomic_state_and_newer_format_is_untouched


# TEST-TIER-BEGIN tests/test_runtime.py::StateTests.test_subscriptions_late_check_and_usage_cannot_overwrite_newer_state
    async def test_subscriptions_late_check_and_usage_cannot_overwrite_newer_state(self) -> None:
        settings_file = self.root / '.arbol/settings.json'
        subscriptions = Subscriptions(self.state, {}, [], settings_file)
        auth = Authentication(subscriptions, {'HOME': str(self.root)}, asyncio.Event())
        created = await subscriptions.create('anthropic', 'Account', 'named')
        await auth.save('named', lambda value: {**value, 'state': 'signed_in', 'checkedAt': now(10)})
        initial = await subscriptions.get('named')
        await auth.save('named', lambda value: {**auth.unsigned(value), 'label': 'Latest'})
        await subscriptions.run_report(initial, dict(Failure('credential_rejected', 'late response').result), {'outcome': 'ok', 'fetchedAt': now(), 'windows': []})
        latest = await subscriptions.get('named')
        self.assertEqual(latest['state'], 'signed_out')
        self.assertEqual(latest['label'], 'Latest')
        self.assertNotIn('lastUsage', latest)
        self.assertEqual(created['id'], 'named')
# TEST-TIER-END tests/test_runtime.py::StateTests.test_subscriptions_late_check_and_usage_cannot_overwrite_newer_state

# TEST-TIER-BEGIN tests/test_runtime.py::StateTests.test_legacy_split_surrogate_map_rebuilds_safely
    async def test_legacy_split_surrogate_map_rebuilds_safely(self) -> None:
        vectors = loads((ROOT / 'tests/fixtures/migration-vectors.json').read_text())
        case = next(case for case in vectors['histories'] if case['name'] == 'split-surrogate-pair')
        chats, runs = Chats(self.state), Runs(self.state)
        chat = 'split-pair'
        home = self.state.subscription('anthropic') / 'home'
        file = home / 'projects/folder/00000000-0000-4000-8000-000000000000.jsonl'
        write_json(file, {})
        write_json(chats.path('anthropic', digest(chat)), {'chatSessionId': chat, 'conversation': file.stem, 'workingDirectory': str(self.root), **{key: case['expected']['claude'][key] for key in ('messages', 'digest')}, 'systemPromptDigest': digest(''), 'writtenAt': now()})
        request = {'runId': 'rebuild', 'subscriptionId': 'anthropic', 'chatSessionId': chat, 'workingDirectory': str(self.root), 'history': case['history']}
        plan, _ = await chats.reserve(request, runs, home)
        self.assertEqual(plan['continuity'], 'rebuilt')
        self.assertEqual(plan['rebuildReason'], 'history_differs')
        self.assertFalse(plan['resume'])
# TEST-TIER-END tests/test_runtime.py::StateTests.test_legacy_split_surrogate_map_rebuilds_safely


class TransportTests(_support.TransportTests):
# TEST-TIER-BEGIN tests/test_runtime.py::TransportTests.test_generated_python_helper_quotes_spaced_interpreter_and_arguments
    async def test_generated_python_helper_quotes_spaced_interpreter_and_arguments(self) -> None:
        with tempfile.TemporaryDirectory(prefix='helper path ') as name:
            interpreter = Path(name) / "python with space ' quoted"
            interpreter.symlink_to(sys.executable)
            helper = Path(name) / 'browser helper'
            python_launcher(helper, 'import sys;print(repr(sys.argv[1:]))', str(interpreter))
            arguments = ["argument with ' quote", '$literal\\path', 'https://claude.com/?code=one&state=two']
            result = await child_command(str(helper), arguments, {'PATH': '/usr/bin:/bin'}, name, 2)
            self.assertEqual(result['code'], 0)
            self.assertEqual(result['stdout'], repr(arguments) + '\n')
            self.assertTrue(helper.read_text().startswith('#!/bin/sh\nexec '))
# TEST-TIER-END tests/test_runtime.py::TransportTests.test_generated_python_helper_quotes_spaced_interpreter_and_arguments

# TEST-TIER-BEGIN tests/test_runtime.py::TransportTests.test_child_command_drains_stderr_and_honors_cancel
    async def test_child_command_drains_stderr_and_honors_cancel(self) -> None:
        env = {'PATH': '/usr/bin:/bin'}
        result = await child_command(sys.executable, ['-c', 'import sys;sys.stderr.write("x"*300000);print("ok")'], env, '/', 2)
        self.assertEqual(result, {'code': 0, 'stdout': 'ok\n'})
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(.05, cancel.set)
        started = time.monotonic()
        with self.assertRaises(Failure):
            await child_command(sys.executable, ['-c', 'import time;time.sleep(60)'], env, '/', 2, cancel)
        self.assertLess(time.monotonic() - started, 1)
# TEST-TIER-END tests/test_runtime.py::TransportTests.test_child_command_drains_stderr_and_honors_cancel

# TEST-TIER-BEGIN tests/test_runtime.py::TransportTests.test_detached_pipe_holder_cannot_keep_command_alive
    async def test_detached_pipe_holder_cannot_keep_command_alive(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            ledger = Path(name) / 'pid'
            code = 'import subprocess,pathlib,sys; child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);pathlib.Path(sys.argv[1]).write_text(str(child.pid));print("done")'
            started = time.monotonic()
            try:
                result = await child_command(sys.executable, ['-c', code, str(ledger)], {'PATH': '/usr/bin:/bin'}, '/', 2)
                self.assertEqual(result['code'], 0)
                self.assertLess(time.monotonic() - started, 2)
            finally:
                if ledger.exists():
                    try:
                        os.killpg(int(ledger.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
# TEST-TIER-END tests/test_runtime.py::TransportTests.test_detached_pipe_holder_cannot_keep_command_alive


# TEST-TIER-BEGIN tests/test_runtime.py::TransportTests.test_secret_command_fresh_environment_and_single_newline
    async def test_secret_command_fresh_environment_and_single_newline(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            counter = folder / 'counter'
            script = folder / 'source'
            python_launcher(script, f'import pathlib,os\np=pathlib.Path({str(counter)!r});n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n));print(str(n)+"key");assert "ANTHROPIC_AUTH_TOKEN" not in os.environ\n')
            secrets = Secrets('service', name, {'HOME': name, 'PATH': '/bin', 'ANTHROPIC_AUTH_TOKEN': 'must-not-leak'})
            source = {'from': 'command', 'command': [str(script)]}
            self.assertEqual(await secrets.read('account', source), '1key')
            self.assertEqual(await secrets.read('account', source), '2key')
            script.write_text('#!/bin/sh\nprintf "key\\n\\n"\n')
            self.assertEqual(await secrets.read('account', source), 'key\n')
            script.write_text('#!/bin/sh\nprintf "private-secret"\nexit 1\n')
            with self.assertRaises(Failure) as caught:
                await secrets.read('account', source)
            self.assertNotIn('private-secret', compact(caught.exception.result))
# TEST-TIER-END tests/test_runtime.py::TransportTests.test_secret_command_fresh_environment_and_single_newline


class ProtocolTests(_support.ProtocolTests):
# TEST-TIER-BEGIN tests/test_runtime.py::ProtocolTests.test_questions_validate_answers_duplicate_ack_and_withdrawal
    async def test_questions_validate_answers_duplicate_ack_and_withdrawal(self) -> None:
        await self.protocol.accept({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'call', 'name': 'AskUserQuestion', 'input': {}}]}})
        await self.protocol.accept({'type': 'control_request', 'request_id': 'ask', 'request': {'subtype': 'can_use_tool', 'tool_name': 'AskUserQuestion', 'tool_use_id': 'call', 'input': {'questions': [{'question': 'Choose', 'options': [{'label': 'Blue'}, {'label': 'Red'}]}]}}})
        with self.assertRaises(Failure):
            await self.protocol.answer('ask', {'type': 'answers', 'answers': {'Choose': ['private value']}})
        self.assertIn('ask', self.protocol.questions)
        self.assertFalse(await self.protocol.answer('ask', {'type': 'answers', 'answers': {'Choose': ['Blue']}}))
        self.assertFalse(await self.protocol.answer('ask', {'type': 'answers', 'answers': {'Choose': ['Blue']}}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.output.records[-1]['type'], 'ask_closed')
        self.events.asks['withdrawn'] = None
        await self.protocol.accept({'type': 'control_cancel_request', 'request_id': 'withdrawn'})
        self.assertEqual(self.output.records[-1]['reason'], 'withdrawn')
# TEST-TIER-END tests/test_runtime.py::ProtocolTests.test_questions_validate_answers_duplicate_ack_and_withdrawal

# TEST-TIER-BEGIN tests/test_runtime.py::ProtocolTests.test_error_fields_override_wording_and_usage_never_double_counts
    async def test_error_fields_override_wording_and_usage_never_double_counts(self) -> None:
        error = run_failure({'api_error_status': 401, 'result': 'rate limit reached'}, None, 'anthropic', '2.1.289', [])
        self.assertEqual(error.result['code'], 'credential_rejected')
        for value in [
            {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'usage': {'input_tokens': 3}}}},
            {'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': 4}}},
            {'type': 'stream_event', 'event': {'type': 'message_stop'}},
            {'type': 'result', 'is_error': False, 'usage': {'input_tokens': 5, 'output_tokens': 6}, 'total_cost_usd': 9, 'modelUsage': {'haiku': {'costBasis': 'subscription'}}},
        ]:
            await self.protocol.accept(value)
        self.assertEqual(self.events.usage, {'inputTokens': 5, 'outputTokens': 6, 'model': 'haiku'})
        self.assertNotIn('costUsd', self.events.usage)
        self.assertEqual([record['seq'] for record in self.output.records], [1, 2])
# TEST-TIER-END tests/test_runtime.py::ProtocolTests.test_error_fields_override_wording_and_usage_never_double_counts


class CliLifecycleTests(_support.CliLifecycleTests):
# TEST-TIER-BEGIN tests/test_runtime.py::CliLifecycleTests.test_replacing_signin_preserves_failure_exit_status
    async def test_replacing_signin_preserves_failure_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / '.arbol').mkdir()
            tool = home / 'tool'
            tool.mkdir()
            vendor = tool / 'vendor-tool'
            vendor.write_text('#!/bin/sh\nexec ' + shlex.join([str(ROOT / 'bin/arbol-claude-driver'), 'dev', 'stand-in', str(tool), '--']) + ' "$@"\n')
            vendor.chmod(0o700)
            (home / '.arbol/arbol-claude-driver.json').write_text(compact({'toolPath': str(vendor)}))
            (tool / 'scenario').write_text('C2 waiting\n')
            env = {**os.environ, 'HOME': name, 'ARBOL_TEST_TIME_SCALE': '10'}
            args = [str(ROOT / 'bin/arbol-claude-driver'), 'subscriptions', 'sign-in', 'anthropic']
            first = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                assert first.stdout
                opened = loads(await asyncio.wait_for(first.stdout.readline(), 3))
                self.assertEqual(opened['step'], 'open_url')
                (tool / 'scenario').write_text('C2 success\n')
                second = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                second_out, _ = await asyncio.wait_for(second.communicate(), 4)
                self.assertEqual(second.returncode, 0, second_out)
                first_out, _ = await asyncio.wait_for(first.communicate(), 4)
                self.assertEqual(first.returncode, 1, first_out)
                self.assertEqual(loads(first_out)['error']['code'], 'in_use')
            finally:
                if first.returncode is None:
                    first.kill()
                    await first.wait()
# TEST-TIER-END tests/test_runtime.py::CliLifecycleTests.test_replacing_signin_preserves_failure_exit_status
