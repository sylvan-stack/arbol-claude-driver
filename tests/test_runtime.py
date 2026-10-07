"""Offline boundaries, format-1 compatibility and asynchronous lifecycle regressions."""
from __future__ import annotations

import asyncio
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from arbol_claude_driver.continuity import Chats, Runs  # noqa: E402
from arbol_claude_driver.core import Failure, Record, clean_environment, compact, loads, now, parse_arguments, read_settings, safe_failure, utf8  # noqa: E402
from arbol_claude_driver.history import MessageDigest, digest, encode_folder, result_text, session_lines  # noqa: E402
from arbol_claude_driver.dev import live_check  # noqa: E402
from arbol_claude_driver.models import ValidatedRequest  # noqa: E402
from arbol_claude_driver.processes import child_command, remaining_groups, supported_version  # noqa: E402
from arbol_claude_driver.protocol import Output, RunEvents, RunProtocol, run_failure, truncate_result  # noqa: E402
from arbol_claude_driver.request import read_request, run_arguments  # noqa: E402
from arbol_claude_driver.state import State, owner_alive, parse_owner, process_owner, read_json, write_json  # noqa: E402
from arbol_claude_driver.subscriptions import Authentication, Secrets, Subscriptions, family_settings, keychain_service, normalize_quota, validate_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CompatibilityTests(unittest.TestCase):
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

    def test_golden_chat_keys_utf16_paths_and_keychain_identity(self) -> None:
        vectors = loads((ROOT / 'tests/fixtures/migration-vectors.json').read_text())
        for case in vectors['chats']:
            self.assertEqual(digest(case['chat']), case['expected']['claude'])
        for case in vectors['folders']:
            self.assertEqual(encode_folder(case['folder']), case['expected'])
        self.assertEqual(keychain_service(Path('/example-home/Library/Application Support/Arbol/drivers/claude'), '/example-home'), vectors['keychain']['defaultService'])
        example = vectors['keychain']['customExample']
        self.assertEqual(keychain_service(Path(example['realPath'])), example['service'])
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / 'real'
            real.mkdir()
            alias = Path(folder) / 'alias'
            alias.symlink_to(real, target_is_directory=True)
            self.assertEqual(keychain_service(real), keychain_service(alias))

    def test_json_stringify_numbers_indices_controls_and_surrogates(self) -> None:
        self.assertEqual(compact({'z': 1.0, '10': 1e-6, '2': 1e21, '01': -0.0}), '{"2":1e+21,"10":0.000001,"z":1,"01":0}')
        self.assertEqual(compact('\ud83d\ude00\ud83d\n\x00'), '"😀\\ud83d\\n\\u0000"')
        self.assertEqual(utf8('\ud83d'), b'\xef\xbf\xbd')
        for value in ('NaN', 'Infinity', '-Infinity', '{"n":NaN}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loads(value)

    def test_independent_node24_json_golden_vectors(self) -> None:
        fixture = loads((ROOT / 'tests/fixtures/migration-json-vectors.json').read_text())
        for case in fixture['vectors']:
            with self.subTest(input_json=case['inputJson']):
                self.assertEqual(compact(loads(case['inputJson'])), case['expected'])

    def test_native_history_links_images_shell_and_error_results(self) -> None:
        history: list[Record] = [
            {'type': 'message', 'role': 'user', 'text': 'hi', 'images': [{'mediaType': 'image/png', 'data': 'aGk='}]},
            {'type': 'tool_call', 'name': 'shell', 'input': {'command': 'pwd', 'ignored': True}, 'ok': False, 'result': {'output': 'failed', 'exitCode': 2}, 'error': 'reason'},
        ]
        rows = session_lines(history, 'conversation', '/folder', 2000)
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0]['parentUuid'])
        self.assertEqual(rows[1]['parentUuid'], rows[0]['uuid'])
        self.assertEqual(rows[2]['parentUuid'], rows[1]['uuid'])
        self.assertEqual(rows[0]['message']['content'][1]['source']['data'], 'aGk=')
        use = rows[1]['message']['content'][0]
        self.assertEqual(use['name'], 'Bash')
        self.assertEqual(use['input'], {'command': 'pwd'})
        self.assertEqual(rows[2]['message']['content'][0]['tool_use_id'], use['id'])
        self.assertEqual(result_text(history[1]), 'failed\nexit code: 2\nreason')

    def test_failure_closed_codes_retryability_redaction_and_truncation(self) -> None:
        error = Failure('rate_limited', 'secret is rejected', {'detail': {'excerpt': '😀' * 400 + 'secret'}, 'retryAfterSeconds': 4}, ['secret'])
        self.assertTrue(error.result['retryable'])
        self.assertNotIn('secret', compact(error.result))
        self.assertLessEqual(len(utf8(error.result['detail']['excerpt'])), 1024)
        self.assertNotIn('retryAfterSeconds', Failure('vendor_error', 'bad', {'retryAfterSeconds': 3}).result)
        self.assertEqual(safe_failure(ValueError('private text'))['code'], 'internal')
        with self.assertRaises(ValueError):
            Failure('invented', 'bad')

    def test_settings_unknown_values_withheld_and_environment_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'settings.json'
            file.write_text('{"unknown":"secret-value","subscriptions":{"zai":{"settings":{"extra":"secret-value"}}}}')
            settings, notices = read_settings(file)
            family_settings('zai', settings['subscriptions']['zai']['settings'], 'zai.', notices)
            self.assertNotIn('secret-value', compact(notices))
        env = clean_environment({'HOME': '/wrong', 'PATH': '/bin', 'LANG': 'C', 'ANTHROPIC_AUTH_TOKEN': 'secret', 'AWS_SECRET_ACCESS_KEY': 'secret', 'NODE_OPTIONS': 'bad'}, '/account', '/config')
        self.assertEqual(env['HOME'], '/account')
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], '/config')
        self.assertNotIn('NODE_OPTIONS', env)
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)

    def test_key_validation_and_quota_math_resets_and_order(self) -> None:
        validate_key('ASCII-key!')
        with self.assertRaises(Failure) as caught:
            validate_key('bad\n😀')
        self.assertIn('U+000A', str(caught.exception))
        current = 1000
        report = normalize_quota({'success': True, 'data': {'limits': [
            {'type': 'CREDIT_LIMIT', 'unit': 6, 'number': 1, 'percentage': 40},
            {'type': 'TOKENS_LIMIT', 'unit': 3, 'number': 5, 'percentage': 99, 'usage': 100, 'remaining': 75, 'nextResetTime': (current + 400 * 60) * 1000},
            {'type': 'TIME_LIMIT', 'percentage': 7},
        ]}}, current)
        self.assertEqual([window['usedPercent'] for window in report['windows']], [25, 40, 7])
        self.assertEqual(report['windows'][0]['windowMinutes'], 300)
        self.assertNotIn('resetsAt', report['windows'][0])

    def test_command_frame_rejects_unknowns_and_preserves_literal_prompt(self) -> None:
        parsed = parse_arguments(['--state-dir', '/state', 'run', '--tools=none', '--label', 'x=a=b', '--', '--literal'])
        self.assertEqual(parsed.positionals, ['--literal'])
        self.assertEqual(parsed.options['label'], ['x=a=b'])
        for args in (['wat'], ['info', '--bad'], ['subscriptions', 'create', '--force'], ['run', '--tools']):
            with self.subTest(args=args), self.assertRaises(Failure):
                parse_arguments(args)
        self.assertTrue(supported_version('2.1.289'))
        self.assertFalse(supported_version('2.1.288'))
        self.assertFalse(supported_version('2.1.289-beta'))


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = State(str(self.root / 'state'))
        self.state.initialize()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

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

    async def test_process_identity_and_lock_contention(self) -> None:
        owner = await process_owner(os.getpid())
        self.assertIsNotNone(owner)
        assert owner
        self.assertTrue(await owner_alive(owner))
        with self.assertRaises(Failure):
            parse_owner({'pid': True, 'startedAt': now()})
        order: list[str] = []
        async def contender() -> None:
            async with self.state.lock('anthropic'):
                order.append('second')
        async with self.state.lock('anthropic'):
            task = asyncio.create_task(contender())
            await asyncio.sleep(.03)
            self.assertEqual(order, [])
            order.append('first')
        await task
        self.assertEqual(order, ['first', 'second'])

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

    async def test_duplicate_run_identifiers_and_safe_paths(self) -> None:
        runs = Runs(self.state)
        request = {'runId': 'one', 'subscriptionId': 'anthropic'}
        await runs.reserve(request, 'conversation')
        with self.assertRaises(Failure) as caught:
            await runs.reserve(request, 'other')
        self.assertEqual(caught.exception.result['code'], 'in_use')
        for identifier in ('../bad', '/absolute', '.', 'x/y'):
            with self.subTest(identifier=identifier), self.assertRaises(Failure):
                runs.path(identifier)
        self.assertNotEqual(Chats(self.state).path('anthropic', digest('chat')), Chats(self.state).path('zai', digest('chat')))

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

    async def test_failed_live_check_reports_observed_cost_and_one_attempt(self) -> None:
        subscriptions = Subscriptions(self.state, {}, [], self.root / '.arbol/settings.json')
        await subscriptions.create('anthropic', 'Test', 'test-anthropic')
        authenticate = Authentication(subscriptions, {'HOME': str(self.root)}, asyncio.Event())
        await authenticate.save('test-anthropic', lambda value: {**value, 'state': 'signed_in'})
        async def fixture_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            code = 'print(\'{"type":"started","continuity":"fresh"}\');print(\'{"type":"usage","usage":{"inputTokens":3,"outputTokens":2,"costUsd":0.01}}\');print("malformed")'
            return await asyncio.create_subprocess_exec(sys.executable, '-c', code, start_new_session=True, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        with patch('arbol_claude_driver.dev.inspect_tool', AsyncMock(return_value={'supported': True, 'path': sys.executable, 'version': '2.1.289'})), patch('arbol_claude_driver.dev.spawn', fixture_spawn):
            status, report = await live_check(True, subscriptions, {'HOME': str(self.root)}, asyncio.Event())
        self.assertEqual(status, 1)
        self.assertEqual(report['inferenceRequests'], 1)
        self.assertEqual(report['turnsAttempted'], 1)
        self.assertEqual(report['tokenUsage'], {'inputTokens': 3, 'outputTokens': 2, 'costUsd': .01})
        self.assertEqual(report['checks'][0]['outcome'], 'failed')
        self.assertEqual(report['checks'][0]['tokenUsage'], report['tokenUsage'])
        self.assertEqual(len(list((self.state.directory / 'research').glob('build-check-*.json'))), 1)

    async def test_live_check_signed_in_gate_precedes_tool_and_prompt(self) -> None:
        subscriptions = Subscriptions(self.state, {}, [], self.root / '.arbol/settings.json')
        await subscriptions.create('anthropic', 'Test', 'test-anthropic')
        with patch('arbol_claude_driver.dev.inspect_tool', AsyncMock()) as inspect, patch('arbol_claude_driver.dev.spawn', AsyncMock()) as spawn:
            with self.assertRaises(Failure) as caught:
                await live_check(True, subscriptions, {}, asyncio.Event())
        self.assertEqual(caught.exception.result['code'], 'not_signed_in')
        inspect.assert_not_called()
        spawn.assert_not_called()


class TransportTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_group_cleanup_refuses_a_reused_leader(self) -> None:
        owner: Record = {'pid': 123, 'startedAt': now(1)}
        self.assertEqual(remaining_groups([owner], [{'pid': 123, 'group': 456, 'zombie': False, 'startedAt': now(1)}]), [])

    async def test_secret_command_fresh_environment_and_single_newline(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            counter = folder / 'counter'
            script = folder / 'source'
            script.write_text(f'#!{sys.executable}\nimport pathlib,os\np=pathlib.Path({str(counter)!r});n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n));print(str(n)+"key");assert "ANTHROPIC_AUTH_TOKEN" not in os.environ\n')
            script.chmod(0o700)
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


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, value: Record, args: list[str] | None = None) -> ValidatedRequest:
        async def first_line() -> bytes:
            return utf8(compact(value))
        return await read_request(parse_arguments(['run', '--request', '-', *(args or [])]), {}, first_line, lambda identifier, labels: None)

    async def test_typed_request_cli_replacement_labels_and_literal_prompt(self) -> None:
        value = {'runId': 'caller', 'subscriptionId': 'anthropic', 'history': [], 'tools': 'none', 'labels': {'x': 'file'}, 'input': {'role': 'user', 'text': 'file'}, 'additionalDirectories': ['/']}
        parsed = await self.read(value, ['--tools', 'all', '--cwd', '/', '--add-dir', '/tmp', '--label', 'x=cli', '--', 'literal'])
        self.assertIsInstance(parsed, ValidatedRequest)
        self.assertEqual(parsed.tools, 'all')
        self.assertEqual(parsed.record()['additionalDirectories'], ['/tmp'])
        self.assertEqual(parsed.record()['input']['text'], 'literal')
        self.assertEqual(parsed.record()['labels'], {'x': 'cli'})
        args = run_arguments(parsed.record(), 'conversation', True)
        self.assertIn('--permission-prompt-tool', args)
        self.assertIn('bypassPermissions', args)
        self.assertIn('--system-prompt-snapshot', args)

    async def test_malformed_nested_json_values_are_invalid_input(self) -> None:
        base: Record = {'history': [], 'tools': 'none', 'input': {'role': 'user', 'text': 'hi'}}
        changes: list[Record] = [{'tools': {}}, {'runId': []}, {'history': [{'type': 'message', 'role': {}, 'text': 'hi'}]}, {'input': {'role': 'user', 'text': 'hi', 'images': [{'mediaType': {}, 'data': 'aGk='}]}}, {'limits': {'idleSeconds': True}}, {'history': [{'type': 'tool_call', 'name': 'shell', 'input': {}, 'ok': True}]}, {'input': {'role': 'user', 'text': 'hi', 'images': [{'mediaType': 'image/png', 'data': 'bad'}]}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Failure) as caught:
                await self.read({**base, **change})
            self.assertEqual(caught.exception.result['code'], 'invalid_input')


class MemoryOutput(Output):
    def __init__(self) -> None:
        self.records: list[Record] = []
        self.text = ''
        self.stop_deadline: float | None = None
        self.stop_output = asyncio.Event()

    async def json(self, value: Any) -> None:
        self.records.append(value)

    async def write(self, text: str) -> None:
        self.text += text


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.output = MemoryOutput()
        self.events = RunEvents(self.output)
        self.sent: list[Record] = []
        async def send(value: Record) -> None:
            self.sent.append(value)
        self.protocol = RunProtocol(self.events, {'subscriptionId': 'anthropic'}, 'dev', '2.1.289', 'anthropic', send, {'continuity': 'fresh'}, True)

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

    async def test_utf8_tool_result_cut_and_single_final_cleanup(self) -> None:
        result = truncate_result('😀' * 60000)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(utf8(result['result'])), 200 * 1024)
        self.assertNotIn('\ufffd', result['result'])
        self.events.calls['open'] = {'name': 'shell'}
        self.events.asks['ask'] = None
        self.events.usage = {'inputTokens': 2}
        await self.events.finish(None, True)
        count = len(self.output.records)
        await self.events.finish(None, True)
        self.assertEqual(len(self.output.records), count)
        self.assertEqual([row['type'] for row in self.output.records], ['ask_closed', 'tool_finished', 'finished'])
        self.assertEqual(self.output.records[-1]['outcome'], 'cancelled')
        self.assertEqual(self.output.records[-1]['usage'], {'inputTokens': 2})


class CliLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacing_signin_preserves_failure_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / '.arbol').mkdir()
            tool = home / 'tool'
            tool.mkdir()
            vendor = tool / 'vendor-tool'
            vendor.write_text('#!/bin/sh\nexec ' + str(ROOT / 'bin/arbol-claude-driver') + ' dev stand-in ' + str(tool) + ' -- "$@"\n')
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


if __name__ == '__main__':
    unittest.main()
