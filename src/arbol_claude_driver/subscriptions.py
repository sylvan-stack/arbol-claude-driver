"""Independent Subscription records, vendor login and secret-source operations."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .core import Env, Failure, Record, clean_environment, compact, epoch, fixed_wait, now, number, safe_failure, safe_text, valid_date, loads, retired_route
from .processes import ACTUAL_HOME, child_command, inspect_tool, kill_group, owner_skills, python_launcher, require_tool, spawn
from .state import State, owner_alive, parse_owner, private_directory, process_owner, read_json, write_json

FAMILIES: list[Record] = [
    {'id': 'anthropic', 'label': 'Anthropic', 'auth': 'vendor_login', 'settings': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


def family_id(value: Any) -> str:
    if value != 'anthropic':
        raise retired_route()
    return 'anthropic'


def family_settings(family: str, value: Record, prefix: str, notices: list[Record]) -> Record:
    family_id(family)
    for key in value:
        if key in {'baseUrl', 'modelMap', 'toolEnvironment'}:
            raise retired_route()
        raise Failure('invalid_input', f'Unknown setting {prefix}{key}.')
    return {}


class Subscriptions:
    def __init__(self, state: State, settings: Record, notices: list[Record], settings_file: Path):
        self.state, self.settings, self.notices, self.settings_file = state, settings, notices, settings_file
        self.resolved: dict[str, Record] = {}

    def defaults(self, identifier: str) -> Record:
        return dict(self.settings.get('subscriptions', {}).get(identifier, {}))

    async def record(self, identifier: str) -> Record:
        if identifier in {'zai', 'claude-zai'}:
            raise retired_route()
        value = read_json(self.state.subscription(identifier) / 'record.json')
        if isinstance(value, dict) and value.get('family') == 'zai':
            raise retired_route()
        if value is None:
            if identifier != 'anthropic':
                raise Failure('not_found', 'Subscription was not found.')
            info = (self.state.directory / 'format').stat()
            timestamp = now(getattr(info, 'st_birthtime', info.st_mtime))
            return {'id': identifier, 'family': identifier, 'label': 'Anthropic' if identifier == 'anthropic' else 'Z.AI', 'auth': 'vendor_login' if identifier == 'anthropic' else 'key', 'state': 'signed_out', 'createdAt': timestamp, 'updatedAt': timestamp}
        allowed = {'id', 'family', 'label', 'auth', 'state', 'account', 'problem', 'lastUsage', 'createdAt', 'updatedAt', 'checkedAt'}
        bad = not isinstance(value, dict)
        if not bad:
            bad = value.get('id') != identifier or value.get('family') != 'anthropic' or not isinstance(value.get('label'), str) or not value['label'] or value.get('state') not in {'signed_out', 'signed_in', 'needs_attention'} or value.get('auth') != ('vendor_login' if value.get('family') == 'anthropic' else 'key') or not all(valid_date(value.get(field)) for field in ['createdAt', 'updatedAt']) or bool(set(value) - allowed) or ((value['state'] == 'needs_attention') != ('problem' in value)) or ('checkedAt' in value and not valid_date(value['checkedAt']))
            account = value.get('account')
            bad |= account is not None and (not isinstance(account, dict) or any(key not in {'name', 'email', 'organization', 'plan'} or not isinstance(item, str) for key, item in account.items()))
        if bad:
            raise Failure('internal', f'Malformed Subscription record for {identifier}.')
        return dict(value)

    async def get(self, identifier: str) -> Record:
        record = await self.record(identifier)
        if identifier not in self.resolved:
            self.resolved[identifier] = family_settings(record['family'], self.defaults(identifier).get('settings', {}), f'{self.settings_file}: subscriptions.{identifier}.settings.', self.notices)
        result = {**record, 'settings': self.resolved[identifier]}
        progress = read_json(self.state.subscription(identifier) / 'sign-in.json')
        if progress is not None:
            if not isinstance(progress, dict) or not valid_date(progress.get('expiresAt')):
                raise Failure('internal', 'Malformed sign-in record.')
            if progress.get('operation', 'sign_in') == 'sign_in' and await owner_alive(parse_owner(progress.get('owner'))):
                result['state'] = 'signing_in'
                result.pop('problem', None)
        return result

    async def list(self) -> list[Record]:
        ids = ['anthropic']
        directory = self.state.directory / 'subscriptions'
        if directory.exists():
            for entry in directory.iterdir():
                if (entry.is_dir() and isinstance(record := read_json(self.state.subscription(entry.name) / 'record.json'), dict)
                        and record.get('family') != 'zai' and entry.name not in ids):
                    ids.append(entry.name)
        return [await self.get(identifier) for identifier in ids]

    async def create(self, family: str, label: str, explicit: str | None = None) -> Record:
        family_id(family)
        if explicit in {'zai', 'claude-zai'}:
            raise retired_route()
        if not label:
            raise Failure('invalid_input', 'Give the new Subscription a label.')
        count = 2
        while True:
            identifier = explicit or f'{family}-{count}'
            family_settings(family, self.defaults(identifier).get('settings', {}), f'{self.settings_file}: subscriptions.{identifier}.settings.', [])
            async with self.state.lock(identifier):
                folder = self.state.subscription(identifier)
                if identifier != 'anthropic' and read_json(folder / 'record.json') is None and read_json(folder / 'sign-in.json') is None:
                    timestamp = now()
                    private_directory(folder / 'home')
                    owner_skills(folder / 'home', str(self.settings_file.parent.parent))
                    write_json(folder / 'record.json', {'id': identifier, 'family': family, 'label': label, 'auth': 'vendor_login' if family == 'anthropic' else 'key', 'state': 'signed_out', 'createdAt': timestamp, 'updatedAt': timestamp})
                    break
            if explicit is not None:
                raise Failure('in_use', 'That Subscription identifier already exists.')
            count += 1
        return await self.get(identifier)

    async def update(self, identifier: str, label: str | None) -> Record:
        await self.get(identifier)
        if label is not None:
            if not label:
                raise Failure('invalid_input', 'Give the Subscription a non-empty label.')
            async with self.state.lock(identifier):
                record = await self.record(identifier)
                home = self.state.subscription(identifier) / 'home'
                private_directory(home)
                owner_skills(home, str(self.settings_file.parent.parent))
                write_json(self.state.subscription(identifier) / 'record.json', {**record, 'label': label, 'updatedAt': now()})
        return await self.get(identifier)

    async def require_signed_in(self, identifier: str) -> Record:
        subscription = await self.get(identifier)
        if subscription['state'] != 'signed_in':
            raise Failure('not_signed_in', 'Sign in to this Subscription first.', {'remedy': 'Sign in.'})
        return subscription

    async def environment(self, identifier: str, inherited: Env, key: str | None = None) -> Env:
        await self.get(identifier)
        home = self.state.subscription(identifier) / 'home'
        private_directory(home)
        async with self.state.lock(identifier):
            owner_skills(home, inherited.get('HOME', ACTUAL_HOME))
        env = clean_environment(inherited, ACTUAL_HOME, str(home))
        return env

    async def run_report(self, initial: Record, failure: Record | None = None, usage: Record | None = None) -> None:
        if (failure or {}).get('code') != 'credential_rejected' and not usage:
            return
        async with self.state.lock(initial['id']):
            current = await self.record(initial['id'])
            own = (failure or {}).get('code') == 'credential_rejected' and current['state'] == 'needs_attention' and current.get('problem') == failure
            if not own and (current['state'] != 'signed_in' or current.get('checkedAt') != initial.get('checkedAt')):
                return
            if usage and (not current.get('lastUsage') or epoch(usage['fetchedAt']) >= epoch(current['lastUsage']['fetchedAt'])):
                current['lastUsage'] = usage
            if (failure or {}).get('code') == 'credential_rejected' and not own:
                current.update(state='needs_attention', problem=failure, checkedAt=now())
            current['updatedAt'] = now()
            write_json(self.state.subscription(initial['id']) / 'record.json', current)


def keychain_service(directory: Path, home: str = ACTUAL_HOME) -> str:
    path = str(directory.resolve())
    normal = str((Path(home) / 'Library/Application Support/Arbol/drivers/claude').resolve())
    service = 'com.sylvan-stack.arbol.claude-driver'
    return service if path == normal else service + '.' + hashlib.sha256(path.encode()).hexdigest()[:12]


def validate_key(key: str) -> None:
    bad = [char for char in key if not '\x21' <= char <= '\x7e'][:3]
    if not key or bad:
        detail = ', '.join(f'{compact(char)} (U+{ord(char):04X})' for char in bad)
        raise Failure('invalid_input', 'The key is empty.' if not key else f'The key contains invalid characters: {detail}.')


class Secrets:
    def __init__(self, service: str, home: str, inherited: Env, cancel: asyncio.Event | None = None, security: str = '/usr/bin/security'):
        self.service, self.home, self.cancel, self.security = service, home, cancel, security
        self.env = clean_environment(inherited, home)

    async def item(self, args: list[str], text: str | None = None) -> Record:
        return await child_command(self.security, args, {**self.env, 'HOME': ACTUAL_HOME}, ACTUAL_HOME, 20, self.cancel, text, '-g' in args)

    async def read(self, identifier: str, source: Record | None = None) -> str | None:
        if source and source['from'] == 'command':
            args = source['command']
            result = await child_command(args[0], args[1:], self.env, self.home, 20, self.cancel)
            if result['code'] != 0:
                raise Failure('tool_failed', 'The secret-source command failed. Its output was withheld.')
            return re.sub(r'\r?\n\Z', '', result['stdout'], count=1)
        result = await self.item(['find-generic-password', '-s', self.service, '-a', identifier, '-w'])
        if result['code'] == 44:
            return None
        if result['code'] != 0:
            raise Failure('tool_failed', 'The Keychain item could not be read.')
        value = re.sub(r'\r?\n\Z', '', result['stdout'], count=1)
        if re.fullmatch('(?:[0-9a-fA-F]{2})+', value):
            typed = await self.item(['find-generic-password', '-s', self.service, '-a', identifier, '-g'])
            if typed['code'] != 0:
                raise Failure('tool_failed', 'The Keychain item could not be read.')
            if re.search(r'^password: 0x[0-9a-f]+', typed.get('stderr', ''), re.I | re.M):
                value = bytes.fromhex(value).decode('utf-8', 'replace')
        return str(value)

    async def remove(self, identifier: str) -> None:
        result = await self.item(['delete-generic-password', '-s', self.service, '-a', identifier])
        if result['code'] not in {0, 44}:
            raise Failure('tool_failed', 'The Keychain item could not be removed.')

    async def store(self, identifier: str, key: str) -> None:
        validate_key(key)
        def quote(value: str) -> str:
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        result = await self.item(['-i'], f'add-generic-password -U -s {quote(self.service)} -a {quote(identifier)} -w {quote(key)}\n')
        if result['code'] != 0 or await self.read(identifier) != key:
            raise Failure('tool_failed', 'The imported key could not be stored in the Keychain.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


async def gateway(base: str, path: str, key: str, timeout: float, cancel: asyncio.Event) -> Any:
    parsed = urllib.parse.urlparse(base)
    url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))
    def fetch() -> Any:
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
        try:
            try:
                response = urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                if status in {401, 403}:
                    raise Failure('credential_rejected', 'Z.AI rejected the key.', {'remedy': 'Replace the key.', 'detail': {'basis': 'fields', 'vendorStatus': status}})
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise Failure('vendor_unavailable', 'Z.AI returned too much data.')
                try:
                    value = loads(data)
                except ValueError:
                    raise Failure('vendor_unavailable', 'Z.AI returned an unreadable answer.') from None
                if not 200 <= status < 300 or not isinstance(value, dict) or value.get('success') is False or ('/quota/' in path and value.get('success') is not True):
                    message = next((value.get(field) for field in ['msg', 'message', 'error'] if isinstance(value, dict) and isinstance(value.get(field), str) and value[field]), 'Z.AI could not answer the request.')
                    raise Failure('vendor_unavailable', safe_text(str(message), [key])[:240], {'detail': {'basis': 'fields', 'vendorStatus': status}}, [key])
                return value
        except Failure:
            raise
        except (OSError, ValueError):
            raise Failure('vendor_unavailable', 'Z.AI could not be reached.') from None
    task = asyncio.create_task(asyncio.to_thread(fetch))
    stopped = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait([task, stopped], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            task.cancel()
            raise Failure('vendor_unavailable', 'Z.AI could not be reached.')
        return await task
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)


def normalize_quota(value: Any, timestamp: float | None = None, key: str = '') -> Record:
    current = time.time() if timestamp is None else timestamp
    if not isinstance(value, dict) or value.get('success') is not True or not isinstance(value.get('data'), dict) or not isinstance(value['data'].get('limits'), list):
        raise Failure('vendor_unavailable', 'Z.AI returned an unreadable quota answer.')
    data = value['data']
    entries = [item for item in data['limits'] if isinstance(item, dict) and item.get('type') in {'TOKENS_LIMIT', 'CREDIT_LIMIT', 'TIME_LIMIT'} and number(item.get('percentage'))]
    def minutes(item: Record) -> float | None:
        unit = {1: 1440, 3: 60, 5: 1, 6: 10080}.get(item.get('unit', -1))
        count: Any = item.get('number')
        return count * unit if unit is not None and number(count) and count > 0 else None
    ordered = sorted([item for item in entries if item['type'] != 'TIME_LIMIT'], key=lambda item: minutes(item) or float('inf')) + [item for item in entries if item['type'] == 'TIME_LIMIT']
    totals: dict[str, float] = {}
    for item in entries:
        for model in item.get('usageDetails', []):
            if isinstance(model, dict) and isinstance(model.get('modelCode'), str) and number(model.get('usage')):
                totals[model['modelCode']] = totals.get(model['modelCode'], 0) + model['usage']
    plan = next((data.get(field) for field in ['planName', 'plan', 'plan_type', 'packageName', 'level'] if isinstance(data.get(field), str) and data[field]), None)
    windows = []
    for index, item in enumerate(ordered):
        length = minutes(item)
        total: Any = item.get('usage')
        remaining: Any = item.get('remaining')
        used: Any = item.get('currentValue')
        percentage = 100 * (total - remaining if number(remaining) else used) / total if number(total) and total > 0 and (number(remaining) or number(used)) else item['percentage']
        reset = item.get('nextResetTime')
        valid_reset = number(reset) and not (length == 300 and item['type'] != 'TIME_LIMIT' and reset / 1000 - current > 301 * 60)
        unit = {1: 'day', 3: 'hour', 5: 'minute', 6: 'week'}.get(item.get('unit', -1))
        label = 'MCP' if item['type'] == 'TIME_LIMIT' else '5-hour window' if length == 300 else 'Weekly window' if length == 10080 else f'{item.get("number")} {unit}{"" if item.get("number") == 1 else "s"} window' if length is not None else 'Usage window'
        window: Record = {'kind': 'other' if item['type'] == 'TIME_LIMIT' else 'session' if index == 0 else 'weekly', 'label': label, 'usedPercent': int(min(100, max(0, percentage)) * 10 + .5) / 10}
        if length is not None:
            window['windowMinutes'] = length
        if valid_reset and reset is not None:
            try:
                window['resetsAt'] = now(reset / 1000)
            except (OverflowError, ValueError):
                pass
        windows.append(window)
    return {'outcome': 'ok', 'fetchedAt': now(current), 'windows': windows, **({'plan': safe_text(plan, [key])} if plan else {}), **({'extra': {'models': [{'model': safe_text(model, [key]), 'usage': usage} for model, usage in sorted(totals.items(), key=lambda pair: -pair[1])]}} if totals else {})}


class Authentication:
    def __init__(self, subscriptions: Subscriptions, env: Env, cancel: asyncio.Event):
        self.subscriptions, self.env, self.cancel = subscriptions, env, cancel
        self.secrets = Secrets(keychain_service(subscriptions.state.directory), env.get('HOME', ACTUAL_HOME), env, cancel)

    def path(self, identifier: str) -> Path:
        return self.subscriptions.state.subscription(identifier) / 'sign-in.json'

    def temporary(self, identifier: str) -> Path:
        return self.subscriptions.state.directory / 'tmp' / f'sign-in-{identifier}'

    def progress(self, identifier: str) -> Record | None:
        value = read_json(self.path(identifier))
        if value is None:
            return None
        if not isinstance(value, dict) or not valid_date(value.get('expiresAt')) or value.get('operation', 'sign_in') not in {'sign_in', 'sign_out', 'remove'} or value.get('stopReason', 'replaced') not in {'replaced', 'signed_out'}:
            raise Failure('internal', 'Malformed sign-in record.')
        parse_owner(value.get('owner'))
        if 'tool' in value:
            parse_owner(value['tool'])
        return dict(value)

    async def clean_progress(self, identifier: str) -> None:
        await self.subscriptions.record(identifier)
        progress = self.progress(identifier)
        if not progress or await owner_alive(progress['owner']):
            return
        if progress.get('tool') and await owner_alive(progress['tool']):
            kill_group(progress['tool']['pid'], signal.SIGKILL)
        async with self.subscriptions.state.lock(identifier):
            current = self.progress(identifier)
            if current and current['owner'] == progress['owner']:
                shutil.rmtree(self.temporary(identifier), ignore_errors=True)
                self.path(identifier).unlink(missing_ok=True)

    async def clean_all_progress(self) -> None:
        directory = self.subscriptions.state.directory / 'subscriptions'
        if directory.exists():
            for path in directory.iterdir():
                if path.is_dir():
                    record = read_json(path / 'record.json')
                    if path.name not in {'zai', 'claude-zai'} and not (isinstance(record, dict) and record.get('family') == 'zai'):
                        await self.clean_progress(path.name)

    async def reserve(self, identifier: str, operation: str) -> Record:
        await self.clean_progress(identifier)
        owner = await process_owner(os.getpid())
        if owner is None:
            raise Failure('internal', 'Cannot identify the Driver process.')
        deadline = time.monotonic() + fixed_wait(5000)
        signalled: set[tuple[int, str]] = set()
        while True:
            if self.cancel.is_set():
                raise Failure('tool_failed', 'The operation was stopped.')
            async with self.subscriptions.state.lock(identifier):
                await self.subscriptions.record(identifier)
                current = self.progress(identifier)
                if current:
                    if current.get('operation', 'sign_in') != 'sign_in':
                        raise Failure('in_use', 'The Subscription is being signed out or removed.')
                    current['stopReason'] = 'replaced' if operation == 'sign_in' else 'signed_out'
                    write_json(self.path(identifier), current)
                else:
                    current = {'owner': owner, 'operation': operation, 'expiresAt': now(time.time() + fixed_wait(300000))}
                    write_json(self.path(identifier), current)
                    return current
            if await owner_alive(current['owner']):
                identity = (current['owner']['pid'], current['owner']['startedAt'])
                if identity not in signalled:
                    try:
                        os.kill(current['owner']['pid'], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    signalled.add(identity)
            else:
                await self.clean_progress(identifier)
            if time.monotonic() >= deadline:
                raise Failure('in_use', 'The previous sign-in did not stop within five seconds.')
            await asyncio.sleep(fixed_wait(50))

    async def release(self, identifier: str, owner: Record) -> None:
        async with self.subscriptions.state.lock(identifier):
            current = self.progress(identifier)
            if current and current['owner'] == owner:
                shutil.rmtree(self.temporary(identifier), ignore_errors=True)
                self.path(identifier).unlink(missing_ok=True)

    async def save(self, identifier: str, change: Callable[[Record], Record], revision: str | None = None, owner: Record | None = None) -> None:
        async with self.subscriptions.state.lock(identifier):
            record = await self.subscriptions.record(identifier)
            if owner:
                progress = self.progress(identifier)
                if not progress or progress['owner'] != owner or progress.get('stopReason'):
                    raise Failure('in_use', 'Another operation replaced this sign-in.')
            if revision is not None and record['updatedAt'] != revision:
                raise Failure('in_use', 'The Subscription changed while the vendor was answering. Retry the operation.')
            write_json(self.subscriptions.state.subscription(identifier) / 'record.json', {**change(record), 'updatedAt': now()})

    @staticmethod
    def unsigned(record: Record) -> Record:
        return {**{key: value for key, value in record.items() if key not in {'account', 'problem', 'lastUsage', 'checkedAt'}}, 'state': 'signed_out'}

    async def tool(self) -> str:
        return require_tool(await inspect_tool(self.subscriptions.state.directory, self.subscriptions.settings.get('toolPath'), self.env, self.cancel))

    async def key(self, identifier: str) -> str:
        await self.subscriptions.get(identifier)
        key = await self.secrets.read(identifier, self.subscriptions.defaults(identifier).get('secret'))
        if key is None:
            raise Failure('credential_rejected', 'The Subscription has no key.', {'remedy': 'Replace the key.'})
        validate_key(key)
        return key

    async def status(self, identifier: str) -> Record:
        env = await self.subscriptions.environment(identifier, self.env)
        result = await child_command(await self.tool(), ['auth', 'status', '--json'], env, env['CLAUDE_CONFIG_DIR'], 20, self.cancel)
        try:
            value = loads(result['stdout'])
        except ValueError:
            raise Failure('tool_failed', 'claude returned an unreadable account status.') from None
        if not isinstance(value, dict) or type(value.get('loggedIn')) is not bool or (result['code'] != 0 and value['loggedIn']):
            raise Failure('tool_failed', 'claude could not report its account status.')
        account = {target: value[source] for target, source in [('name', 'name'), ('email', 'email'), ('organization', 'orgName'), ('plan', 'subscriptionType')] if isinstance(value.get(source), str)}
        return {'accepted': value['loggedIn'], **({'account': account} if account else {})}

    async def rejection(self, initial: Record, failure: Failure) -> None:
        await self.save(initial['id'], lambda record: {**record, 'state': 'needs_attention', 'problem': failure.result, 'checkedAt': now()}, initial['updatedAt'])

    async def check(self, identifier: str) -> Record:
        await self.clean_progress(identifier)
        initial = await self.subscriptions.get(identifier)
        if initial['state'] == 'signed_out':
            raise Failure('not_signed_in', 'Sign in to this Subscription first.', {'remedy': 'Sign in.'})
        try:
            account = None
            status = await self.status(identifier)
            if not status['accepted']:
                raise Failure('credential_rejected', 'claude reports that the account is not logged in.', {'remedy': 'Sign in.'})
            account = status.get('account')
            await self.save(identifier, lambda record: {**{k: v for k, v in record.items() if k not in {'problem', 'account'}}, 'state': 'signed_in', **({'account': account} if account else {}), 'checkedAt': now()}, initial['updatedAt'])
        except Failure as error:
            if error.result['code'] != 'credential_rejected':
                raise
            await self.rejection(initial, error)
        return await self.subscriptions.get(identifier)

    async def usage(self, identifier: str) -> Record:
        initial = await self.subscriptions.get(identifier)
        if initial['state'] != 'signed_in':
            return {'outcome': 'needs_sign_in', 'message': 'Sign in to this Subscription first.', 'fetchedAt': now(), 'windows': []}
        return dict(initial.get('lastUsage', {'outcome': 'unsupported', 'message': 'Usage is available after a run reports it.', 'fetchedAt': now(), 'windows': []}))

    async def models(self, identifier: str) -> Record:
        await self.subscriptions.require_signed_in(identifier)
        fetched = now()
        child = None
        try:
            env = await self.subscriptions.environment(identifier, self.env)
            child = await spawn(await self.tool(), ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'bypassPermissions'], {**env, 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}, env['CLAUDE_CONFIG_DIR'])
            assert child.stdin and child.stdout and child.stderr
            drain = asyncio.create_task(child.stderr.read())
            child.stdin.write((compact({'type': 'control_request', 'request_id': 'catalog', 'request': {'subtype': 'initialize'}}) + '\n').encode())
            answer = None
            async with asyncio.timeout(30):
                while line := await child.stdout.readline():
                    try:
                        value = loads(line)
                    except ValueError:
                        continue
                    response = value.get('response') if isinstance(value, dict) else None
                    if isinstance(response, dict) and response.get('request_id') == 'catalog':
                        answer = response.get('response') if response.get('subtype') == 'success' else None
                        break
            if not isinstance(answer, dict) or not isinstance(answer.get('models'), list):
                raise Failure('tool_failed', 'claude did not return a model catalog.')
            models = []
            for item in answer['models']:
                if not isinstance(item, dict) or not isinstance(item.get('value'), str) or not isinstance(item.get('displayName'), str):
                    continue
                model = {'id': item['value'], 'label': item['displayName']}
                if isinstance(item.get('description'), str):
                    model['description'] = item['description']
                if item.get('supportsEffort') is True and isinstance(item.get('supportedEffortLevels'), list) and all(isinstance(level, str) for level in item['supportedEffortLevels']):
                    model['efforts'] = item['supportedEffortLevels']
                if item['value'] == 'default':
                    model['isDefault'] = True
                models.append(model)
            if not models:
                raise Failure('tool_failed', 'claude returned an empty model catalog.')
            return {'source': 'tool', 'fetchedAt': fetched, 'models': models}
        except (Failure, TimeoutError) as error:
            if isinstance(error, Failure) and error.result['code'] != 'tool_failed':
                raise
            return {'source': 'builtin', 'fetchedAt': fetched, 'models': [{'id': name, 'label': name} for name in ['default', 'opus', 'sonnet', 'haiku']]}
        finally:
            if child:
                kill_group(child.pid, signal.SIGKILL)
                await child.wait()
                await drain

    async def sign_in(self, identifier: str, by_code: bool, emit: Callable[[Record], Awaitable[None]], input_reader: asyncio.StreamReader) -> int:
        initial = await self.subscriptions.get(identifier)
        if by_code and initial['family'] != 'anthropic':
            raise Failure('unsupported', 'This family has no code sign-in.')
        progress = await self.reserve(identifier, 'sign_in')
        child = None
        succeeded = False
        timed_out = False
        output = ''
        try:
            await self.save(identifier, self.unsigned, owner=progress['owner'])
            async with asyncio.timeout(max(.001, epoch(progress['expiresAt']) - time.time())):
                folder = self.temporary(identifier)
                private_directory(folder)
                address = folder / 'address'
                browser = folder / 'browser'
                python_launcher(browser, f'import os,sys\nfd=os.open({str(address)!r},os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)\nos.write(fd,sys.argv[1].encode())\nos.close(fd)\n')
                env = await self.subscriptions.environment(identifier, self.env)
                child = await spawn(await self.tool(), ['auth', 'login', '--claudeai'], {**env, 'BROWSER': str(browser)}, env['CLAUDE_CONFIG_DIR'])
                assert child.stdout and child.stderr and child.stdin
                tool = await process_owner(child.pid)
                async with self.subscriptions.state.lock(identifier):
                    current = self.progress(identifier)
                    if current and current['owner'] == progress['owner']:
                        write_json(self.path(identifier), {**current, **({'tool': tool} if tool else {})})
                async def collect() -> None:
                    nonlocal output
                    assert child and child.stdout
                    while chunk := await child.stdout.read(65536):
                        output += chunk.decode('utf-8', 'replace')
                        if len(output.encode()) > 2 * 1024 * 1024:
                            raise Failure('tool_failed', 'The child command returned too much output.')
                reader = asyncio.create_task(collect())
                drainer = asyncio.create_task(child.stderr.read())
                url = None
                while not url and child.returncode is None:
                    if self.cancel.is_set():
                        raise Failure('tool_failed', 'Sign-in was cancelled.')
                    if by_code:
                        url = next((value for value in re.findall(r'https://[^\s]+', output) if 'platform.claude.com%2Foauth%2Fcode%2Fcallback' in value or 'platform.claude.com/oauth/code/callback' in value), None)
                    elif address.exists():
                        url = address.read_text()
                    if not url:
                        await asyncio.sleep(fixed_wait(50))
                if url:
                    allowed = ['claude.com', 'claude.ai', 'platform.claude.com', 'localhost']
                    if urllib.parse.urlparse(url).hostname not in allowed:
                        raise Failure('tool_failed', 'claude returned a sign-in address on an unexpected host.')
                    await emit({'step': 'open_url', 'url': url, 'allowedHosts': allowed, 'expiresAt': progress['expiresAt']})
                    if by_code:
                        await emit({'step': 'enter_code', 'prompt': 'Enter the one-time code shown by Claude.', 'expiresAt': progress['expiresAt']})
                        input_task = asyncio.create_task(input_reader.readline())
                        stop_task = asyncio.create_task(self.cancel.wait())
                        await asyncio.wait([input_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
                        stop_task.cancel()
                        if self.cancel.is_set():
                            input_task.cancel()
                            raise Failure('tool_failed', 'Sign-in was cancelled.')
                        try:
                            value = loads(await input_task)
                        except ValueError:
                            raise Failure('invalid_input', 'Enter the code as one JSON line.') from None
                        if not isinstance(value, dict) or not isinstance(value.get('input'), str) or not value['input'] or re.search('[\r\n]', value['input']):
                            raise Failure('invalid_input', 'Enter one nonempty code.')
                        child.stdin.write((value['input'] + '\n').encode())
                        await child.stdin.drain()
                while child.returncode is None:
                    if self.cancel.is_set():
                        raise Failure('tool_failed', 'Sign-in was cancelled.')
                    await asyncio.sleep(fixed_wait(50))
                await reader
                await drainer
                if child.returncode != 0:
                    raise Failure('credential_rejected', 'Claude sign-in failed. Try signing in again.', {'remedy': 'Sign in.'})
                status = await self.status(identifier)
                if not status['accepted']:
                    raise Failure('credential_rejected', 'claude did not confirm the login.', {'remedy': 'Sign in.'})
                await self.save(identifier, lambda record: {**record, 'state': 'signed_in', **({'account': status['account']} if status.get('account') else {}), 'checkedAt': now()}, owner=progress['owner'])
            succeeded = True
            await self.release(identifier, progress['owner'])
            await emit({'step': 'done', 'subscription': await self.subscriptions.get(identifier)})
            return 0
        except BaseException as error:
            timed_out = isinstance(error, TimeoutError)
            current = self.progress(identifier)
            reason = current.get('stopReason') if current else None
            final_failure = Failure('in_use', 'Another sign-in replaced this attempt.') if reason == 'replaced' else Failure('in_use', 'The Subscription was signed out during this attempt.') if reason == 'signed_out' else Failure('limit_reached', 'Sign-in timed out after five minutes.') if timed_out else Failure('tool_failed', 'Sign-in was cancelled.') if self.cancel.is_set() else error
            await emit({'step': 'failed', 'error': safe_failure(final_failure)})
            return 2 if self.cancel.is_set() and not reason else 1
        finally:
            if child:
                kill_group(child.pid, signal.SIGKILL)
                await child.wait()
            if not succeeded:
                async with self.subscriptions.state.lock(identifier):
                    current = self.progress(identifier)
                    if current and current['owner'] == progress['owner']:
                        write_json(self.subscriptions.state.subscription(identifier) / 'record.json', {**self.unsigned(await self.subscriptions.record(identifier)), 'updatedAt': now()})
                if initial['family'] == 'anthropic':
                    env = await self.subscriptions.environment(identifier, self.env)
                    cleanup = await inspect_tool(self.subscriptions.state.directory, self.subscriptions.settings.get('toolPath'), self.env, asyncio.Event())
                    if cleanup.get('path') and cleanup['supported']:
                        try:
                            await child_command(cleanup['path'], ['auth', 'logout'], env, env['CLAUDE_CONFIG_DIR'])
                        except Failure:
                            pass
            await self.release(identifier, progress['owner'])

    async def sign_out(self, identifier: str, force: bool, remove: bool = False) -> Record | None:
        from .continuity import Runs
        initial = await self.subscriptions.get(identifier)
        runs = Runs(self.subscriptions.state)
        for record in await runs.list():
            if record['subscriptionId'] == identifier:
                if not force:
                    raise Failure('in_use', 'A run is using the Subscription. Stop it first or use --force.')
                await runs.stop(record['runId'])
        progress = await self.reserve(identifier, 'remove' if remove else 'sign_out')
        try:
            await self.save(identifier, self.unsigned, owner=progress['owner'])
            if initial['family'] == 'anthropic' and initial['state'] != 'signed_out':
                env = await self.subscriptions.environment(identifier, self.env)
                result = await child_command(await self.tool(), ['auth', 'logout'], env, env['CLAUDE_CONFIG_DIR'], cancel=self.cancel)
                if result['code'] != 0:
                    raise Failure('tool_failed', 'claude could not sign the account out.')
            if remove:
                folder = self.subscriptions.state.subscription(identifier)
                shutil.rmtree(folder / 'home', ignore_errors=True)
                shutil.rmtree(folder / 'chats', ignore_errors=True)
                (folder / 'record.json').unlink(missing_ok=True)
        finally:
            await self.release(identifier, progress['owner'])
        return None if remove else await self.subscriptions.get(identifier)

    async def import_settings(self, identifier: str, file: Path) -> Record:
        raise retired_route()
