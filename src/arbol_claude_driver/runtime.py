"""Inference lifecycle: independent reads, interruption and cleanup before finalization."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path

from .continuity import Chats, Runs
from .core import Command, Env, Failure, Record, compact, fixed_wait, redact, safe_text, loads
from .history import MessageDigest, text_history
from .processes import ACTUAL_HOME, close_pipes, end_run_groups, inspect_tool, kill_group, owner_mcp_arguments, require_tool, run_launchers, spawn, wait_exit
from .protocol import RunEvents, RunProtocol, exit_failure
from .request import read_request, run_arguments, user_record
from .state import private_directory, process_owner
from .subscriptions import Subscriptions


async def run(command: Command, subscriptions: Subscriptions, events: RunEvents, env: Env, build: str, cancel: asyncio.Event, input_reader: asyncio.StreamReader) -> int:
    def identity(identifier: str, labels: Record | None) -> None:
        events.run_id, events.labels = identifier, labels
    request = (await read_request(command, subscriptions.settings, input_reader.readline, identity)).record()
    initial = await subscriptions.require_signed_in(request['subscriptionId'])
    tool = await inspect_tool(subscriptions.state.directory, subscriptions.settings.get('toolPath'), env, cancel)
    program = require_tool(tool)
    key = None
    runs, chats = Runs(subscriptions.state), Chats(subscriptions.state)
    home = subscriptions.state.subscription(initial['id']) / 'home'
    plan, record = await chats.reserve(request, runs, home)
    fingerprint = MessageDigest()
    for item in request['history']:
        fingerprint.add(item)
    fingerprint.add({'type': 'message', **request['input']})
    events.on_text = lambda text: fingerprint.add({'type': 'message', 'role': 'assistant', 'text': text})
    temp = subscriptions.state.directory / 'tmp' / request['runId']
    error: BaseException | None = None
    cancelled, settled = False, False
    last_usage = None
    active: RunProtocol | None = None
    stop_event = asyncio.Event()
    total_start = time.monotonic()

    async def controls() -> None:
        nonlocal cancelled, error
        try:
            while line := await input_reader.readline():
                try:
                    value = loads(line)
                except ValueError:
                    await events.emit({'type': 'control_refused', 'error': Failure('invalid_input', 'The control line must be JSON.').result})
                    continue
                if isinstance(value, dict) and value.get('type') == 'stop':
                    cancelled = True
                    stop_event.set()
                    return
                if isinstance(value, dict) and value.get('type') == 'answer' and isinstance(value.get('askId'), str) and value['askId']:
                    try:
                        if active is None:
                            raise Failure('not_found', 'That question is not open.')
                        if await active.answer(value['askId'], value.get('answer')):
                            cancelled = True
                            stop_event.set()
                            return
                    except Failure as failure:
                        if failure.result['code'] not in {'not_found', 'invalid_input'}:
                            error = error or failure
                            stop_event.set()
                            return
                        await events.emit({'type': 'control_refused', 'askId': value['askId'], 'error': failure.result})
                    continue
                await events.emit({'type': 'control_refused', 'error': Failure('invalid_input', 'This control line is not accepted.', secrets=events.secrets).result, **({'askId': value['askId']} if isinstance(value, dict) and isinstance(value.get('askId'), str) else {})})
            if not settled:
                cancelled = error is None
                stop_event.set()
        except asyncio.CancelledError:
            raise
        except BaseException:
            if not settled:
                cancelled = True
                stop_event.set()
    control_task = asyncio.create_task(controls()) if command.options.get('control') else None
    try:
        if cancel.is_set():
            cancelled = True
            raise Failure('tool_failed', 'The run was stopped.')
        private_directory(temp)
        cwd = request.get('workingDirectory', str(temp / 'work'))
        if request.get('workingDirectory') is None:
            private_directory(Path(cwd))
        child_env = await subscriptions.environment(initial['id'], env, key)
        owner_args = owner_mcp_arguments(env.get('HOME', ACTUAL_HOME)) if request['tools'] == 'all' else []
        launcher, shell = run_launchers(temp, env)
        child_env.update(CLAUDE_CODE_SHELL=str(shell), CLAUDE_CODE_DISABLE_AUTO_MEMORY='1', BASH_DEFAULT_TIMEOUT_MS='300000', BASH_MAX_TIMEOUT_MS='1800000', CLAUDE_CODE_MAX_RETRIES='3')
        if request['searchPath']:
            child_env['PATH'] = child_env.get('PATH', '') + os.pathsep + os.pathsep.join(request['searchPath'])
        chats.prepare(plan, request, home, cwd)
        for attempt in range(2):
            child = await spawn(str(launcher), [program, *run_arguments(request, plan['conversation'], command.options.get('control') is True, plan['resume']), *owner_args], child_env, cwd)
            assert child.stdin and child.stdout and child.stderr
            stderr = ''
            stop_deadline: float | None = None
            cleanup_done = False
            cleanup_remaining: list[int] = []
            interrupt_id: str | None = None
            prompt_sent = False
            resume_refused = False
            pending_init = None
            initialized, got_settings = False, False
            initialize_id, settings_id = str(uuid.uuid4()), str(uuid.uuid4())
            last_life = attempt_start = time.monotonic()
            life_started = False
            blocked = False
            life_changed = asyncio.Event()
            send_lock = asyncio.Lock()

            async def send(value: Record) -> None:
                assert child.stdin
                async with send_lock:
                    try:
                        child.stdin.write((compact(value) + '\n').encode())
                        await child.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        raise Failure('tool_failed', 'Claude input closed.') from None

            protocol = RunProtocol(events, request, build, tool['version'], initial['family'], send, plan, command.options.get('control') is True)
            active = protocol

            async def stderr_reader() -> None:
                nonlocal stderr
                assert child.stderr
                while chunk := await child.stderr.read(65536):
                    stderr = safe_text(stderr + chunk.decode('utf-8', 'replace'), events.secrets, 4096)

            async def monitor_clock() -> None:
                nonlocal error
                idle = request.get('limits', {}).get('idleSeconds')
                idle = fixed_wait(900000) if idle is None else idle
                while True:
                    life_changed.clear()
                    deadlines = []
                    if not life_started:
                        deadlines.append((attempt_start + fixed_wait(60000), 'start'))
                    if life_started and not blocked and idle > 0:
                        deadlines.append((last_life + idle, 'idle'))
                    if 'totalSeconds' in request.get('limits', {}):
                        deadlines.append((total_start + request['limits']['totalSeconds'], 'total'))
                    if not deadlines:
                        await life_changed.wait()
                        continue
                    deadline, limit = min(deadlines)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = error or Failure('limit_reached', f'The run reached its {limit} time limit.', {'detail': {'basis': 'fields', 'limit': limit}})
                        stop_event.set()
                        return
                    try:
                        await asyncio.wait_for(life_changed.wait(), remaining)
                    except TimeoutError:
                        continue

            async def stopper() -> None:
                nonlocal cancelled, interrupt_id, stop_deadline, cleanup_done, cleanup_remaining
                signal_wait = asyncio.create_task(cancel.wait())
                control_wait = asyncio.create_task(stop_event.wait())
                try:
                    await asyncio.wait([signal_wait, control_wait], return_when=asyncio.FIRST_COMPLETED)
                    cancelled |= cancel.is_set()
                    interrupt_id = str(uuid.uuid4())
                    stop_deadline = time.monotonic() + fixed_wait(15000)
                    grace_deadline = time.monotonic() + fixed_wait(2000)
                    events.output.stopping(stop_deadline)
                    try:
                        await asyncio.wait_for(send({'type': 'control_request', 'request_id': interrupt_id, 'request': {'subtype': 'interrupt'}}), max(.001, fixed_wait(2000)))
                    except (Failure, TimeoutError):
                        pass
                    await asyncio.sleep(max(0, grace_deadline - time.monotonic()))
                    kill_group(child.pid, signal.SIGKILL)
                    # Cleanup must progress while a contract emit waits for a
                    # slow caller. Finalization uses the same absolute bound.
                    try:
                        cleanup_remaining = await end_run_groups(temp, record.get('tool'), stop_deadline)
                        cleanup_done = True
                    finally:
                        close_pipes(child)
                finally:
                    signal_wait.cancel()
                    control_wait.cancel()
                    await asyncio.gather(signal_wait, control_wait, return_exceptions=True)

            errors_task = asyncio.create_task(stderr_reader())
            clock_task = asyncio.create_task(monitor_clock())
            stop_task = asyncio.create_task(stopper())
            def life() -> None:
                nonlocal last_life, life_started, blocked
                last_life = time.monotonic()
                life_started |= protocol.started or pending_init is not None
                blocked = protocol.background or bool(events.calls) or bool(events.asks)
                life_changed.set()
            def refusal(text: str) -> bool:
                return re.search('no conversation (?:was )?found|conversation.*not found|session.*not found', text, re.I) is not None
            try:
                owner = await process_owner(child.pid)
                if owner:
                    record['tool'] = owner
                    runs.save(record)
                await send({'type': 'control_request', 'request_id': initialize_id, 'request': {'subtype': 'initialize'}})
                while line := await child.stdout.readline():
                    life()
                    try:
                        value = loads(line)
                    except ValueError:
                        raise Failure('tool_unsupported', 'Claude returned invalid JSON.') from None
                    if isinstance(value, dict) and value.get('type') == 'control_response' and isinstance(value.get('response'), dict):
                        response = value['response']
                        if response.get('request_id') == interrupt_id and (protocol.result or not prompt_sent):
                            settled = True
                            child.stdin.close()
                        elif response.get('request_id') == initialize_id:
                            answer = response.get('response')
                            if initialized or response.get('subtype') != 'success' or not isinstance(answer, dict) or not isinstance(answer.get('models'), list):
                                raise Failure('tool_unsupported', 'Claude could not report its model catalog.')
                            initialized = True
                            model = next((item for item in answer['models'] if isinstance(item, dict) and (item.get('value') == request.get('model') or item.get('resolvedModel') == request.get('model'))), None)
                            if initial['family'] == 'anthropic' and request.get('model') and not model:
                                raise Failure('invalid_input', 'The requested model is not in the tool catalog.')
                            if initial['family'] == 'anthropic' and request.get('effort') and model and request['effort'] not in model.get('supportedEffortLevels', []):
                                raise Failure('invalid_input', 'The requested effort is not supported by the model.')
                            await send({'type': 'control_request', 'request_id': settings_id, 'request': {'subtype': 'get_settings'}})
                        elif response.get('request_id') == settings_id:
                            answer = response.get('response')
                            if got_settings or response.get('subtype') != 'success' or not isinstance(answer, dict) or not isinstance(answer.get('applied'), dict):
                                raise Failure('tool_unsupported', 'Claude could not report its applied settings.')
                            got_settings = True
                            if request.get('effort') and answer['applied'].get('effort') != request['effort']:
                                raise Failure('invalid_input', 'Claude did not apply the requested effort.')
                            if isinstance(answer['applied'].get('effort'), str):
                                request['effort'] = answer['applied']['effort']
                            if not cancelled and not cancel.is_set():
                                await send(user_record({**request, 'input': {'role': 'user', **text_history(request['history'], request['input'])}} if plan['textForm'] else request))
                                prompt_sent = True
                    else:
                        if plan['resume'] and not protocol.started and not events.produced_output and isinstance(value, dict) and value.get('type') == 'result' and value.get('is_error') is True and refusal('\n'.join(str(item) for item in [value.get('result'), *value.get('errors', [])])):
                            resume_refused = True
                            child.stdin.close()
                            break
                        if plan['resume'] and isinstance(value, dict) and value.get('type') == 'system' and value.get('subtype') == 'init':
                            pending_init = value
                        else:
                            if pending_init:
                                await protocol.accept(pending_init)
                                pending_init = None
                            await protocol.accept(value)
                    life()
                    if protocol.error and protocol.error.result['code'] == 'credential_rejected':
                        error = error or protocol.error
                        stop_event.set()
                        await subscriptions.run_report(initial, redact(protocol.error.result, events.secrets), redact(protocol.last_usage, events.secrets))
                    if isinstance(value, dict) and value.get('type') == 'result' and protocol.result and (not protocol.background or protocol.error):
                        settled = True
                        child.stdin.close()
                        break
                # Read the remaining stdout independently while waiting for child exit.
                async def drain_rest() -> None:
                    assert child.stdout
                    while await child.stdout.read(65536):
                        pass
                rest = asyncio.create_task(drain_rest())
                try:
                    await wait_exit(child, fixed_wait(2000) if settled or resume_refused else fixed_wait(15000))
                except TimeoutError:
                    kill_group(child.pid, signal.SIGKILL)
                    await wait_exit(child, max(1, fixed_wait(15000)))
                finally:
                    rest.cancel()
                    await asyncio.gather(rest, return_exceptions=True)
                if errors_task.done():
                    await errors_task
                resume_refused |= plan['resume'] and not protocol.started and not events.produced_output and refusal(stderr)
                if not resume_refused:
                    if protocol.error:
                        raise protocol.error
                    if protocol.background and not cancelled and not cancel.is_set():
                        raise Failure('tool_failed', 'Claude ended while background tasks were still running.')
                    if not protocol.result and not cancelled and not cancel.is_set():
                        raise exit_failure(stderr, child.returncode, tool['version'], events.secrets)
                    if not prompt_sent and not cancelled and not cancel.is_set():
                        raise Failure('tool_unsupported', 'Claude did not complete its settings handshake.')
            except BaseException as failure:
                kill_group(child.pid, signal.SIGKILL)
                await wait_exit(child, max(1, fixed_wait(15000)))
                if plan['resume'] and not protocol.started and not events.produced_output and refusal(stderr):
                    resume_refused = True
                else:
                    error = error or failure
            finally:
                clock_task.cancel()
                stop_task.cancel()
                errors_task.cancel()
                await asyncio.gather(clock_task, stop_task, errors_task, return_exceptions=True)
                if protocol.last_usage:
                    last_usage = protocol.last_usage
                active = None
                kill_group(child.pid, signal.SIGKILL)
                try:
                    remaining = cleanup_remaining if cleanup_done else await end_run_groups(temp, record.get('tool'), stop_deadline)
                    if remaining:
                        error = error or Failure('internal', 'Could not end process groups: ' + ', '.join(map(str, remaining)))
                        await events.emit({'type': 'notice', 'code': 'cleanup_incomplete', 'message': 'Could not end process groups: ' + ', '.join(map(str, remaining))})
                except BaseException as failure:
                    error = error or failure
                finally:
                    close_pipes(child)
                    try:
                        await asyncio.wait_for(child.wait(), max(.1, fixed_wait(500)))
                    except TimeoutError:
                        error = error or Failure('internal', 'The vendor subprocess did not close after cleanup.')
            if resume_refused and error is None and not cancelled and not cancel.is_set() and attempt == 0:
                plan.update(continuity='rebuilt', conversation=str(uuid.uuid4()), resume=False, textForm=True)
                plan.setdefault('rebuildReason', 'resume_refused')
                record['conversation'] = plan['conversation']
                record.pop('tool', None)
                runs.save(record)
                continue
            if resume_refused and error is None:
                error = Failure('tool_failed', 'Claude refused the conversation.')
            break
    except BaseException as failure:
        error = error or failure
    finally:
        settled = True
        if control_task:
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)
        if request.get('workingDirectory') is None and plan.get('syntheticFile'):
            Path(plan['syntheticFile']).unlink(missing_ok=True)
        shutil.rmtree(temp, ignore_errors=True)
    cancelled |= cancel.is_set()
    try:
        await subscriptions.run_report(initial, redact(error.result if isinstance(error, Failure) else None, events.secrets), redact(last_usage, events.secrets))
        await chats.settle(request, plan, fingerprint.snapshot(), error is None and not cancelled, home)
    except BaseException as failure:
        error = error or failure
        await chats.settle(request, plan, fingerprint.snapshot(), False, home)
    events.on_text = None
    try:
        return await events.finish(error, cancelled)
    except BaseException:
        await chats.settle(request, {**plan, 'continuedConversation': plan['conversation']}, fingerprint.snapshot(), False, home)
        return 2 if cancel.is_set() else 1
    finally:
        runs.remove(request['runId'])
