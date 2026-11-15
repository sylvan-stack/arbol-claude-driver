"""Exact exclusive administrative consumer; no normal settings/vendor dispatch.

The inherited descriptor is a duplicate of the parent's locked description.
Neither this module nor its caller ever issues LOCK_UN against it.
"""
from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .admission import (
    COMPONENT, MAINTENANCE_CONTEXT, MAINTENANCE_FD, NORMAL_CONTEXT, NORMAL_FD,
    descriptor_number, verify_acceptance,
)
from .core import Command, Failure
from .identity import canonical_id
from .identity_protocol import (
    Authority, Object, Payload, canonical_uuid, decode, physical, private_directory,
    read, read_bytes, refuse, sha256, validate,
)

OPERATIONS = ('upgrade', 'identity-reset', 'restore', 'identity-recover')


def administrative_arguments(command: Command) -> str:
    operation = command.name.removeprefix('state ')
    required = {'state-dir', 'maintenance', 'backup', 'confirm-quiescent'}
    if operation == 'upgrade':
        required.add('from-format')
    elif operation == 'restore':
        required.add('source')
    elif operation == 'identity-recover':
        required.update(('subscription', 'retire'))
    if (operation not in OPERATIONS or set(command.options) != required or command.positionals
            or command.options.get('confirm-quiescent') is not True
            or operation == 'upgrade' and command.options.get('from-format') != '1'
            or operation == 'identity-recover' and command.options.get('retire') is not True):
        raise Failure('invalid_input', 'Give the exact operation-specific state maintenance arguments.')
    for name in ('state-dir', 'maintenance', 'backup', *(['source'] if operation == 'restore' else [])):
        value = command.options.get(name)
        if not isinstance(value, str) or not value or len(value.encode()) > 4096 or not os.path.isabs(value) or os.path.abspath(value) != value:
            raise Failure('invalid_input', 'Maintenance requires bounded absolute canonical paths.')
    if operation == 'identity-recover':
        identifier = canonical_id(command.options['subscription'])
        if identifier in ('zai', 'claude-zai'):
            from .core import retired_route
            raise retired_route()
    return operation


@dataclass
class Maintenance:
    authority: Authority
    payload: Payload
    context: Object
    context_path: Path
    context_raw: bytes
    descriptor: int
    command: Command
    closed: bool = False

    @classmethod
    def receive(cls, payload: Payload, command: Command, environment: dict[str, str]) -> Maintenance:
        operation = administrative_arguments(command)
        if (any(name in environment for name in (NORMAL_FD, NORMAL_CONTEXT))
                or not all(name in environment for name in (MAINTENANCE_FD, MAINTENANCE_CONTEXT))):
            refuse()
        descriptor = descriptor_number(environment[MAINTENANCE_FD])
        path = physical(environment[MAINTENANCE_CONTEXT], directory=False)
        raw = read_bytes(path)
        context = decode(raw)
        validate('context', context)
        if (context['component'] != COMPONENT or context['payloadSha256'] != payload.digest
                or context['operation'] != operation or not canonical_uuid(context['session'])
                or context['stateDirectory'] != command.options['state-dir']
                or operation == 'identity-recover' and context['subscription'] != command.options['subscription']):
            refuse()
        authority = Authority(physical(context['registry']), COMPONENT)
        expected_path = authority.folder / 'sessions' / context['session'] / 'contexts' / (context['attempt'] + '.json')
        if (path != expected_path or payload.component != COMPONENT
                or payload.root != authority.registry / COMPONENT / payload.build / 'driver-payload'):
            refuse()
        result = cls(authority, payload, context, path, raw, descriptor, command)
        with authority.mutex():
            result.check_locked()
        # Only after the complete live handoff has validated do any target
        # state/backup path observations occur. No format/settings read here.
        state = physical(str(command.options['state-dir']))
        backup = private_directory(physical(str(command.options['backup'])))
        if (backup.is_relative_to(state) or state.is_relative_to(backup)
                or backup.is_relative_to(authority.registry) or authority.registry.is_relative_to(backup)):
            refuse()
        if operation == 'restore':
            source = private_directory(physical(str(command.options['source'])))
            if source == backup or source.is_relative_to(state) or state.is_relative_to(source):
                refuse()
        os.set_inheritable(descriptor, False)
        return result

    @property
    def session_folder(self) -> Path:
        return self.authority.folder / 'sessions' / str(self.context['session'])

    def operation(self) -> Object:
        operation = read(self.session_folder / 'operation.json')
        validate('operation', operation)
        base = {key: value for key, value in operation.items() if key not in ('phase', 'sourceSha256', 'backupSha256')}
        if base != self.context or (self.context['operation'] == 'restore') != (operation['sourceSha256'] is not None):
            refuse()
        return operation

    def check_locked(self) -> None:
        if self.closed or read_bytes(self.context_path) != self.context_raw:
            refuse()
        control = self.authority.read('control')
        if control != {'version': 1, 'phase': 'exclusive', 'session': self.context['session'], 'epoch': self.context['epoch']}:
            refuse()
        receipt_path = self.authority.folder / 'maintenance.json'
        if str(receipt_path) != self.command.options['maintenance']:
            refuse()
        raw = read_bytes(receipt_path)
        receipt = decode(raw)
        validate('maintenance', receipt)
        if (receipt != {'version': 1, 'kind': 'driver-state-maintenance', 'component': COMPONENT,
                        'session': self.context['session'], 'epoch': self.context['epoch'],
                        'phase': 'exclusive', 'gateSha256': self.context['gateSha256']}
                or sha256(raw) != self.context['receiptSha256']):
            refuse()
        acceptance = verify_acceptance(self.authority, self.payload, self.context['acceptanceSha256'])
        if acceptance['gateSha256'] != self.context['gateSha256']:
            refuse()
        self.operation()
        current = os.fstat(self.descriptor)
        named = (self.authority.folder / 'admission.lock').lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o600
                or (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)):
            refuse()
        fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        independent = self.authority.open_lock('admission.lock')
        try:
            try:
                fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                refuse()
        finally:
            os.close(independent)

    def check(self) -> None:
        with self.authority.mutex():
            self.check_locked()

    async def execute(self) -> Object:
        self.check()
        # Exact operation implementations follow as separately owned leaves;
        # this reserved dispatch never falls back to ordinary state admission.
        raise Failure('unsupported', 'This maintenance operation is not available in this source checkpoint.')

    def finish(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True
