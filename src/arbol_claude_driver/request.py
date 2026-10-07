"""Validate caller JSON before inference and preserve option replacement rules."""
from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Never, Literal, cast

from .core import Command, Failure, Record, number, loads
from .history import image_blocks
from .models import ValidatedRequest


async def read_request(command: Command, settings: Record, first_line: Callable[[], Awaitable[bytes]], identity: Callable[[str, Any], None]) -> ValidatedRequest:
    options = command.options
    file = options.get('request')
    value: Record = {}
    def bad(message: str) -> Never:
        raise Failure('invalid_input', message)
    if isinstance(file, str):
        try:
            raw = loads(await first_line() if file == '-' else Path(file).read_text())
        except (OSError, ValueError):
            bad('The request could not be read as JSON.')
            raw = None
        if not isinstance(raw, dict):
            bad('The request must be a JSON object.')
        value = dict(raw)
    for option, field in {'subscription': 'subscriptionId', 'model': 'model', 'effort': 'effort', 'service-tier': 'serviceTier', 'cwd': 'workingDirectory', 'tools': 'tools', 'chat-session': 'chatSessionId', 'run-id': 'runId'}.items():
        if option in options:
            value[field] = options[option]
    if 'system-prompt-file' in options:
        try:
            value['systemPrompt'] = Path(str(options['system-prompt-file'])).read_text()
        except OSError:
            bad('The system prompt file could not be read.')
    if command.positionals:
        value['input'] = {**(value['input'] if isinstance(value.get('input'), dict) else {}), 'role': 'user', 'text': command.positionals[0]}
    if 'add-dir' in options:
        value['additionalDirectories'] = options['add-dir']
    if 'label' in options:
        labels = dict(value['labels'] if isinstance(value.get('labels'), dict) else {})
        items = options['label']
        assert isinstance(items, list)
        for item in items:
            key, equal, text = item.partition('=')
            if not key or not equal:
                bad('A label must be key=value.')
            labels[key] = text
        value['labels'] = labels
    for field in ['runId', 'subscriptionId', 'model', 'effort', 'workingDirectory', 'chatSessionId', 'serviceTier']:
        if field in value and (not isinstance(value[field], str) or not value[field]):
            bad(f'Invalid request field {field}.')
    if 'systemPrompt' in value and not isinstance(value['systemPrompt'], str):
        bad('Invalid systemPrompt.')
    if 'serviceTier' in value:
        bad('Claude does not offer a service tier setting.')
    subscription = value.get('subscriptionId', settings.get('defaultSubscription', 'anthropic'))
    if not isinstance(subscription, str) or not subscription:
        bad('Invalid subscription identifier.')
    defaults = settings.get('subscriptions', {}).get(subscription, {})
    identifier = value.get('runId', str(uuid.uuid4()))
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', identifier):
        bad('The run identifier is not a safe file name.')
    if 'labels' in value and (not isinstance(value['labels'], dict) or not all(isinstance(item, str) for item in value['labels'].values())):
        bad('Labels must be a string mapping.')
    identity(identifier, value.get('labels'))
    tools = value.get('tools', 'none' if file is None else None)
    if tools not in ['all', 'none']:
        bad('The request must name tools as all or none.')
    history = value.get('history', [] if file is None else None)
    if not isinstance(history, list):
        bad('The request must contain a history array.')
    def images_valid(images: Any) -> bool:
        return images is None or isinstance(images, list) and all(isinstance(image, dict) and image.get('mediaType') in ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] and isinstance(image.get('data'), str) and len(image['data']) > 0 and len(image['data']) % 4 == 0 and re.fullmatch('[A-Za-z0-9+/]*={0,2}', image['data']) is not None for image in images)
    for item in history:
        if not isinstance(item, dict):
            bad('Each history item must be an object.')
        if item.get('type') == 'message':
            if item.get('role') not in ['user', 'assistant'] or not isinstance(item.get('text'), str) or not images_valid(item.get('images')) or (item['role'] == 'assistant' and 'images' in item):
                bad('Invalid history message.')
        elif item.get('type') == 'tool_call':
            if not isinstance(item.get('name'), str) or not item['name'] or 'input' not in item or type(item.get('ok')) is not bool or ('error' in item and not isinstance(item['error'], str)):
                bad('Invalid history tool call.')
            if item['name'] == 'shell' and (not isinstance(item['input'], dict) or not isinstance(item['input'].get('command'), str)):
                bad('A shell history call needs a command.')
        else:
            bad('Unknown history item type.')
    message = value.get('input')
    if not isinstance(message, dict) or message.get('role') != 'user' or not isinstance(message.get('text'), str):
        bad('The input must be a user message with text.')
    if not images_valid(message.get('images')):
        bad('Images must have supported media types and base64 data.')
    for field in ['additionalDirectories', 'searchPath']:
        paths = value.get(field, [])
        if not isinstance(paths, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
            bad(f'{field} must contain absolute paths.')
        value[field] = paths
    cwd = value.get('workingDirectory')
    if cwd is None and tools == 'all':
        bad('A run with tools needs a working directory.')
    for path in ([cwd] if cwd is not None else []) + value['additionalDirectories']:
        if not Path(path).is_absolute() or not Path(path).is_dir():
            bad('A working or additional directory does not exist.')
    if cwd is not None:
        value['workingDirectory'] = str(Path(cwd).resolve())
    if 'limits' in value and (not isinstance(value['limits'], dict) or any(key not in {'idleSeconds', 'totalSeconds'} or not number(item) or item < 0 for key, item in value['limits'].items())):
        bad('Invalid run limits.')
    fields = {**value, **({'model': defaults['defaultModel']} if 'model' not in value and 'defaultModel' in defaults else {}), **({'effort': defaults['defaultEffort']} if 'effort' not in value and 'defaultEffort' in defaults else {})}
    return ValidatedRequest(identifier, subscription, cast(Literal['all', 'none'], tools), history, message, fields)


def run_arguments(request: Record, conversation: str, control: bool, resume: bool = False) -> list[str]:
    args = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--permission-mode', 'bypassPermissions', '--system-prompt-snapshot', 'off', '--resume' if resume else '--session-id', conversation]
    if request['tools'] == 'none':
        args += ['--tools', '']
    elif control:
        args += ['--permission-prompt-tool', 'stdio']
    if request.get('workingDirectory') is None:
        args.append('--no-session-persistence')
    for option, field in [('--model', 'model'), ('--effort', 'effort'), ('--append-system-prompt', 'systemPrompt')]:
        if field in request:
            args += [option, request[field]]
    for directory in request['additionalDirectories']:
        args += ['--add-dir', directory]
    return args


def user_record(request: Record) -> Record:
    message = request['input']
    return {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': message['text']}, *image_blocks(message.get('images'))]}}
