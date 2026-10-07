"""Run registration, cleanup election and conditional saved-session continuity."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path

from .core import Failure, Record, compact, fixed_wait, now
from .history import digest, encode_folder, message_digest, session_lines
from .processes import end_run_groups
from .state import State, atomic_write, owner_alive, parse_owner, prepare_file, process_owner, read_json, write_json


def same_reservation(current: Record, expected: Record) -> bool:
    return all(current.get(key) == expected.get(key) for key in ('runId', 'subscriptionId', 'startedAt', 'owner'))


class Runs:
    def __init__(self, state: State):
        self.state = state

    def path(self, identifier: str) -> Path:
        if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', identifier):
            raise Failure('invalid_input', 'The run identifier is not a safe file name.')
        return self.state.directory / 'runs' / (identifier + '.json')

    async def list(self, recover: bool = True) -> list[Record]:
        directory = self.state.directory / 'runs'
        if not directory.exists():
            return []
        result = []
        for file in directory.iterdir():
            if file.suffix != '.json' or file.name.startswith('.'):
                continue
            record = read_json(file)
            if record is None:
                continue
            if not isinstance(record, dict) or not isinstance(record.get('runId'), str) or file.name != record['runId'] + '.json' or not isinstance(record.get('subscriptionId'), str):
                raise Failure('internal', 'Malformed run record.')
            self.path(record['runId'])
            self.state.subscription(record['subscriptionId'])
            if await owner_alive(parse_owner(record.get('owner'))):
                result.append(record)
            elif recover:
                await self.recover(record)
        return result

    async def recover(self, stale: Record) -> None:
        owner = await process_owner(os.getpid())
        if owner is None:
            raise Failure('internal', 'Cannot identify the run cleanup owner.')
        deadline = time.monotonic() + fixed_wait(20000)
        while True:
            async with self.state.lock(stale['subscriptionId']):
                current = read_json(self.path(stale['runId']))
                if not current or not same_reservation(current, stale) or await owner_alive(parse_owner(current['owner'])):
                    return
                if current.get('cleanupOwner') and await owner_alive(parse_owner(current['cleanupOwner'])):
                    claimed = None
                else:
                    current['cleanupOwner'] = owner
                    self.save(current)
                    claimed = current
            if claimed is None:
                if time.monotonic() >= deadline:
                    raise Failure('internal', 'Another call could not finish recovering the run.')
                await asyncio.sleep(fixed_wait(50))
                continue
            try:
                folder = self.state.directory / 'tmp' / stale['runId']
                remaining = await end_run_groups(folder, claimed.get('tool'), min(deadline, time.monotonic() + fixed_wait(15000)))
                if remaining:
                    raise Failure('internal', 'Could not end orphaned process groups: ' + ', '.join(map(str, remaining)))
                async with self.state.lock(stale['subscriptionId']):
                    current = read_json(self.path(stale['runId']))
                    if not current or not same_reservation(current, claimed) or current.get('cleanupOwner') != owner or await owner_alive(parse_owner(current['owner'])):
                        return
                    shutil.rmtree(folder, ignore_errors=True)
                    if claimed.get('chatKey') and claimed.get('continuedConversation'):
                        if not re.fullmatch('[a-f0-9]{64}', claimed['chatKey']):
                            raise Failure('internal', 'Malformed chat key.')
                        path = self.state.subscription(stale['subscriptionId']) / 'chats' / (claimed['chatKey'] + '.json')
                        entry = read_json(path)
                        if isinstance(entry, dict) and entry.get('conversation') == claimed['continuedConversation']:
                            path.unlink(missing_ok=True)
                    self.remove(stale['runId'])
            except BaseException:
                async with self.state.lock(stale['subscriptionId']):
                    current = read_json(self.path(stale['runId']))
                    if current and same_reservation(current, claimed) and current.get('cleanupOwner') == owner:
                        current.pop('cleanupOwner', None)
                        self.save(current)
                raise
            return

    async def stop(self, identifier: str) -> None:
        self.path(identifier)
        record = next((record for record in await self.list() if record['runId'] == identifier), None)
        if record is None:
            return
        if await owner_alive(record['owner']):
            try:
                os.kill(record['owner']['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + fixed_wait(20000)
        while True:
            current = read_json(self.path(identifier))
            if current is None or current['owner'] != record['owner']:
                return
            if not await owner_alive(parse_owner(current['owner'])):
                await self.recover(current)
                return
            if time.monotonic() >= deadline:
                raise Failure('in_use', 'The run did not stop within twenty seconds.')
            await asyncio.sleep(fixed_wait(50))

    async def reserve(self, request: Record, conversation: str, continued: str | None = None) -> Record:
        owner = await process_owner(os.getpid())
        if owner is None:
            raise Failure('internal', 'Cannot identify the run owner.')
        record = {'runId': request['runId'], 'subscriptionId': request['subscriptionId'], 'owner': owner, 'startedAt': now(), 'conversation': conversation, **({'labels': request['labels']} if request.get('labels') else {}), **({'continuedConversation': continued} if continued else {}), **({'chatKey': digest(request['chatSessionId'])} if request.get('chatSessionId') else {})}
        path = self.path(request['runId'])
        temporary = prepare_file(path, compact(record) + '\n')
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise Failure('in_use', 'That run identifier is already in use.') from None
        finally:
            temporary.unlink()
        return record

    def save(self, record: Record) -> None:
        write_json(self.path(record['runId']), record)

    def remove(self, identifier: str) -> None:
        self.path(identifier).unlink(missing_ok=True)


def session_file(home: Path, conversation: str) -> Path | None:
    projects = home / 'projects'
    if projects.is_dir():
        for folder in projects.iterdir():
            path = folder / (conversation + '.jsonl')
            if folder.is_dir() and path.is_file():
                return path
    return None


class Chats:
    def __init__(self, state: State):
        self.state = state

    def path(self, subscription: str, key: str) -> Path:
        return self.state.subscription(subscription) / 'chats' / (key + '.json')

    def entry(self, subscription: str, key: str) -> Record | None:
        value = read_json(self.path(subscription, key))
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get('chatSessionId'), str) or not re.fullmatch('[a-fA-F0-9-]{36}', str(value.get('conversation'))) or not isinstance(value.get('workingDirectory'), str) or type(value.get('messages')) is not int or value['messages'] < 0 or not all(re.fullmatch('[a-f0-9]{64}', str(value.get(field))) for field in ['digest', 'systemPromptDigest']) or not isinstance(value.get('writtenAt'), str):
            raise Failure('internal', 'Malformed chat map entry.')
        return dict(value)

    async def reserve(self, request: Record, runs: Runs, home: Path) -> tuple[Record, Record]:
        await runs.list()
        async with self.state.lock(request['subscriptionId']):
            active = await runs.list(False)
            plan: Record = {'continuity': 'rebuilt' if request['history'] else 'fresh', 'conversation': str(uuid.uuid4()), 'resume': False, 'textForm': False}
            if request['history']:
                key = digest(request['chatSessionId']) if request.get('chatSessionId') and request.get('workingDirectory') is not None else None
                entry = self.entry(request['subscriptionId'], key) if key else None
                fingerprint = message_digest(request['history'])
                reason = 'no_chat_session' if not key else 'first_run' if not entry else 'history_differs' if entry['messages'] != fingerprint['messages'] or entry['digest'] != fingerprint['digest'] else 'working_folder_changed' if entry['workingDirectory'] != request.get('workingDirectory') else 'conversation_gone' if not session_file(home, entry['conversation']) else 'in_use' if any(record['subscriptionId'] == request['subscriptionId'] and record.get('conversation') == entry['conversation'] for record in active) else None
                if reason:
                    plan['rebuildReason'] = reason
                else:
                    assert entry is not None
                    plan.update(continuity='resumed', resume=True, conversation=entry['conversation'], continuedConversation=entry['conversation'])
            record = await runs.reserve(request, plan['conversation'], plan.get('continuedConversation'))
            return plan, record

    def prepare(self, plan: Record, request: Record, home: Path, cwd: str) -> None:
        if plan['continuity'] != 'rebuilt':
            return
        folder = encode_folder(cwd)
        if len(folder) > 200:
            projects = home / 'projects'
            candidates = sorted([entry.name for entry in projects.iterdir() if entry.is_dir() and entry.name.startswith(folder[:200])]) if projects.is_dir() else []
            if not candidates:
                plan['textForm'] = True
                return
            folder = candidates[0]
        file = home / 'projects' / folder / (plan['conversation'] + '.jsonl')
        try:
            atomic_write(file, '\n'.join(compact(line) for line in session_lines(request['history'], plan['conversation'], cwd)) + '\n')
        except OSError:
            plan['textForm'] = True
            return
        plan.update(syntheticFile=str(file), resume=True)

    async def settle(self, request: Record, plan: Record, fingerprint: Record, completed: bool, home: Path) -> None:
        if not request.get('chatSessionId') or request.get('workingDirectory') is None:
            return
        key = digest(request['chatSessionId'])
        saved = completed and session_file(home, plan['conversation'])
        async with self.state.lock(request['subscriptionId']):
            if saved:
                write_json(self.path(request['subscriptionId'], key), {'chatSessionId': request['chatSessionId'], 'conversation': plan['conversation'], 'workingDirectory': request['workingDirectory'], **fingerprint, 'systemPromptDigest': digest(request.get('systemPrompt', '')), 'writtenAt': now()})
            elif plan.get('continuedConversation'):
                entry = self.entry(request['subscriptionId'], key)
                if entry and entry['conversation'] == plan['continuedConversation']:
                    self.path(request['subscriptionId'], key).unlink(missing_ok=True)
