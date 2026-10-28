"""Packaged opt-in checks. Probes never write a user record."""
from __future__ import annotations

import asyncio
from typing import Any

from .core import Env, Failure, Record, now
from .subscriptions import Subscriptions

ENUMERATED = {'control_response', 'result', 'success', 'error', 'error_during_execution', 'none', 'claude.ai', 'firstParty', 'bypassPermissions', 'connected', 'failed', 'pending', 'needs-auth', 'low', 'medium', 'high', 'xhigh', 'max'}
BASE = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'bypassPermissions', '--system-prompt-snapshot', 'off']


def probe_shape(value: Any) -> Any:
    if isinstance(value, list):
        return [probe_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: item if key in {'type', 'subtype', 'authMethod', 'apiProvider', 'current_permission_mode', 'status', 'effort'} and isinstance(item, str) and item in ENUMERATED else probe_shape(item) for key, item in value.items()}
    return None if value is None else 'boolean' if type(value) is bool else 'number' if type(value) in {int, float} else 'string' if isinstance(value, str) else 'object'


def verify(condition: Any, message: str) -> None:
    if not condition:
        raise Failure('tool_unsupported', message)


# TEST-TIER-DISABLED-BEGIN src/arbol_claude_driver/dev.py::probe
# async def probe(subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> Record:
#     tool = await inspect_tool(subscriptions.state.directory, subscriptions.settings.get('toolPath'), inherited, cancel)
#     program = require_tool(tool)
#     temp_root = subscriptions.state.directory / 'tmp'
#     private_directory(temp_root)
#     directory = Path(tempfile.mkdtemp(prefix='probe-', dir=temp_root))
#     checks: list[Record] = []
#     report = {'check': 'dev.probe', 'at': now(), 'toolVersion': tool['version'], 'inferenceRequests': 0, 'checks': checks}
#     def environment(config: Path) -> Env:
#         return {**clean_environment(inherited, ACTUAL_HOME, str(config)), 'PATH': str(Path(sys.executable).parent) + ':' + str(Path(program).parent) + ':/usr/bin:/bin', 'TMPDIR': str(directory), 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1', 'CLAUDE_CODE_MAX_RETRIES': '3'}
#     def home(name: str) -> Path:
#         path = directory / name
#         private_directory(path)
#         return path
#     async def command(config: Path, args: list[str]) -> Record:
#         return await child_command(program, args, environment(config), config, 25, cancel, capture_stderr=True)
#     async def controls(config: Path, args: list[str], requests: list[str]) -> list[Record]:
#         child = await spawn(program, args, environment(config), config)
#         assert child.stdin and child.stdout and child.stderr
#         errors = asyncio.create_task(child.stderr.read())
#         records: list[Record] = []
#         try:
#             async with asyncio.timeout(25):
#                 for subtype in requests:
#                     identifier = str(uuid.uuid4())
#                     child.stdin.write((compact({'type': 'control_request', 'request_id': identifier, 'request': {'subtype': subtype}}) + '\n').encode())
#                     await child.stdin.drain()
#                     answer = None
#                     while line := await child.stdout.readline():
#                         try:
#                             value = loads(line)
#                         except ValueError:
#                             continue
#                         if isinstance(value, dict):
#                             records.append(value)
#                             if isinstance(value.get('response'), dict) and value['response'].get('request_id') == identifier:
#                                 answer = value
#                                 break
#                     verify(answer and answer['response'].get('subtype') == 'success', f'Claude did not accept the {subtype} probe.')
#                 child.stdin.close()
#                 while line := await child.stdout.readline():
#                     try:
#                         value = loads(line)
#                         if isinstance(value, dict):
#                             records.append(value)
#                     except ValueError:
#                         continue
#                 await child.wait()
#                 verify(child.returncode == 0, 'Claude did not end its control probe successfully.')
#             return records
#         finally:
#             kill_group(child.pid, signal.SIGKILL)
#             await child.wait()
#             await errors
#     outcome = 'passed'
#     try:
#         fresh = home('fresh')
#         version = await command(fresh, ['--version'])
#         verify(version['code'] == 0 and version['stdout'].strip() == tool['version'] + ' (Claude Code)', 'Claude changed its version record.')
#         checks.append({'check': 'probe.base-info', 'outcome': 'passed', 'versionLine': tool['version'] + ' (Claude Code)'})
#         status = loads((await command(fresh, ['auth', 'status', '--json']))['stdout'])
#         verify(status.get('loggedIn') is False, 'A disposable Claude home unexpectedly has a login.')
#         catalog = await controls(fresh, BASE, ['initialize'])
#         verify(catalog[0].get('response', {}).get('response', {}).get('models'), 'Claude returned no models without a prompt.')
#         checks.append({'check': 'probe.subscriptions', 'outcome': 'passed', 'freshStatus': probe_shape(status), 'initialize': probe_shape(catalog)})
#         testing = next((item for item in await subscriptions.list() if item['id'] == 'test-anthropic' and item['state'] == 'signed_in'), None)
#         if testing:
#             config = subscriptions.state.subscription(testing['id']) / 'home'
#             status = loads((await command(config, ['auth', 'status', '--json']))['stdout'])
#             verify(status.get('loggedIn') is True, 'The test Subscription login is unavailable.')
#             checks.append({'check': 'probe.signed-subscription', 'outcome': 'passed', 'status': probe_shape(status), 'initialize': probe_shape(await controls(config, BASE, ['initialize']))})
#         login_home, address, browser = home('login'), directory / 'address', directory / 'browser'
#         python_launcher(browser, f'import os,sys\nfd=os.open({str(address)!r},os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)\nos.write(fd,sys.argv[1].encode())\nos.close(fd)\n')
#         child = await spawn(program, ['auth', 'login', '--claudeai'], {**environment(login_home), 'BROWSER': str(browser)}, login_home)
#         assert child.stdout and child.stderr
#         readers = [asyncio.create_task(child.stdout.read()), asyncio.create_task(child.stderr.read())]
#         try:
#             deadline = time.monotonic() + 12
#             while not address.exists() and time.monotonic() < deadline and not cancel.is_set():
#                 await asyncio.sleep(.05)
#             verify(address.exists(), 'Claude did not hand off its login address.')
#             parsed = urllib.parse.urlparse(address.read_text())
#             verify(parsed.hostname in {'claude.com', 'claude.ai'}, 'Claude handed off an unexpected login host.')
#             checks.append({'check': 'probe.browser-handoff', 'outcome': 'passed', 'protocol': parsed.scheme + ':', 'host': parsed.hostname, 'queryKeys': sorted(urllib.parse.parse_qs(parsed.query))})
#         finally:
#             kill_group(child.pid, signal.SIGKILL)
#             await child.wait()
#             await asyncio.gather(*readers)
#         settings = await controls(fresh, [*BASE, '--tools', '', '--model', 'sonnet', '--effort', 'low', '--no-session-persistence'], ['get_settings'])
#         verify(settings[0].get('response', {}).get('response', {}).get('applied', {}).get('effort') == 'low', 'Claude did not apply the requested probe effort.')
#         checks.append({'check': 'probe.run-settings', 'outcome': 'passed', 'records': probe_shape(settings)})
#         missing = await command(fresh, [*BASE, '--tools', '', '--resume', str(uuid.uuid4())])
#         verify(missing['code'] == 1 and 'No conversation found' in missing['stdout'] + missing['stderr'], 'Claude changed its missing-conversation refusal.')
#         session = str(uuid.uuid4())
#         atomic_write(fresh / 'projects' / encode_folder(str(fresh)) / (session + '.jsonl'), '\n'.join(compact(record) for record in session_lines([{'type': 'message', 'role': 'user', 'text': 'Synthetic history, never submitted for inference.'}, {'type': 'tool_call', 'name': 'shell', 'input': {'command': 'printf fixture'}, 'ok': False, 'result': {'output': 'fixture'}}], session, str(fresh))) + '\n')
#         resumed = await controls(fresh, [*BASE, '--tools', '', '--resume', session], ['initialize'])
#         checks.append({'check': 'probe.continuity', 'outcome': 'passed', 'missingExit': missing['code'], 'missingRecord': probe_shape(loads(missing['stdout'].strip())), 'resumed': probe_shape(resumed)})
#         start = time.monotonic()
#         interrupted = await controls(fresh, BASE, ['initialize', 'interrupt'])
#         checks.append({'check': 'probe.stopping', 'outcome': 'passed', 'elapsedMs': round((time.monotonic() - start) * 1000), 'records': probe_shape(interrupted)})
#         tools_home = home('tools')
#         owner_skills(tools_home, inherited.get('HOME', ACTUAL_HOME))
#         tools = await controls(tools_home, [*BASE, '--permission-prompt-tool', 'stdio', *owner_mcp_arguments(inherited.get('HOME', ACTUAL_HOME))], ['initialize', 'mcp_status'])
#         verify(tools[0].get('response', {}).get('response', {}).get('current_permission_mode') == 'bypassPermissions', 'Claude did not apply trusted mode.')
#         checks.append({'check': 'probe.tools', 'outcome': 'passed', 'records': probe_shape(tools)})
#         argument = await command(fresh, ['--arbol-unknown-option'])
#         verify(argument['code'] != 0 and re.search('unknown option|unknown argument', argument['stderr'], re.I), 'Claude changed its argument-error refusal.')
#         checks.append({'check': 'probe.argument-errors', 'outcome': 'passed', 'exit': argument['code'], 'stderr': 'Unknown option diagnostic on stderr; no protocol result.'})
#     except BaseException:
#         outcome = 'failed'
#         checks.append({'check': 'probe.failure', 'outcome': 'failed'})
#         raise
#     finally:
#         shutil.rmtree(directory, ignore_errors=True)
#         write_json(subscriptions.state.directory / 'research' / ('probe-' + report['at'].replace(':', '-') + '.json'), {**report, 'outcome': outcome})
#     return {**report, 'outcome': outcome}
# TEST-TIER-DISABLED-END src/arbol_claude_driver/dev.py::probe
async def probe(subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> Record:
    raise RuntimeError('Tests are commented under the tier policy; see TESTS.md. Restore only in an explicitly requested testing task.')  # TEST-TIER-REFUSAL


async def check_account(subscriptions: Subscriptions) -> Record:
    account = await subscriptions.require_signed_in('test-anthropic')
    if account['family'] != 'anthropic':
        raise Failure('invalid_input', 'test-anthropic must belong to the anthropic family.')
    usage = account.get('lastUsage', {'outcome': 'unsupported', 'fetchedAt': now(), 'windows': []})
    if any(window.get('limitReached') or window.get('usedPercent', 0) >= 100 for window in usage['windows']):
        raise Failure('rate_limited', 'The test Subscription has a usage window used up. Wait before running the check.')
    return dict(usage)


def usage_change(before: Record, after: Record) -> list[Record]:
    changes = []
    for window in after['windows']:
        previous: Record = next((item for item in before['windows'] if item['kind'] == window['kind'] and item['label'] == window['label']), {})
        changes.append({'kind': window['kind'], 'label': window['label'], **({'beforePercent': previous['usedPercent']} if 'usedPercent' in previous else {}), **({'afterPercent': window['usedPercent']} if 'usedPercent' in window else {}), **({'changePercent': round(window['usedPercent'] - previous['usedPercent'], 3)} if 'usedPercent' in previous and 'usedPercent' in window else {})})
    return changes


# TEST-TIER-DISABLED-BEGIN src/arbol_claude_driver/dev.py::live_check
# async def live_check(build_only: bool, subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> tuple[int, Record]:
#     before = await check_account(subscriptions)
#     tool = await inspect_tool(subscriptions.state.directory, subscriptions.settings.get('toolPath'), inherited, cancel)
#     program = require_tool(tool)
#     temp_root = subscriptions.state.directory / 'tmp'
#     private_directory(temp_root)
#     directory = Path(tempfile.mkdtemp(prefix='build-check-' if build_only else 'live-check-', dir=temp_root))
#     cli = Path(__file__).resolve().parents[2] / 'bin/arbol-claude-driver'
#     env = {**clean_environment(inherited, inherited.get('HOME', ACTUAL_HOME)), 'PATH': str(Path(sys.executable).parent) + ':' + str(Path(program).parent) + ':/usr/bin:/bin', **({'ARBOL_TEST_TIME_SCALE': inherited['ARBOL_TEST_TIME_SCALE']} if inherited.get('ARBOL_TEST_TIME_SCALE') else {})}
#     checks: list[Record] = []
#     report: Record = {'check': 'dev.build-check' if build_only else 'dev.live-check', 'at': now(), 'toolVersion': tool['version'], 'subscriptionId': 'test-anthropic', 'model': 'haiku', 'turnsAttempted': 0, 'inferenceRequests': 0, 'tokenUsage': {}, 'usageBefore': before, 'checks': checks}
#     async def message(name: str, prompt: str, tools: bool = False, control: bool = False, stop: bool = False, chat: str | None = None, history: list[Record] | None = None) -> tuple[list[Record], str]:
#         await check_account(subscriptions)
#         request = directory / (str(uuid.uuid4()) + '.json')
#         write_json(request, {'subscriptionId': 'test-anthropic', 'model': 'haiku', 'tools': 'all' if tools else 'none', **({'workingDirectory': str(directory)} if tools or chat else {}), **({'chatSessionId': chat} if chat else {}), 'history': history or [], 'input': {'role': 'user', 'text': prompt}, 'limits': {'totalSeconds': 60}})
#         child = await spawn(str(cli), ['run', '--request', str(request), '--state-dir', str(subscriptions.state.directory), *(['--control'] if control or stop else [])], env, directory)
#         assert child.stdin and child.stdout and child.stderr
#         report['turnsAttempted'] += 1
#         events: list[Record] = []
#         stopped = started = False
#         settled = False
#         errors = asyncio.create_task(child.stderr.read())
#         if not control and not stop:
#             child.stdin.close()
#         try:
#             async with asyncio.timeout(80):
#                 while line := await child.stdout.readline():
#                     record = loads(line)
#                     events.append(record)
#                     if record['type'] == 'started' and not started:
#                         started = True
#                         report['inferenceRequests'] += 1
#                     if record['type'] == 'ask_opened' and not stop:
#                         child.stdin.write((compact({'type': 'answer', 'askId': record['askId'], 'answer': {'type': 'answers', 'answers': {question['id']: [question['options'][0]['label']] for question in record['ask']['questions']}}}) + '\n').encode())
#                     if stop and record['type'] == 'tool_started' and not stopped:
#                         stopped = True
#                         child.stdin.write(b'{"type":"stop"}\n')
#                     if record['type'] == 'finished':
#                         child.stdin.close()
#                 await child.wait()
#             settled = True
#             final = events[-1] if events else {}
#             answer = ''.join(record['text'] for record in events if record['type'] == 'text' and not record.get('parentCallId'))
#             if sum(record['type'] == 'finished' for record in events) != 1 or final.get('outcome') != ('cancelled' if stop else 'completed') or child.returncode != (2 if stop else 0):
#                 raise Failure('tool_failed', f'The {name} check did not finish as expected.')
#             return events, answer
#         finally:
#             final = events[-1] if events else {}
#             usage = final.get('usage', next((record['usage'] for record in reversed(events) if record['type'] == 'usage'), {}))
#             previous_cost = report['tokenUsage'].get('costUsd', 0)
#             report['tokenUsage'] = add_usage(report['tokenUsage'], usage)
#             if 'costUsd' in usage:
#                 report['tokenUsage']['costUsd'] = previous_cost + usage['costUsd']
#             checks.append({'check': name, 'outcome': final.get('outcome', 'failed') if settled else 'failed', 'exit': child.returncode, 'tokenUsage': usage, 'continuity': events[0].get('continuity') if events else None, 'eventTypes': [record['type'] for record in events], **({'error': final['error']} if final.get('error') else {})})
#             kill_group(child.pid, signal.SIGKILL)
#             try:
#                 await wait_exit(child, 2)
#                 await asyncio.wait_for(errors, 2)
#             finally:
#                 close_pipes(child)
#                 errors.cancel()
#                 await asyncio.gather(errors, return_exceptions=True)
#     status = 0
#     error = None
#     try:
#         _, answer = await message('live.ready-usage', 'Answer with the single word: ready')
#         if not re.search(r'\bready\b', answer, re.I):
#             raise Failure('tool_failed', 'The ready answer check did not produce the expected behavior.')
#         if not build_only:
#             events, _ = await message('live.tools-questions', 'Use AskUserQuestion to ask Ready? with options Ready and Later. Then use Bash to run printf ready. Finish with ready.', tools=True, control=True)
#             verify(any(event['type'] == 'ask_opened' for event in events) and any(event['type'] == 'tool_finished' and event['ok'] for event in events), 'The tools and questions check did not produce the expected behavior.')
#             events, answer = await message('live.background', 'Start one Haiku sub-agent in the background. Tell it to run printf ready and return ready. Wait for it and finish with ready.', tools=True)
#             verify(any(event.get('parentCallId') for event in events) and re.search(r'\bready\b', answer, re.I), 'The background sub-agent check did not produce the expected behavior.')
#             chat, first_text = str(uuid.uuid4()), 'Remember the word willow. Answer only ready.'
#             _, first = await message('live.continuity-fresh', first_text, chat=chat)
#             events, answer = await message('live.continuity-resumed', 'What word did I give you? Answer only that word.', chat=chat, history=[{'type': 'message', 'role': 'user', 'text': first_text}, {'type': 'message', 'role': 'assistant', 'text': first}])
#             verify(events[0].get('continuity') == 'resumed' and re.search(r'\bwillow\b', answer, re.I), 'The continuity check did not produce the expected behavior.')
#             await message('live.stopping', 'Use Bash to run sleep 8 then printf ready.', tools=True, stop=True)
#     except BaseException as failure:
#         error, status = failure, 2 if cancel.is_set() else 1
#     finally:
#         shutil.rmtree(directory, ignore_errors=True)
#     after = (await subscriptions.get('test-anthropic')).get('lastUsage', before)
#     result = {**report, 'outcome': 'passed' if status == 0 else 'cancelled' if status == 2 else 'failed', 'usageAfter': after, 'usageChange': usage_change(before, after), **({'error': safe_failure(error)} if error else {})}
#     write_json(subscriptions.state.directory / 'research' / (('build-check-' if build_only else 'live-check-') + report['at'].replace(':', '-') + '.json'), result)
#     return status, result
# TEST-TIER-DISABLED-END src/arbol_claude_driver/dev.py::live_check
async def live_check(build_only: bool, subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> tuple[int, Record]:
    raise RuntimeError('Tests are commented under the tier policy; see TESTS.md. Restore only in an explicitly requested testing task.')  # TEST-TIER-REFUSAL

# Archived complete imports; restore needed names in their original scope, not at this file tail.
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:5
# import re
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:5
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:6
# import shutil
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:6
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:7
# import signal
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:7
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:8
# import sys
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:8
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:9
# import tempfile
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:9
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:10
# import time
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:10
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:11
# import urllib.parse
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:11
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:12
# import uuid
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:12
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:13
# from pathlib import Path
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:13
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:16
# from .core import Env, Failure, Record, clean_environment, compact, now, safe_failure, loads
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:16
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:17
# from .history import encode_folder, session_lines
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:17
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:18
# from .processes import ACTUAL_HOME, child_command, close_pipes, inspect_tool, kill_group, owner_mcp_arguments, owner_skills, python_launcher, require_tool, spawn, wait_exit
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:18
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:19
# from .protocol import add_usage
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:19
# TEST-TIER-IMPORTS-BEGIN src/arbol_claude_driver/dev.py:20
# from .state import atomic_write, private_directory, write_json
# TEST-TIER-IMPORTS-END src/arbol_claude_driver/dev.py:20
