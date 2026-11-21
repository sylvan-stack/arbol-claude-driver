"""Packaged opt-in checks. Probes never write a user record."""
from __future__ import annotations
import re
import shutil
import signal
import sys
import tempfile
import uuid
from pathlib import Path

from .admission import current_admission
from .core import Env, Failure, Record, clean_environment, compact, now, safe_failure, loads
from .processes import ACTUAL_HOME, close_pipes, inspect_tool, kill_group, require_tool, wait_exit
from .protocol import add_usage
from .state import private_directory, write_json


import asyncio

from .subscriptions import Authentication, Subscriptions, native_sign_in_required

def verify(condition: object, message: str) -> None:
    if not condition:
        raise Failure('tool_unsupported', message)


async def probe(subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> Record:
    report: Record = {'check': 'dev.probe', 'at': now(), 'inferenceRequests': 0, 'outcome': 'failed'}
    checks: list[Record] = []
    try:
        tool = await inspect_tool(subscriptions.state.directory, subscriptions.settings.get('toolPath'), inherited, cancel)
        program = require_tool(tool)
        report['toolVersion'] = tool['version']
        checks.append({'check': 'probe.base-info', 'outcome': 'passed', 'toolVersion': tool['version']})
        status = await Authentication(subscriptions, inherited, cancel).status('anthropic', program=program)
        signed_in = status['accepted'] is True
        checks.append({'check': 'probe.native-status', 'outcome': 'passed' if signed_in else 'failed',
                       'signedIn': signed_in})
        if not signed_in:
            raise native_sign_in_required()
        report['outcome'] = 'passed'
    finally:
        report['checks'] = checks
        receipt = 'probe-' + report['at'].replace(':', '-') + '.json'
        write_json(subscriptions.state.directory / 'research' / receipt, report)
    return report


async def check_account(subscriptions: Subscriptions, identifier: str = 'anthropic') -> Record:
    subscriptions.require_native_route(identifier)
    if not (subscriptions.state.subscription(identifier) / 'record.json').is_file():
        raise Failure('not_found', 'Choose the existing anthropic Subscription before live qualification.')
    account = await subscriptions.get(identifier)
    if account['state'] != 'signed_in':
        raise native_sign_in_required()
    usage = account.get('lastUsage', {'outcome': 'unsupported', 'fetchedAt': now(), 'windows': []})
    if any(window.get('limitReached') or window.get('usedPercent', 0) >= 100 for window in usage['windows']):
        raise Failure('rate_limited', 'The selected Subscription has a usage window used up. Wait before running the check.')
    return dict(usage)


def usage_change(before: Record, after: Record) -> list[Record]:
    changes = []
    for window in after['windows']:
        previous: Record = next((item for item in before['windows'] if item['kind'] == window['kind'] and item['label'] == window['label']), {})
        changes.append({'kind': window['kind'], 'label': window['label'], **({'beforePercent': previous['usedPercent']} if 'usedPercent' in previous else {}), **({'afterPercent': window['usedPercent']} if 'usedPercent' in window else {}), **({'changePercent': round(window['usedPercent'] - previous['usedPercent'], 3)} if 'usedPercent' in previous and 'usedPercent' in window else {})})
    return changes


async def live_check(build_only: bool, subscriptions: Subscriptions, inherited: Env, cancel: asyncio.Event) -> tuple[int, Record]:
    identifier = inherited.get('ARBOL_BUILD_LIVE_SUBSCRIPTION', 'anthropic')
    before = await check_account(subscriptions, identifier)
    tool = await inspect_tool(subscriptions.state.directory, subscriptions.settings.get('toolPath'), inherited, cancel)
    program = require_tool(tool)
    _, lifetime = await subscriptions.snapshot(identifier)
    await Authentication(subscriptions, inherited, cancel).require_native_login(identifier, lifetime, program=program)
    temp_root = subscriptions.state.directory / 'tmp'
    private_directory(temp_root)
    directory = Path(tempfile.mkdtemp(prefix='build-check-' if build_only else 'live-check-', dir=temp_root))
    env = {**clean_environment(inherited, inherited.get('HOME', ACTUAL_HOME)), 'PATH': str(Path(sys.executable).parent) + ':' + str(Path(program).parent) + ':/usr/bin:/bin', **({'ARBOL_TEST_TIME_SCALE': inherited['ARBOL_TEST_TIME_SCALE']} if inherited.get('ARBOL_TEST_TIME_SCALE') else {})}
    checks: list[Record] = []
    report: Record = {'check': 'dev.build-check' if build_only else 'dev.live-check', 'at': now(), 'toolVersion': tool['version'], 'subscriptionId': identifier, 'model': 'haiku', 'turnsAttempted': 0, 'inferenceRequests': 0, 'tokenUsage': {}, 'usageBefore': before, 'checks': checks}
    async def message(name: str, prompt: str, tools: bool = False, control: bool = False, stop: bool = False, chat: str | None = None, history: list[Record] | None = None) -> tuple[list[Record], str]:
        await check_account(subscriptions, identifier)
        request = directory / (str(uuid.uuid4()) + '.json')
        write_json(request, {'subscriptionId': identifier, 'model': 'haiku', 'tools': 'all' if tools else 'none', **({'workingDirectory': str(directory)} if tools or chat else {}), **({'chatSessionId': chat} if chat else {}), 'history': history or [], 'input': {'role': 'user', 'text': prompt}, 'limits': {'totalSeconds': 60}})
        child = await current_admission().spawn_helper_async(
            ['run', '--request', str(request), '--state-dir', str(subscriptions.state.directory),
             *(['--control'] if control or stop else [])],
            entry='bin/entry.py', environment=env, cwd=directory, limit=4 * 1024 * 1024,
        )
        assert child.stdin and child.stdout and child.stderr
        report['turnsAttempted'] += 1
        events: list[Record] = []
        stopped = started = False
        settled = False
        errors = asyncio.create_task(child.stderr.read())
        if not control and not stop:
            child.stdin.close()
        try:
            async with asyncio.timeout(80):
                while line := await child.stdout.readline():
                    record = loads(line)
                    events.append(record)
                    if record['type'] == 'started' and not started:
                        started = True
                        report['inferenceRequests'] += 1
                    if record['type'] == 'ask_opened' and not stop:
                        child.stdin.write((compact({'type': 'answer', 'askId': record['askId'], 'answer': {'type': 'answers', 'answers': {question['id']: [question['options'][0]['label']] for question in record['ask']['questions']}}}) + '\n').encode())
                    if stop and record['type'] == 'tool_started' and not stopped:
                        stopped = True
                        child.stdin.write(b'{"type":"stop"}\n')
                    if record['type'] == 'finished':
                        child.stdin.close()
                await child.wait()
            settled = True
            final = events[-1] if events else {}
            answer = ''.join(record['text'] for record in events if record['type'] == 'text' and not record.get('parentCallId'))
            if sum(record['type'] == 'finished' for record in events) != 1 or final.get('outcome') != ('cancelled' if stop else 'completed') or child.returncode != (2 if stop else 0):
                raise Failure('tool_failed', f'The {name} check did not finish as expected.')
            return events, answer
        finally:
            final = events[-1] if events else {}
            usage = final.get('usage', next((record['usage'] for record in reversed(events) if record['type'] == 'usage'), {}))
            previous_cost = report['tokenUsage'].get('costUsd', 0)
            report['tokenUsage'] = add_usage(report['tokenUsage'], usage)
            if 'costUsd' in usage:
                report['tokenUsage']['costUsd'] = previous_cost + usage['costUsd']
            checks.append({'check': name, 'outcome': final.get('outcome', 'failed') if settled else 'failed', 'exit': child.returncode, 'tokenUsage': usage, 'continuity': events[0].get('continuity') if events else None, 'eventTypes': [record['type'] for record in events], **({'error': final['error']} if final.get('error') else {})})
            kill_group(child.pid, signal.SIGKILL)
            try:
                await wait_exit(child, 2)
                await asyncio.wait_for(errors, 2)
            finally:
                close_pipes(child)
                errors.cancel()
                await asyncio.gather(errors, return_exceptions=True)
    status = 0
    error = None
    try:
        _, answer = await message('live.ready-usage', 'Answer with the single word: ready')
        if not re.search(r'\bready\b', answer, re.I):
            raise Failure('tool_failed', 'The ready answer check did not produce the expected behavior.')
        if not build_only:
            events, _ = await message('live.tools-questions', 'Use AskUserQuestion to ask Ready? with options Ready and Later. Then use Bash to run printf ready. Finish with ready.', tools=True, control=True)
            verify(any(event['type'] == 'ask_opened' for event in events) and any(event['type'] == 'tool_finished' and event['ok'] for event in events), 'The tools and questions check did not produce the expected behavior.')
            events, answer = await message('live.background', 'Start one Haiku sub-agent in the background. Tell it to run printf ready and return ready. Wait for it and finish with ready.', tools=True)
            verify(any(event.get('parentCallId') for event in events) and re.search(r'\bready\b', answer, re.I), 'The background sub-agent check did not produce the expected behavior.')
            chat, first_text = str(uuid.uuid4()), 'Remember the word willow. Answer only ready.'
            _, first = await message('live.continuity-fresh', first_text, chat=chat)
            events, answer = await message('live.continuity-resumed', 'What word did I give you? Answer only that word.', chat=chat, history=[{'type': 'message', 'role': 'user', 'text': first_text}, {'type': 'message', 'role': 'assistant', 'text': first}])
            verify(events[0].get('continuity') == 'resumed' and re.search(r'\bwillow\b', answer, re.I), 'The continuity check did not produce the expected behavior.')
            await message('live.stopping', 'Use Bash to run sleep 8 then printf ready.', tools=True, stop=True)
    except BaseException as failure:
        error, status = failure, 2 if cancel.is_set() else 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    after = (await subscriptions.get(identifier)).get('lastUsage', before)
    result = {**report, 'outcome': 'passed' if status == 0 else 'cancelled' if status == 2 else 'failed', 'usageAfter': after, 'usageChange': usage_change(before, after), **({'error': safe_failure(error)} if error else {})}
    write_json(subscriptions.state.directory / 'research' / (('build-check-' if build_only else 'live-check-') + report['at'].replace(':', '-') + '.json'), result)
    return status, result

# Archived complete imports; restore needed names in their original scope, not at this file tail.
