"""Local Subscription records and nonsecret native account status."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Any

from .core import Env, Failure, Record, clean_environment, compact, epoch, fixed_wait, now, safe_failure, valid_date, loads, retired_route
from .identity import canonical_id, private_file, unsafe
from .identity_protocol import sync_directory, token, valid_token
from .processes import ACTUAL_HOME, child_command, inspect_tool, kill_group, require_tool, spawn
from .state import State, durable_delete, owner_alive, parse_owner, process_owner, read_json, write_json

FAMILIES: list[Record] = [
    {'id': 'anthropic', 'label': 'Anthropic', 'auth': 'vendor_login', 'settings': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


def native_sign_in_required() -> Failure:
    return Failure(
        'not_signed_in', 'This Subscription needs an active Claude Code account.',
        {'remedy': 'Sign in directly in Claude Code, then refresh the Subscription.'},
    )


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

    async def identity(self, identifier: str, expected: tuple[str, str] | None = None) -> Record:
        if identifier in {'zai', 'claude-zai'}:
            raise retired_route()
        canonical_id(identifier)
        identity = self.state.identity
        if identity is None:
            raise unsafe()
        identity.require_proof()
        async with self.state.lock(identifier):
            path = self.state.subscription(identifier) / 'record.json'
            try:
                private_file(path)
            except FileNotFoundError:
                if identifier != 'anthropic':
                    raise Failure('not_found', 'Subscription was not found.') from None
            # Only the local canonical record validator is used: no settings,
            # progress cleanup, tool discovery, credential or vendor access.
            await self.record(identifier)
            result = identity.capture(identifier, expected)
            if len(compact(result).encode('utf-8')) + 1 > 4096:
                raise unsafe()
            return result

    async def get(self, identifier: str) -> Record:
        record = await self.record(identifier)
        if identifier not in self.resolved:
            self.resolved[identifier] = family_settings(record['family'], self.defaults(identifier).get('settings', {}), f'{self.settings_file}: subscriptions.{identifier}.settings.', self.notices)
        result = {**record, 'settings': self.resolved[identifier]}
        progress = read_json(self.state.subscription(identifier) / 'sign-in.json')
        if progress is not None:
            if not isinstance(progress, dict) or not valid_date(progress.get('expiresAt')):
                raise Failure('internal', 'Malformed sign-in record.')
            if 'attempt' in progress and not valid_token(progress['attempt']):
                raise Failure('internal', 'Malformed sign-in attempt.')
            current_lifetime = self.state.identity is None or self.matches(identifier, progress.get('identity'))
            if current_lifetime and progress.get('operation', 'sign_in') == 'sign_in' and await owner_alive(parse_owner(progress.get('owner'))):
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

    async def settle_local(self, identifier: str) -> None:
        """Only under the canonical short lock; no vendor/native cleanup here."""
        identity = self.state.identity
        if identity is None or identity.format != 2:
            return
        journal = identity.read_pending(identifier)
        if journal is None:
            return
        if journal['operation'] == 'remove':
            if journal['phase'] != 'committed':
                raise unsafe()  # Missing owner/cleanup proof never reactivates it.
            identity.finish_remove(identifier, journal['witness'])
            return
        present = read_json(self.state.subscription(identifier) / 'record.json') is not None
        if present:
            await self.record(identifier)
            identity.finish_create(identifier, journal['witness'])
        elif journal['phase'] == 'prepared':
            identity.publish_sidecar(identifier, journal['witness'], 'retired')
            identity.clear_journal(identifier)
        else:
            raise unsafe()

    @asynccontextmanager
    async def mutation(self, identifier: str) -> AsyncIterator[None]:
        async with self.state.lock(identifier, allow_pending=True):
            await self.settle_local(identifier)
            yield

    def matches(self, identifier: str, lifetime: Record | None) -> bool:
        identity = self.state.identity
        return identity.matches(identifier, lifetime) if identity is not None else lifetime is None

    async def snapshot(self, identifier: str) -> tuple[Record, Record | None]:
        async with self.mutation(identifier):
            record = await self.get(identifier)
            lifetime = self.state.identity.lifetime(identifier) if self.state.identity is not None else None
            return record, lifetime

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
            async with self.mutation(identifier):
                folder = self.state.subscription(identifier)
                if identifier != 'anthropic' and read_json(folder / 'record.json') is None and read_json(folder / 'sign-in.json') is None:
                    timestamp = now()
                    identity = self.state.identity
                    witness = token() if identity is not None and identity.format == 2 else None
                    if witness is not None and identity is not None:
                        identity.journal(identifier, 'create', witness)
                    write_json(folder / 'record.json', {'id': identifier, 'family': family, 'label': label, 'auth': 'vendor_login' if family == 'anthropic' else 'key', 'state': 'signed_out', 'createdAt': timestamp, 'updatedAt': timestamp})
                    if witness is not None and identity is not None:
                        identity.finish_create(identifier, witness)
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
            async with self.mutation(identifier):
                if read_json(self.state.subscription(identifier) / 'sign-in.json') is not None:
                    raise Failure('in_use', 'An earlier Subscription operation is still active or needs recovery.')
                record = await self.record(identifier)
                write_json(self.state.subscription(identifier) / 'record.json', {**record, 'label': label, 'updatedAt': now()})
        return await self.get(identifier)

    @staticmethod
    def require_native_route(identifier: str) -> None:
        if identifier in {'zai', 'claude-zai'}:
            raise retired_route()
        if identifier != 'anthropic':
            raise Failure('unsupported', 'This Subscription cannot select a native Claude Code account.',
                          {'remedy': 'Use the anthropic Subscription with your existing Claude Code sign-in.'})

    @staticmethod
    def folder() -> Path:
        return Path(ACTUAL_HOME) / '.claude'

    async def environment(self, identifier: str, inherited: Env, *, lifetime: Record | None = None) -> Env:
        self.require_native_route(identifier)
        await self.get(identifier)
        if lifetime is not None and not self.matches(identifier, lifetime):
            raise Failure('in_use', 'The Subscription lifetime changed before environment preparation.')
        native_env = clean_environment(inherited, ACTUAL_HOME)
        if 'SHELL' in inherited:
            native_env['SHELL'] = inherited['SHELL']
        return native_env

    async def run_report(self, initial: Record, failure: Record | None = None, usage: Record | None = None, *, lifetime: Record | None = None) -> None:
        if (failure or {}).get('code') != 'credential_rejected' and not usage:
            return
        async with self.state.lock(initial['id'], allow_pending=True):
            if not self.matches(initial['id'], lifetime):
                return
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


class Authentication:
    def __init__(self, subscriptions: Subscriptions, env: Env, cancel: asyncio.Event):
        self.subscriptions, self.env, self.cancel = subscriptions, env, cancel

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
        if 'attempt' in value and not valid_token(value['attempt']):
            raise Failure('internal', 'Malformed sign-in attempt.')
        if 'tool' in value:
            parse_owner(value['tool'])
        if 'localOnly' in value and (
            value['localOnly'] is not True or value.get('operation') not in {'sign_out', 'remove'} or 'tool' in value
        ):
            raise Failure('internal', 'Malformed local Subscription reservation.')
        return dict(value)

    @staticmethod
    def same_progress(current: Record | None, expected: Record) -> bool:
        return current is not None and all(
            current.get(key) == expected.get(key) for key in ('owner', 'attempt', 'identity', 'operation', 'localOnly')
        )

    async def clean_progress(self, identifier: str) -> None:
        async with self.subscriptions.mutation(identifier):
            await self.subscriptions.record(identifier)
            progress = self.progress(identifier)
            if not progress:
                return
        if await owner_alive(progress['owner']):
            return
        if progress.get('tool') and await owner_alive(progress['tool']):
            return
        async with self.subscriptions.state.lock(identifier, allow_pending=True):
            current = self.progress(identifier)
            if current != progress:
                return
            if not progress.get('localOnly'):
                if identifier != 'anthropic' or progress.get('operation', 'sign_in') != 'sign_in':
                    # A legacy test route could have launched an unrecorded native logout.
                    return
                if 'tool' not in progress:
                    try:
                        self.temporary(identifier).lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        # The callback directory preceded native login spawn.
                        return
            if self.subscriptions.matches(identifier, progress.get('identity')):
                shutil.rmtree(self.temporary(identifier), ignore_errors=True)
            # Reap only this exact dead slot and never a newer lifetime's files.
            durable_delete(self.path(identifier))

    async def clean_all_progress(self) -> None:
        directory = self.subscriptions.state.directory / 'subscriptions'
        if directory.exists():
            for path in directory.iterdir():
                if path.is_dir() and (path / 'sign-in.json').is_file():
                    record = read_json(path / 'record.json')
                    if path.name not in {'zai', 'claude-zai'} and not (isinstance(record, dict) and record.get('family') == 'zai'):
                        await self.clean_progress(path.name)

    def require_idle(self, identifier: str) -> None:
        if self.progress(identifier) is not None:
            raise Failure('in_use', 'An earlier Subscription operation is still active or needs recovery.')

    async def reserve(self, identifier: str, operation: str, *, lifetime: Record | None) -> Record:
        await self.clean_progress(identifier)
        if self.cancel.is_set():
            raise Failure('tool_failed', 'The operation was stopped.')
        owner = await process_owner(os.getpid())
        if owner is None:
            raise Failure('internal', 'Cannot identify the Driver process.')
        async with self.subscriptions.mutation(identifier):
            if not self.subscriptions.matches(identifier, lifetime):
                raise Failure('in_use', 'The Subscription lifetime changed before reservation.')
            await self.subscriptions.record(identifier)
            self.require_idle(identifier)
            progress = {'owner': owner, 'operation': operation, 'localOnly': True,
                        'expiresAt': now(time.time() + fixed_wait(300000)),
                        'attempt': token(), **({'identity': lifetime} if lifetime is not None else {})}
            write_json(self.path(identifier), progress)
            return progress

    async def release(self, identifier: str, reservation: Record, *, allow_pending: bool = False) -> None:
        async with self.subscriptions.state.lock(identifier, allow_pending=True):
            current = self.progress(identifier)
            if self.same_progress(current, reservation) and self.subscriptions.matches(identifier, reservation.get('identity')):
                shutil.rmtree(self.temporary(identifier), ignore_errors=True)
                durable_delete(self.path(identifier))

    async def save(self, identifier: str, change: Callable[[Record], Record], revision: str | None = None,
                   owner: Record | None = None, *, lifetime: Record | None = None) -> None:
        if owner is not None:
            lifetime = owner.get('identity')
        async with self.subscriptions.state.lock(identifier, allow_pending=True):
            if not self.subscriptions.matches(identifier, lifetime):
                raise Failure('in_use', 'The Subscription lifetime changed while the operation was in progress.')
            record = await self.subscriptions.record(identifier)
            if owner is None:
                self.require_idle(identifier)
            else:
                progress = self.progress(identifier)
                if not self.same_progress(progress, owner) or progress is None or progress.get('stopReason'):
                    raise Failure('in_use', 'Another operation replaced this reservation.')
            if revision is not None and record['updatedAt'] != revision:
                raise Failure('in_use', 'The Subscription changed while the vendor was answering. Retry the operation.')
            write_json(self.subscriptions.state.subscription(identifier) / 'record.json', {**change(record), 'updatedAt': now()})

    @staticmethod
    def unsigned(record: Record) -> Record:
        return {**{key: value for key, value in record.items() if key not in {'account', 'problem', 'lastUsage', 'checkedAt'}}, 'state': 'signed_out'}

    async def tool(self) -> str:
        return require_tool(await inspect_tool(self.subscriptions.state.directory, self.subscriptions.settings.get('toolPath'), self.env, self.cancel))

    async def status(self, identifier: str, lifetime: Record | None = None, *, program: str | None = None) -> Record:
        env = await self.subscriptions.environment(identifier, self.env, lifetime=lifetime)
        result = await child_command(program or await self.tool(), ['auth', 'status', '--json'], env,
                                     ACTUAL_HOME, 20, self.cancel)
        try:
            value = loads(result['stdout'])
        except ValueError:
            raise Failure('tool_failed', 'claude returned an unreadable account status.') from None
        if not isinstance(value, dict) or type(value.get('loggedIn')) is not bool or (result['code'] != 0 and value['loggedIn']):
            raise Failure('tool_failed', 'claude could not report its account status.')
        account = {target: value[source] for target, source in [('name', 'name'), ('email', 'email'), ('organization', 'orgName'), ('plan', 'subscriptionType')] if isinstance(value.get(source), str)}
        return {'accepted': value['loggedIn'], **({'account': account} if account else {})}

    async def require_native_login(self, identifier: str, lifetime: Record | None, *, program: str | None = None) -> None:
        self.require_idle(identifier)
        if not (await self.status(identifier, lifetime, program=program))['accepted']:
            raise native_sign_in_required()

    async def check(self, identifier: str) -> Record:
        self.subscriptions.require_native_route(identifier)
        await self.clean_progress(identifier)
        initial, lifetime = await self.subscriptions.snapshot(identifier)
        self.require_idle(identifier)
        status = await self.status(identifier, lifetime)
        account = status.get('account')
        checked_at = now()

        def observed(record: Record) -> Record:
            if not status['accepted']:
                return {**self.unsigned(record), 'state': 'needs_attention',
                        'problem': native_sign_in_required().result, 'checkedAt': checked_at}
            updated = {key: value for key, value in record.items() if key not in {'problem', 'account'}}
            if record.get('account') != account:
                updated.pop('lastUsage', None)
            if account:
                updated['account'] = account
            return {**updated, 'state': 'signed_in', 'checkedAt': checked_at}

        await self.save(identifier, observed, initial['updatedAt'], lifetime=lifetime)
        return await self.subscriptions.get(identifier)

    async def usage(self, identifier: str) -> Record:
        self.subscriptions.require_native_route(identifier)
        initial = await self.subscriptions.get(identifier)
        if initial['state'] != 'signed_in':
            return {'outcome': 'needs_sign_in', 'message': 'Sign in directly in Claude Code, then refresh the Subscription.', 'fetchedAt': now(), 'windows': []}
        return dict(initial.get('lastUsage', {'outcome': 'unsupported', 'message': 'Usage is available after a run reports it.', 'fetchedAt': now(), 'windows': []}))

    async def models(self, identifier: str) -> Record:
        self.subscriptions.require_native_route(identifier)
        initial, lifetime = await self.subscriptions.snapshot(identifier)
        if initial['state'] != 'signed_in':
            raise native_sign_in_required()
        program = await self.tool()
        await self.require_native_login(identifier, lifetime, program=program)
        fetched = now()
        child = None
        try:
            env = await self.subscriptions.environment(identifier, self.env, lifetime=lifetime)
            child = await spawn(program, ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose'], env, Path.cwd())
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

    async def sign_in(self, identifier: str, by_code: bool, emit: Callable[[Record], Awaitable[None]]) -> int:
        try:
            if by_code:
                raise Failure('unsupported', 'Complete the sign-in directly in Claude Code.')
            subscription = await self.check(identifier)
            if subscription['state'] != 'signed_in':
                raise native_sign_in_required()
        except Failure as error:
            await emit({'step': 'failed', 'error': safe_failure(error)})
            return 2 if self.cancel.is_set() else 1
        await emit({'step': 'done', 'subscription': subscription})
        return 0

    async def sign_out(self, identifier: str, force: bool, remove: bool = False) -> Record | None:
        from .continuity import Runs
        await self.subscriptions.get(identifier)
        if identifier == 'anthropic':
            raise Failure('invalid_input', 'The anthropic Subscription uses the Claude Code account.',
                          {'remedy': 'Sign out directly in Claude Code.'})
        _, lifetime = await self.subscriptions.snapshot(identifier)
        progress = await self.reserve(identifier, 'remove' if remove else 'sign_out', lifetime=lifetime)
        try:
            runs = Runs(self.subscriptions.state)
            for record in await runs.list():
                if record['subscriptionId'] == identifier:
                    if not force:
                        raise Failure('in_use', 'A run is using the Subscription. Stop it first or use --force.')
                    await runs.stop(record['runId'])
            await self.save(identifier, self.unsigned, owner=progress)
            if remove:
                folder = self.subscriptions.state.subscription(identifier)
                identity = self.subscriptions.state.identity
                witness = None
                async with self.subscriptions.mutation(identifier):
                    await self.subscriptions.record(identifier)
                    current = self.progress(identifier)
                    if not self.same_progress(current, progress):
                        raise Failure('in_use', 'Another operation replaced removal.')
                    if not self.subscriptions.matches(identifier, progress.get('identity')):
                        raise Failure('in_use', 'The Subscription lifetime changed during removal.')
                    if identity is not None and identity.format == 2:
                        witness = identity.capture(identifier)['witness']
                        identity.journal(identifier, 'remove', witness)
                # Retire the local map only. Any legacy native home belongs to Claude Code.
                path = folder / 'chats'
                if path.resolve(strict=False) != path:
                    raise unsafe()
                try:
                    shutil.rmtree(path)
                    sync_directory(folder)
                except FileNotFoundError:
                    pass
                async with self.subscriptions.state.lock(identifier, allow_pending=True):
                    current = self.progress(identifier)
                    if not self.same_progress(current, progress):
                        raise Failure('in_use', 'Another operation replaced removal.')
                    if witness is not None and identity is not None:
                        journal = identity.read_pending(identifier)
                        if journal != {'version': 1, 'operation': 'remove', 'witness': witness, 'phase': 'prepared'}:
                            raise unsafe()
                        durable_delete(self.path(identifier))
                        shutil.rmtree(self.temporary(identifier), ignore_errors=True)
                        identity.finish_remove(identifier, witness)
                    else:
                        durable_delete(folder / 'record.json')
        finally:
            await self.release(identifier, progress, allow_pending=remove)
        return None if remove else await self.subscriptions.get(identifier)

    async def import_settings(self, identifier: str, file: Path) -> Record:
        raise retired_route()
