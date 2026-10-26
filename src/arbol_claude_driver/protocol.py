"""Claude bidirectional stream JSON translated into ordered contract events."""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .core import Failure, Record, compact, epoch, fixed_wait, now, number, redact, safe_failure, utf8
from .processes import OLDEST_SUPPORTED
from .models import EventEnvelope


class Output:
    def __init__(self, cancel: asyncio.Event, fd: int = 1):
        self.fd, self.cancel = fd, cancel
        self.lock = asyncio.Lock()
        self.stop_deadline: float | None = None
        self.stop_output = asyncio.Event()
        os.set_blocking(fd, False)

    def stopping(self, deadline: float | None = None) -> None:
        limit = time.monotonic() + fixed_wait(15000) if deadline is None else deadline
        self.stop_deadline = limit if self.stop_deadline is None else min(self.stop_deadline, limit)
        self.stop_output.set()

    async def write(self, text: str) -> None:
        data = utf8(text)
        loop = asyncio.get_running_loop()
        async with self.lock:
            position = 0
            while position < len(data):
                try:
                    position += os.write(self.fd, data[position:])
                except BlockingIOError:
                    if self.cancel.is_set():
                        self.stopping()
                    ready = loop.create_future()
                    def wake() -> None:
                        if not ready.done():
                            ready.set_result(None)
                    loop.add_writer(self.fd, wake)
                    cancelled = asyncio.create_task(self.cancel.wait())
                    stopping = asyncio.create_task(self.stop_output.wait())
                    try:
                        remaining = None if self.stop_deadline is None else max(0, self.stop_deadline - time.monotonic())
                        if self.stop_deadline is None:
                            done, _ = await asyncio.wait([ready, cancelled, stopping], return_when=asyncio.FIRST_COMPLETED)
                            if cancelled in done:
                                self.stopping()
                        else:
                            await asyncio.wait_for(ready, remaining)
                    except TimeoutError:
                        self.cancel.set()
                        raise BrokenPipeError('Caller output did not accept data within cleanup deadline.') from None
                    finally:
                        loop.remove_writer(self.fd)
                        cancelled.cancel()
                        stopping.cancel()
                        await asyncio.gather(cancelled, stopping, return_exceptions=True)
                except (BrokenPipeError, OSError):
                    self.cancel.set()
                    raise

    async def json(self, value: Any) -> None:
        await self.write(compact(value) + '\n')


class RunEvents:
    def __init__(self, output: Output, text_only: bool = False):
        self.output, self.text_only = output, text_only
        self.run_id = str(uuid.uuid4())
        self.labels: Record | None = None
        self.seq, self.status = 0, 1
        self.finished = False
        self.produced_output = False
        self.usage: Record = {}
        self.calls: dict[str, Record] = {}
        self.asks: dict[str, None] = {}
        self.secrets: list[str] = []
        self.on_text: Callable[[str], None] | None = None

    async def emit(self, event: Record) -> None:
        if self.finished:
            return
        event = redact(event, self.secrets)
        self.seq += 1
        record = redact(EventEnvelope(self.run_id, self.seq, now(), self.labels, event).record(), self.secrets)
        if (event['type'] in {'text', 'text_delta', 'thinking', 'thinking_delta'} and event.get('text')) or event['type'] == 'tool_started':
            self.produced_output = True
        if event['type'] == 'text' and not event.get('parentCallId') and self.on_text:
            self.on_text(event['text'])
        if event['type'] == 'finished':
            self.finished = True
        if self.text_only:
            if event['type'] == 'text' and not event.get('parentCallId'):
                await self.output.write(str(event['text']))
        else:
            await self.output.json(record)

    async def finish(self, error: BaseException | None = None, cancelled: bool = False) -> int:
        self.output.stopping()
        if self.finished:
            return 2 if cancelled else self.status
        unfinished = list(self.calls)
        for identifier in self.asks:
            await self.emit({'type': 'ask_closed', 'askId': identifier, 'reason': 'withdrawn'})
        self.asks.clear()
        for identifier, call in self.calls.items():
            await self.emit({'type': 'tool_finished', 'callId': identifier, 'ok': False, 'error': 'The run ended before the tool call finished.', 'interrupted': True, **({'parentCallId': call['parentCallId']} if call.get('parentCallId') else {})})
        self.calls.clear()
        failure = safe_failure(error) if error is not None and not cancelled else None
        self.status = 2 if cancelled else 1 if failure else 0
        await self.emit({'type': 'finished', 'outcome': 'cancelled' if cancelled else 'failed' if failure else 'completed', **({'error': failure} if failure else {}), **({'usage': self.usage} if self.usage else {}), 'producedOutput': self.produced_output, 'unfinishedCalls': unfinished})
        return self.status


def truncate_result(result: Any) -> Record:
    text = result if isinstance(result, str) else compact(result)
    data = utf8(text)
    return {'result': result} if len(data) <= 200 * 1024 else {'result': data[:200 * 1024].decode('utf-8', 'ignore'), 'truncated': True}


def token_usage(value: Any) -> Record:
    if not isinstance(value, dict):
        return {}
    result: Record = {}
    inputs = [value[field] for field in ['input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'] if number(value.get(field)) and value[field] >= 0]
    if inputs:
        result['inputTokens'] = sum(inputs)
    for raw, field in [('cache_read_input_tokens', 'cachedInputTokens'), ('output_tokens', 'outputTokens')]:
        if number(value.get(raw)) and value[raw] >= 0:
            result[field] = value[raw]
    thinking = value.get('output_tokens_details', {}).get('thinking_tokens') if isinstance(value.get('output_tokens_details'), dict) else None
    if number(thinking) and thinking >= 0:
        result['reasoningTokens'] = thinking
    return result


def add_usage(a: Record, b: Record) -> Record:
    total = {**a, **({'model': b['model']} if b.get('model') else {})}
    for field in ['inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens']:
        if field in b:
            total[field] = a.get(field, 0) + b[field]
    return total


def protocol_failure() -> Failure:
    return Failure('tool_unsupported', 'Claude returned a malformed protocol record.')


def retry_wait(text: str) -> float | None:
    match = re.search(r'\b(retry after|retry in|try again in|wait)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s\b|minutes?|mins?|m\b)?', text, re.I)
    if not match or (not match[3] and match[1].lower() != 'retry after') or (not match[3] and re.match(r'\s*[a-z]', text[match.end():], re.I)):
        return None
    return float(match[2]) * (60 if (match[3] or '').lower().startswith('m') else 1)


def limit_report(record: Record) -> Record | None:
    # Both windows count tokens (CLD-D23, DRV-1.27). The stream carries no other window: `unifiedWindows` held
    # `five_hour` and `seven_day` only when probed on 26 October 2026 (SGM-D26); an unknown key is left unmapped.
    info = record.get('rate_limit_info')
    if not isinstance(info, dict) or not isinstance(info.get('unifiedWindows'), dict):
        return None
    windows = []
    for key, kind, minutes, label in [('five_hour', 'session', 300, 'Five hours'), ('seven_day', 'weekly', 10080, 'Seven days')]:
        raw = info['unifiedWindows'].get(key)
        if not isinstance(raw, dict):
            continue
        window = {'kind': kind, 'label': label, 'resource': 'tokens', 'windowMinutes': minutes}
        utilization = raw.get('utilization')
        if number(utilization) and utilization >= 0:
            window.update(usedPercent=utilization * 100, limitReached=utilization >= 1)
        if number(raw.get('resetsAt')):
            try:
                window['resetsAt'] = now(raw['resetsAt'])
            except (OverflowError, ValueError):
                pass
        windows.append(window)
    extra = {key: info[key] for key in ['status', 'rateLimitType'] if isinstance(info.get(key), str)}
    if type(info.get('isUsingOverage')) is bool:
        extra['isUsingOverage'] = info['isUsingOverage']
    return {'outcome': 'ok', 'fetchedAt': now(), 'windows': windows, 'extra': extra}


def run_failure(result: Record | None, assistant: Record | None, family: str, version: str, secrets: list[str], usage: Record | None = None) -> Failure:
    result, assistant = result or {}, assistant or {}
    content = assistant.get('message', {}).get('content', []) if isinstance(assistant.get('message'), dict) else []
    message = '\n'.join(block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)) or '\n'.join(value for value in result.get('errors', []) if isinstance(value, str)) or (result.get('result') if isinstance(result.get('result'), str) else '') or 'Claude reported an error.'
    vendor_code = assistant.get('error') if isinstance(assistant.get('error'), str) and assistant['error'] else None
    status = result.get('api_error_status') if type(result.get('api_error_status')) is int else None
    code, basis = 'vendor_error', 'fields'
    if vendor_code == 'authentication_failed' or status in {401, 403}:
        code = 'credential_rejected'
    elif vendor_code == 'model_not_found' or status == 404:
        code = 'invalid_input'
    elif vendor_code and re.search('rate.?limit', vendor_code, re.I) or status == 429:
        code = 'rate_limited'
    elif status is not None and status >= 500 or vendor_code == 'server_error':
        code = 'vendor_unavailable'
    elif status == 400 and re.search(r'(?:prompt|conversation|context).*(?:too long|too large|exceed)|(?:too long|too large).*(?:prompt|conversation|context)', message, re.I):
        code = 'context_exceeded'
    elif vendor_code or status is not None and 400 <= status < 500:
        code = 'vendor_error'
    elif result.get('subtype') == 'error_max_turns' or result.get('terminal_reason') == 'max_turns':
        code = 'limit_reached'
    elif status is None:
        basis = 'wording'
        if re.search('rate limit|rate_limit|too many requests|quota exceeded|usage limit|overloaded', message, re.I):
            code = 'rate_limited'
        elif re.search('failed to authenticate|authentication_error|invalid authentication', message, re.I):
            code = 'credential_rejected'
    wait = retry_wait(message) if code == 'rate_limited' else None
    if code == 'rate_limited' and wait is None and family == 'anthropic' and usage:
        kind = {'five_hour': 'session', 'seven_day': 'weekly'}.get(usage.get('extra', {}).get('rateLimitType'))
        reset = next((window.get('resetsAt') for window in usage['windows'] if kind and window['kind'] == kind), None)
        if reset:
            wait = max(0, epoch(reset) - time.time())
    detail = {'basis': basis, 'toolVersion': version, **({'vendorCode': vendor_code} if vendor_code else {}), **({'vendorStatus': status} if status is not None else {})}
    remedy = 'Sign in again.' if code == 'credential_rejected' else 'Choose a model from the catalog.' if code == 'invalid_input' else 'Start a new chat.' if code == 'context_exceeded' else None
    return Failure(code, message, {'detail': detail, **({'retryAfterSeconds': wait} if wait is not None else {}), **({'remedy': remedy} if remedy else {})}, secrets)


def exit_failure(stderr: str, code: int | None, version: str, secrets: list[str], missing: bool = False) -> Failure:
    missing |= code == 127 and re.search(r'exec:.*(?:not found|no such file)', stderr, re.I) is not None
    unsupported = re.search('unknown (?:option|argument)|unrecognized (?:option|argument)|unexpected argument', stderr, re.I) is not None
    return Failure('tool_missing' if missing else 'tool_unsupported' if unsupported else 'tool_failed', f'claude was not found; version not found, needed {OLDEST_SUPPORTED}.' if missing else 'Claude ended without a result' + (': ' + stderr if stderr else '.'), {'detail': {'basis': 'wording' if unsupported else 'fields', 'toolVersion': version, **({'neededVersion': OLDEST_SUPPORTED} if missing or unsupported else {}), **({'toolExit': code} if code is not None and code >= 0 else {}), **({'excerpt': stderr} if stderr else {})}, **({'remedy': 'Update claude.' if unsupported else 'Install claude.'} if missing or unsupported else {})}, secrets)


class RunProtocol:
    def __init__(self, events: RunEvents, request: Record, build: str, version: str, family: str, send: Callable[[Record], Awaitable[None]], continuity: Record, control: bool):
        self.events, self.request, self.build, self.version, self.family, self.send, self.continuity, self.control = events, request, build, version, family, send, continuity, control
        self.started, self.background = False, False
        self.result: Record | None = None
        self.error: Failure | None = None
        self.last_usage: Record | None = None
        self.assistant_error: Record | None = None
        self.messages: dict[str, Record] = {}
        self.final_usage: Record = {}
        self.questions: dict[str, Record] = {}
        self.answered: set[str] = set()

    async def answer(self, identifier: str, value: Any) -> bool:
        if identifier in self.answered:
            return False
        ask = self.questions.get(identifier)
        if ask is None:
            raise Failure('not_found', 'That question is not open.')
        def bad() -> None:
            raise Failure('invalid_input', 'The answer must choose the offered labels for every question.')
        if not isinstance(value, dict):
            bad()
        decline = value.get('type') == 'decline'
        if decline:
            response = {'behavior': 'deny', 'message': 'The caller declined the question.', 'interrupt': True}
        else:
            if value.get('type') != 'answers' or not isinstance(value.get('answers'), dict) or len(value['answers']) != len(ask['questions']):
                bad()
            converted = {}
            for question in ask['questions']:
                labels = value['answers'].get(question['id'])
                if not isinstance(labels, list) or not labels or not all(isinstance(label, str) for label in labels) or (not question['multiple'] and len(labels) != 1) or len(set(labels)) != len(labels) or not all(any(option['label'] == label for option in question['options']) for label in labels):
                    bad()
                converted[question['id']] = ', '.join(labels)
            response = {'behavior': 'allow', 'updatedInput': {**ask['input'], 'answers': converted}}
        del self.questions[identifier]
        self.answered.add(identifier)
        await self.send({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': identifier, 'response': response}})
        if identifier in self.events.asks:
            del self.events.asks[identifier]
            await self.events.emit({'type': 'ask_closed', 'askId': identifier, 'reason': 'answered'})
        return bool(decline)

    async def accept(self, record: Any) -> None:
        if not isinstance(record, dict) or not isinstance(record.get('type'), str):
            raise protocol_failure()
        parent = {'parentCallId': record['parent_tool_use_id']} if isinstance(record.get('parent_tool_use_id'), str) and record['parent_tool_use_id'] else {}
        async def emit(event: Record) -> None:
            await self.events.emit({**event, **parent})
        kind = record['type']
        if kind == 'system':
            subtype = record.get('subtype')
            if subtype == 'init' and not self.started:
                self.started = True
                await emit({'type': 'started', 'continuity': self.continuity['continuity'], **({'rebuildReason': self.continuity['rebuildReason']} if self.continuity.get('rebuildReason') else {}), 'subscriptionId': self.request['subscriptionId'], **({'model': record['model']} if isinstance(record.get('model'), str) else {}), **({'effort': self.request['effort']} if self.request.get('effort') else {}), 'toolVersion': record.get('claude_code_version', self.version), 'driverBuild': self.build})
                if self.continuity.get('textForm'):
                    await emit({'type': 'notice', 'code': 'history_as_text', 'message': 'The earlier conversation was sent as labelled text because no native session could be used.'})
            elif subtype == 'api_retry':
                await emit({'type': 'notice', 'code': 'api_retry', 'message': f'Claude is retrying (attempt {record.get("attempt", "unknown")} of {record.get("max_retries", "unknown")}, delay {record.get("retry_delay_ms", "unknown")} ms' + (f', vendor status {record["error_status"]}' if number(record.get('error_status')) else '') + ').'})
                if record.get('error_status') in {401, 403} or record.get('error') == 'authentication_failed':
                    self.error = run_failure({'api_error_status': record.get('error_status'), 'result': 'Claude reports a rejected credential while retrying.'}, {'error': record.get('error')}, self.family, self.version, self.events.secrets, self.last_usage)
            elif subtype == 'background_tasks_changed':
                if not isinstance(record.get('tasks'), list):
                    raise protocol_failure()
                self.background = bool(record['tasks'])
        elif kind == 'stream_event':
            event = record.get('event')
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise protocol_failure()
            key = parent.get('parentCallId', 'main')
            subtype = event['type']
            if subtype == 'content_block_delta':
                delta = event.get('delta')
                if not isinstance(delta, dict):
                    raise protocol_failure()
                if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                    await emit({'type': 'text_delta', 'text': delta['text']})
                elif delta.get('type') == 'thinking_delta' and isinstance(delta.get('thinking'), str) and delta['thinking']:
                    await emit({'type': 'thinking_delta', 'text': delta['thinking']})
            elif subtype == 'message_start':
                message = event.get('message')
                if not isinstance(message, dict):
                    raise protocol_failure()
                self.messages[key] = {**token_usage(message.get('usage')), **({'model': message['model']} if isinstance(message.get('model'), str) else {})}
            elif subtype == 'message_delta':
                self.messages[key] = {**self.messages.get(key, {}), **token_usage(event.get('usage'))}
            elif subtype == 'message_stop' and key in self.messages:
                self.events.usage = add_usage(self.events.usage, self.messages.pop(key))
                await emit({'type': 'usage', 'usage': self.events.usage})
        elif kind in {'assistant', 'user'}:
            message = record.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), list):
                raise protocol_failure()
            if kind == 'assistant' and record.get('error'):
                self.assistant_error = record
                self.error = run_failure(None, record, self.family, self.version, self.events.secrets, self.last_usage)
                return
            for block in message['content']:
                if not isinstance(block, dict):
                    if kind == 'assistant':
                        raise protocol_failure()
                    continue
                if kind == 'assistant' and block.get('type') in {'text', 'thinking'}:
                    text = block.get('text' if block['type'] == 'text' else 'thinking')
                    if not isinstance(text, str):
                        raise protocol_failure()
                    if text:
                        await emit({'type': block['type'], 'text': text})
                elif kind == 'assistant' and block.get('type') == 'tool_use':
                    if not isinstance(block.get('id'), str) or not block['id'] or not isinstance(block.get('name'), str) or 'input' not in block or block['id'] in self.events.calls:
                        raise protocol_failure()
                    self.events.calls[block['id']] = {'name': block['name'], **parent}
                    argument = block['input']
                    if block['name'] == 'Bash':
                        argument = {'command': argument.get('command') if isinstance(argument, dict) else None, **({'cwd': argument['cwd']} if isinstance(argument, dict) and isinstance(argument.get('cwd'), str) else {})}
                    await emit({'type': 'tool_started', 'callId': block['id'], 'name': 'shell' if block['name'] == 'Bash' else block['name'], 'input': argument})
                elif kind == 'user' and block.get('type') == 'tool_result':
                    if not isinstance(block.get('tool_use_id'), str) or block['tool_use_id'] not in self.events.calls:
                        raise protocol_failure()
                    call = self.events.calls.pop(block['tool_use_id'])
                    content = block.get('content')
                    result = {'output': content if isinstance(content, str) else compact(content)} if call['name'] == 'Bash' else content
                    reason = truncate_result(content)['result']
                    await self.events.emit({'type': 'tool_finished', 'callId': block['tool_use_id'], 'ok': not bool(block.get('is_error')), **truncate_result(result), **({'error': reason if isinstance(reason, str) else compact(reason)} if block.get('is_error') else {}), **({'parentCallId': call['parentCallId']} if call.get('parentCallId') else parent)})
        elif kind == 'control_request':
            identifier, request = record.get('request_id'), record.get('request')
            if not isinstance(identifier, str) or not identifier or not isinstance(request, dict):
                raise protocol_failure()
            if self.control and request.get('subtype') == 'can_use_tool' and request.get('tool_name') == 'AskUserQuestion':
                value = request.get('input')
                question_call = self.events.calls.get(str(request.get('tool_use_id', '')))
                if not isinstance(value, dict) or not isinstance(value.get('questions'), list) or not value['questions'] or not question_call or question_call['name'] != 'AskUserQuestion' or identifier in self.events.asks or identifier in self.answered:
                    raise protocol_failure()
                questions = []
                for question in value['questions']:
                    if not isinstance(question, dict) or not isinstance(question.get('question'), str) or not question['question'] or not isinstance(question.get('options'), list) or not question['options'] or not all(isinstance(option, dict) and isinstance(option.get('label'), str) and option['label'] for option in question['options']) or len({option['label'] for option in question['options']}) != len(question['options']):
                        raise protocol_failure()
                    questions.append({'id': question['question'], 'question': question['question'], **({'header': question['header']} if isinstance(question.get('header'), str) else {}), 'multiple': question.get('multiSelect') is True, 'options': question['options']})
                if len({question['id'] for question in questions}) != len(questions):
                    raise protocol_failure()
                self.questions[identifier] = {'input': value, 'questions': questions}
                self.events.asks[identifier] = None
                await self.events.emit({'type': 'ask_opened', 'askId': identifier, 'ask': {'callId': request['tool_use_id'], 'questions': questions}})
            elif request.get('subtype') == 'can_use_tool':
                question = request.get('tool_name') == 'AskUserQuestion'
                allow = not question and isinstance(request.get('tool_name'), str) and bool(request['tool_name']) and isinstance(request.get('input'), dict)
                message = 'Nobody is there to answer questions in this run.' if question else 'An unexpected permission request was approved.' if allow else 'An unusable permission request was declined.'
                await self.send({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': identifier, 'response': {'behavior': 'allow', 'updatedInput': request['input']} if allow else {'behavior': 'deny', 'message': 'Nobody is there to answer questions in this run.' if question else 'The permission request has no usable tool name or input.'}}})
                await emit({'type': 'notice', 'code': 'question_unavailable' if question else 'permission_answered', 'message': message})
            else:
                await self.send({'type': 'control_response', 'response': {'subtype': 'error', 'request_id': identifier, 'error': 'Unknown control request.'}})
        elif kind == 'result':
            if type(record.get('is_error')) is not bool:
                raise protocol_failure()
            self.result = record
            self.final_usage = add_usage(self.final_usage, token_usage(record.get('usage')))
            models = record.get('modelUsage') if isinstance(record.get('modelUsage'), dict) else {}
            if models:
                self.final_usage['model'] = list(models)[-1]
            if self.family == 'anthropic' and number(record.get('total_cost_usd')) and record['total_cost_usd'] >= 0 and models and all(isinstance(model, dict) and model.get('costBasis') == 'list' for model in models.values()):
                self.final_usage['costUsd'] = record['total_cost_usd']
            else:
                self.final_usage.pop('costUsd', None)
            self.events.usage = dict(self.final_usage)
            await emit({'type': 'usage', 'usage': self.events.usage})
            self.error = run_failure(record, self.assistant_error, self.family, self.version, self.events.secrets, self.last_usage) if record['is_error'] else None
        elif kind == 'control_cancel_request':
            identifier = str(record.get('request_id', ''))
            self.questions.pop(identifier, None)
            if identifier in self.events.asks:
                del self.events.asks[identifier]
                await self.events.emit({'type': 'ask_closed', 'askId': identifier, 'reason': 'withdrawn'})
        elif kind == 'rate_limit_event':
            if self.family == 'anthropic':
                self.last_usage = limit_report(record) or self.last_usage
        elif kind != 'control_response':
            raise protocol_failure()
