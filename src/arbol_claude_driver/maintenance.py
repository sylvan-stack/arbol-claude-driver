"""Exact exclusive administrative consumer; no normal settings/vendor dispatch.

The inherited descriptor is a duplicate of the parent's locked description.
Neither this module nor its caller ever issues LOCK_UN against it.
"""
from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .admission import (
    COMPONENT, MAINTENANCE_CONTEXT, MAINTENANCE_FD, NORMAL_CONTEXT, NORMAL_FD,
    descriptor_number, verify_acceptance,
)
from .core import Command, Failure
from .identity import canonical_id, pending, private_file, root_identity, sidecar, state_format
from .identity_protocol import (
    Authority, LIMIT, MANIFEST_LIMIT, MAX_ENTRIES, Object, Payload, canonical, canonical_uuid, decode, fields, natural, physical, private_directory,
    publish, publish_bytes, read, read_bytes, refuse, remove, sha256, sync_directory, valid_hash, valid_token, validate,
)

from .state import State, owner_alive, parse_owner, prepare_file

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


ROOT_METADATA = ('format', 'identity.json', 'identity-maintenance.json', 'identity-upgrade.json')


def metadata_name(value: object) -> str:
    if not isinstance(value, str):
        refuse()
    if value in ROOT_METADATA:
        return value
    parts = value.split('/')
    if len(parts) != 3 or parts[0] != 'subscriptions' or parts[2] not in ('identity.json', 'identity-pending.json'):
        refuse()
    canonical_id(parts[1])
    return value


def metadata_names(directory: Path, extra: list[str] | None = None) -> list[str]:
    names = set(ROOT_METADATA)
    names.update(metadata_name(name) for name in (extra or []))
    subscriptions = directory / 'subscriptions'
    try:
        subscriptions.lstat()
    except FileNotFoundError:
        pass
    else:
        private_directory(subscriptions)
        for entry in subscriptions.iterdir():
            canonical_id(entry.name)
            private_directory(entry)
            names.update(f'subscriptions/{entry.name}/{name}' for name in ('identity.json', 'identity-pending.json'))
            if len(names) > MAX_ENTRIES:
                refuse()
    if len(names) > MAX_ENTRIES:
        refuse()
    return sorted(names, key=lambda name: name.encode())


def raw_format(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    return {b'1': 1, b'2': 2}.get(raw.strip())


@dataclass(frozen=True)
class Archive:
    directory: Path
    manifest: Object
    digest: str

    @classmethod
    def load(cls, directory: Path, *, source: bool = False) -> Archive:
        private_directory(directory)
        raw = read_bytes(directory / 'manifest.json', limit=MANIFEST_LIMIT)
        manifest = decode(raw, limit=MANIFEST_LIMIT)
        fields(manifest, 'version kind format files')
        if (manifest['kind'] != 'driver-identity-backup' or manifest['format'] is not None
                and (type(manifest['format']) is not int or manifest['format'] not in (1, 2))
                or not isinstance(manifest['files'], list) or len(manifest['files']) > MAX_ENTRIES):
            refuse()
        prior = b''
        expected = {'manifest.json'}
        size = len(raw)
        data: dict[str, bytes | None] = {}
        for row in manifest['files']:
            if not isinstance(row, dict) or set(row) != {'path', 'present', 'size', 'sha256'}:
                refuse()
            name = metadata_name(row['path'])
            if name.encode() <= prior or type(row['present']) is not bool or not natural(row['size']):
                refuse()
            prior = name.encode()
            if not row['present']:
                if row['size'] != 0 or row['sha256'] is not None:
                    refuse()
                data[name] = None
                continue
            if not valid_hash(row['sha256']):
                refuse()
            content = read_bytes(directory / name, limit=MANIFEST_LIMIT - size)
            if len(content) != row['size'] or sha256(content) != row['sha256']:
                refuse()
            size += len(content)
            data[name] = content
            expected.add(name)
            expected.update('/'.join(name.split('/')[:index]) for index in range(1, len(name.split('/'))))
        if not set(ROOT_METADATA).issubset(data) or raw_format(data['format']) != manifest['format']:
            refuse()
        actual: set[str] = set()

        def visit(folder: Path) -> None:
            for path in folder.iterdir():
                spelling = path.relative_to(directory).as_posix()
                if spelling not in expected:
                    refuse()
                actual.add(spelling)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    private_directory(path)
                    visit(path)
                elif not stat.S_ISREG(info.st_mode):
                    refuse()
        visit(directory)
        if actual != expected:
            refuse()
        if source:
            if manifest['format'] not in (1, 2) or manifest['format'] == 2 and data['identity.json'] is None:
                refuse()
            for name, source_bytes in data.items():
                if source_bytes is None or name == 'format':
                    continue
                document = decode(source_bytes)
                if name == 'identity.json':
                    fields(document, 'version generation')
                    if not valid_token(document['generation']):
                        refuse()
                elif name.endswith('/identity.json'):
                    sidecar(document)
                elif name.endswith('/identity-pending.json'):
                    pending(document)
                elif name == 'identity-maintenance.json':
                    local_marker(document)
                else:
                    fields(document, 'version from to generation backup backupSha256')
                    if (type(document['from']) is not int or document['from'] != 1
                            or type(document['to']) is not int or document['to'] != 2
                            or not valid_token(document['generation']) or not valid_hash(document['backupSha256'])
                            or not isinstance(document['backup'], str) or not os.path.isabs(document['backup'])):
                        refuse()
        return cls(directory, manifest, sha256(raw))

    @cached_property
    def rows(self) -> dict[str, Object]:
        return {row['path']: row for row in self.manifest['files']}

    def content(self, name: str) -> bytes | None:
        row = self.rows.get(name)
        if row is None or not row['present']:
            return None
        raw = read_bytes(self.directory / name, limit=MANIFEST_LIMIT)
        if len(raw) != row['size'] or sha256(raw) != row['sha256']:
            refuse()
        return raw


def local_marker(value: Object) -> Object:
    fields(value, 'version component session epoch operation attempt payloadSha256 generation phase sourceSha256 backupSha256')
    if (value['component'] != COMPONENT or not canonical_uuid(value['session'])
            or value['operation'] not in OPERATIONS or value['phase'] not in ('pending', 'complete')
            or any(not valid_token(value[key]) for key in ('epoch', 'attempt', 'generation'))
            or any(not valid_hash(value[key]) for key in ('payloadSha256', 'backupSha256'))
            or value['sourceSha256'] is not None and not valid_hash(value['sourceSha256'])
            or (value['operation'] == 'restore') != (value['sourceSha256'] is not None)):
        refuse()
    return value


def snapshot(directory: Path, names: list[str]) -> tuple[Object, dict[str, bytes | None]]:
    data: dict[str, bytes | None] = {}
    size = 0
    rows: list[Object] = []
    for name in names:
        metadata_name(name)
        try:
            content = read_bytes(directory / name, limit=MANIFEST_LIMIT - size)
        except FileNotFoundError:
            content = None
        data[name] = content
        size += len(content) if content is not None else 0
        rows.append({'path': name, 'present': content is not None, 'size': len(content) if content is not None else 0,
                     'sha256': sha256(content) if content is not None else None})
    manifest: Object = {'version': 1, 'kind': 'driver-identity-backup', 'format': raw_format(data['format']), 'files': rows}
    if len(canonical(manifest)) + size > MANIFEST_LIMIT:
        refuse()
    return manifest, data


def archive_parent(directory: Path, relative: str) -> None:
    current = directory
    for part in relative.split('/')[:-1]:
        current /= part
        private_directory(current, create=True)


def preserve(directory: Path, destination: Path, context: Object, source_hash: str | None,
             *, extra: list[str] | None = None) -> Archive:
    """Only called under the verified exclusive component and local guard."""
    binding_path = destination / 'preservation.json'
    before = destination / 'before'
    binding = {'version': 1, 'kind': 'driver-identity-preservation', 'attempt': context['attempt'],
               'operation': context['operation'], 'stateDirectorySha256': sha256(str(directory).encode()),
               'sourceSha256': source_hash}
    if binding_path.exists():
        saved = read(binding_path)
        fields(saved, 'version kind attempt operation stateDirectorySha256 sourceSha256 beforeSha256')
        if {key: value for key, value in saved.items() if key != 'beforeSha256'} != binding or not valid_hash(saved['beforeSha256']):
            refuse()
        if (before / 'manifest.json').exists():
            archive = Archive.load(before)
            if archive.digest != saved['beforeSha256']:
                refuse()
            return archive
    else:
        if any(destination.iterdir()):
            refuse()
        saved = {}
    manifest, data = snapshot(directory, metadata_names(directory, extra))
    digest = sha256(canonical(manifest))
    if saved and saved['beforeSha256'] != digest:
        refuse()  # Partial preservation cannot be rebound to changed state.
    if not saved:
        publish(binding_path, {**binding, 'beforeSha256': digest})
    private_directory(before, create=True)
    for name, content in data.items():
        if content is None:
            continue
        archive_parent(before, name)
        path = before / name
        if path.exists():
            if read_bytes(path, limit=MANIFEST_LIMIT) != content:
                refuse()
        else:
            publish_bytes(path, content, limit=MANIFEST_LIMIT)
    # Keep interrupted staging bytes as evidence outside the immutable entry.
    # Only exact private writer temporary names from this same bound attempt
    # are moved; unknown files are never silently ignored.
    for folder, _, files in os.walk(before, followlinks=False):
        for name in files:
            if not name.startswith('.') or '.' not in name[1:]:
                continue
            base, _, suffix = name[1:].rpartition('.')
            if base not in ('format', 'identity.json', 'identity-pending.json', 'identity-upgrade.json', 'identity-maintenance.json', 'manifest.json') or not valid_token(suffix):
                refuse()
            path = Path(folder) / name
            read_bytes(path, limit=MANIFEST_LIMIT)
            evidence = private_directory(destination / 'staging-evidence', create=True)
            target = evidence / (sha256(path.relative_to(before).as_posix().encode()) + '-' + suffix)
            if target.exists():
                refuse()
            os.rename(path, target)
            sync_directory(path.parent)
            sync_directory(evidence)
    publish(before / 'manifest.json', manifest, limit=MANIFEST_LIMIT)
    return Archive.load(before)



def preserve_entry(destination: Path, data: dict[str, bytes | None]) -> Archive:
    """An internal immutable metadata entry, not a registry authority source."""
    names = sorted(set(ROOT_METADATA) | set(data), key=lambda name: name.encode())
    if len(names) > MAX_ENTRIES:
        refuse()
    rows: list[Object] = []
    total = 0
    for name in names:
        metadata_name(name)
        content = data.get(name)
        total += len(content) if content is not None else 0
        rows.append({'path': name, 'present': content is not None, 'size': len(content) if content is not None else 0,
                     'sha256': sha256(content) if content is not None else None})
    manifest: Object = {'version': 1, 'kind': 'driver-identity-backup', 'format': raw_format(data.get('format')), 'files': rows}
    digest = sha256(canonical(manifest))
    if total + len(canonical(manifest)) > MANIFEST_LIMIT:
        refuse()
    private_directory(destination, create=True)
    if (destination / 'manifest.json').exists():
        result = Archive.load(destination)
        if result.digest != digest:
            refuse()
        return result
    for row in rows:
        if not row['present']:
            continue
        name = row['path']
        content = data[name]
        assert content is not None
        archive_parent(destination, name)
        path = destination / name
        if path.exists():
            if read_bytes(path, limit=MANIFEST_LIMIT) != content:
                refuse()
        else:
            publish_bytes(path, content, limit=MANIFEST_LIMIT)
    for folder, _, files in os.walk(destination, followlinks=False):
        for name in files:
            if not name.startswith('.'):
                continue
            base, _, suffix = name[1:].rpartition('.')
            if base not in (*ROOT_METADATA, 'identity-pending.json', 'manifest.json') or not valid_token(suffix):
                refuse()
            path = Path(folder) / name
            read_bytes(path, limit=MANIFEST_LIMIT)
            evidence = private_directory(destination.parent / 'staging-evidence', create=True)
            target = evidence / (sha256(str(path.relative_to(destination.parent)).encode()) + '-' + suffix)
            if target.exists():
                refuse()
            os.rename(path, target)
            sync_directory(path.parent)
            sync_directory(evidence)
    publish(destination / 'manifest.json', manifest, limit=MANIFEST_LIMIT)
    return Archive.load(destination)


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
    guard: int | None = None

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
        if self.guard is not None:
            path = Path(str(self.command.options['state-dir'])) / 'identity.guard'
            private_file(path)
            current, named = os.fstat(self.guard), path.lstat()
            if (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino):
                refuse()

    def begin_attempt(self) -> tuple[Archive, Object, Archive | None]:
        self.check()
        directory = Path(str(self.command.options['state-dir']))
        source = Archive.load(Path(str(self.command.options['source'])), source=True) if self.context['operation'] == 'restore' else None
        operation = self.operation()
        source_hash = source.digest if source is not None else None
        if source_hash != operation['sourceSha256']:
            refuse()
        before = preserve(directory, Path(str(self.command.options['backup'])), self.context, source_hash,
                          extra=[row['path'] for row in source.manifest['files']] if source is not None else None)
        if operation['backupSha256'] is not None and operation['backupSha256'] != before.digest:
            refuse()
        marker = local_marker({'version': 1, 'component': COMPONENT, 'session': self.context['session'],
                               'epoch': self.context['epoch'], 'operation': self.context['operation'],
                               'attempt': self.context['attempt'], 'payloadSha256': self.payload.digest,
                               'generation': self.context['generation'], 'phase': 'pending',
                               'sourceSha256': source_hash, 'backupSha256': before.digest})
        path = directory / 'identity-maintenance.json'
        if path.exists():
            try:
                current = local_marker(read(path))
            except (ValueError, OSError):
                if self.context['operation'] != 'restore':
                    raise
                current = None  # Raw failed bytes are already preserved.
            if current in (marker, {**marker, 'phase': 'complete'}):
                assert current is not None
                marker = current
            else:
                if self.context['operation'] != 'restore':
                    if current is None or current['phase'] != 'complete':
                        refuse()
                    # A new closed-session operation cannot consume an earlier
                    # result. Its exact completed predecessor must have been
                    # durably archived by the provider before pointer replacement.
                    prior = read(self.session_folder / 'attempts' / current['attempt'] / 'result.json')
                    validate('result', prior)
                    for key in ('component', 'session', 'epoch', 'operation', 'attempt', 'payloadSha256', 'generation', 'backupSha256'):
                        if prior[key] != current[key]:
                            refuse()
                    if prior['stateDirectorySha256'] != sha256(str(directory).encode()):
                        refuse()
                publish(path, marker)
        else:
            publish(path, marker)
        return before, marker, source

    def acquire_guard(self) -> Path:
        self.check()
        directory = private_directory(physical(str(self.command.options['state-dir'])))
        try:
            number = state_format(directory)
        except Failure as error:
            if self.context['operation'] != 'restore' or error.result['code'] == 'state_newer':
                raise
            number = None
        if number is None and self.context['operation'] != 'restore':
            raise Failure('invalid_input', 'Maintenance requires existing supported Driver state.')
        if self.guard is None:
            path = directory / 'identity.guard'
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if number == 1 or self.context['operation'] == 'restore':
                flags |= os.O_CREAT
            descriptor = os.open(path, flags, 0o600)
            try:
                private_file(path)
                current, named = os.fstat(descriptor), path.lstat()
                if (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino):
                    refuse()
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.set_inheritable(descriptor, False)
                os.fsync(descriptor)
                sync_directory(directory)
                self.guard = descriptor
            except BaseException:
                os.close(descriptor)
                raise
        self.check()
        return directory

    async def validate_records(self, directory: Path, *, allow_pending: bool = False) -> None:
        # No account record, native home, credential, settings or chat bytes
        # are read. Only private record-file metadata and owned-work records.
        metadata_names(directory)
        subscriptions = directory / 'subscriptions'
        work: list[Path] = []
        if subscriptions.exists():
            for folder in subscriptions.iterdir():
                record = folder / 'record.json'
                if os.path.lexists(record):
                    private_file(record)
                progress = folder / 'sign-in.json'
                if os.path.lexists(progress):
                    work.append(progress)
                for name in ('identity.json', 'identity-pending.json'):
                    path = folder / name
                    if os.path.lexists(path):
                        if name == 'identity-pending.json':
                            pending(read(path))
                            if not allow_pending:
                                raise Failure('internal', 'Subscription identity settlement must be recovered before this operation.')
                        else:
                            sidecar(read(path))
        runs = directory / 'runs'
        if os.path.lexists(runs):
            private_directory(runs)
            work.extend(path for path in runs.iterdir() if path.suffix == '.json' and not path.name.startswith('.'))
        if len(work) > MAX_ENTRIES:
            refuse()
        for path in work:
            value = decode(read_bytes(path, limit=MANIFEST_LIMIT), limit=MANIFEST_LIMIT, exact=False)
            if 'owner' not in value:
                refuse()
            for key in ('owner', 'tool', 'cleanupOwner'):
                if key in value and await owner_alive(parse_owner(value[key])):
                    raise Failure('in_use', 'Driver state still records active owned work; finish its verified cleanup first.')

    def complete(self, directory: Path, marker: Object) -> Object:
        self.check()
        if state_format(directory) != 2 or root_identity(directory) != self.context['generation']:
            refuse()
        if (directory / 'identity-upgrade.json').exists():
            refuse()
        current = local_marker(read(directory / 'identity-maintenance.json'))
        if current not in (marker, {**marker, 'phase': 'complete'}):
            refuse()
        publish(directory / 'identity-maintenance.json', {**marker, 'phase': 'complete'})
        result = self.result()
        self.validate_result(result)
        return result

    def result(self) -> Object:
        maintenance = {key: self.context[key] for key in
                       ('component', 'session', 'epoch', 'operation', 'attempt', 'payloadSha256')}
        maintenance['stateDirectorySha256'] = sha256(str(self.command.options['state-dir']).encode('utf-8'))
        if self.context['operation'] == 'identity-recover':
            maintenance['subscription'] = self.context['subscription']
        result = {'version': 1, 'format': 2, 'generation': self.context['generation'], 'maintenance': maintenance}
        if len(canonical(result)) > LIMIT:
            refuse()
        return result

    def validate_result(self, result: Object) -> None:
        self.check()
        directory = Path(str(self.command.options['state-dir']))
        if result != self.result() or state_format(directory) != 2 or root_identity(directory) != self.context['generation']:
            refuse()
        marker = local_marker(read(directory / 'identity-maintenance.json'))
        expected = {'version': 1, 'component': COMPONENT, 'session': self.context['session'], 'epoch': self.context['epoch'],
                    'operation': self.context['operation'], 'attempt': self.context['attempt'],
                    'payloadSha256': self.payload.digest, 'generation': self.context['generation'], 'phase': 'complete',
                    'sourceSha256': self.operation()['sourceSha256'], 'backupSha256': marker['backupSha256']}
        if marker != expected or (directory / 'identity-upgrade.json').exists():
            refuse()
        archive = Archive.load(Path(str(self.command.options['backup'])) / 'before')
        if archive.digest != marker['backupSha256']:
            refuse()
        for name in metadata_names(directory):
            if not name.endswith('/identity-pending.json'):
                continue
            relevant = self.context['operation'] != 'identity-recover' or name == f"subscriptions/{self.context['subscription']}/identity-pending.json"
            if relevant and os.path.lexists(directory / name):
                refuse()
        # Do not publish provider result/control or release either exclusive
        # description. The registered supervisor owns acknowledgement/reopen.

    def convert(self, directory: Path, archive: Archive, backup: Path) -> None:
        journal_path = directory / 'identity-upgrade.json'
        expected = {'version': 1, 'from': 1, 'to': 2, 'generation': self.context['generation'],
                    'backup': str(backup), 'backupSha256': archive.digest}
        if journal_path.exists():
            journal = read(journal_path)
            fields(journal, 'version from to generation backup backupSha256')
            if journal != expected or type(journal['from']) is not int or type(journal['to']) is not int:
                refuse()
        else:
            if state_format(directory) != 1:
                raise Failure('invalid_input', 'Upgrade requires format 1 or its exact pending conversion journal.')
        # Stage the root and fsync it before the durable format fence.
        staged = prepare_file(directory / 'identity.json', canonical({'version': 1, 'generation': self.context['generation']}).decode())
        try:
            publish(journal_path, expected)
            publish_bytes(directory / 'format', b'2\n')
            os.replace(staged, directory / 'identity.json')
            sync_directory(directory)
            remove(journal_path)
        finally:
            staged.unlink(missing_ok=True)

    async def upgrade(self, directory: Path, before: Archive, marker: Object) -> Object:
        await self.validate_records(directory)
        number = state_format(directory)
        if number == 2 and not (directory / 'identity-upgrade.json').exists():
            if root_identity(directory) != self.context['generation']:
                refuse()
            return self.complete(directory, marker)
        if before.manifest['format'] != 1:
            raise Failure('invalid_input', 'The original upgrade before image must be format 1.')
        self.convert(directory, before, Path(str(self.command.options['backup'])))
        await self.validate_records(directory)
        return self.complete(directory, marker)

    async def reset(self, directory: Path, before: Archive, marker: Object) -> Object:
        if before.manifest['format'] != 2 or state_format(directory) != 2:
            raise Failure('invalid_input', 'Identity reset requires valid format 2; upgrade format 1 first.')
        original = before.content('identity.json')
        if original is None:
            refuse()
        identity = decode(original)
        fields(identity, 'version generation')
        if not valid_token(identity['generation']) or identity['generation'] == self.context['generation']:
            refuse()
        await self.validate_records(directory)
        current = root_identity(directory)
        if current not in (identity['generation'], self.context['generation']):
            refuse()
        publish(directory / 'identity.json', {'version': 1, 'generation': self.context['generation']})
        return self.complete(directory, marker)

    async def restore(self, directory: Path, before: Archive, marker: Object, source: Archive) -> Object:
        destination = Path(str(self.command.options['backup']))
        desired = self.context['generation']
        # A known restore must invalidate both old lineages, including an exact
        # byte-for-byte input. Malformed failed before bytes are only evidence.
        for archive in (source, before):
            content = archive.content('identity.json')
            if content is None:
                continue
            try:
                value = decode(content)
            except ValueError:
                if archive is source:
                    raise
                continue
            if value.get('generation') == desired:
                refuse()
        checkpoint_path = destination / 'restore-checkpoint.json'
        fixed = {'version': 1, 'kind': 'driver-identity-restore', 'attempt': self.context['attempt'],
                 'sourceSha256': source.digest, 'backupSha256': before.digest}
        if checkpoint_path.exists():
            checkpoint = read(checkpoint_path)
            fields(checkpoint, 'version kind attempt sourceSha256 backupSha256 phase conversionBeforeSha256')
            if ({key: value for key, value in checkpoint.items() if key not in ('phase', 'conversionBeforeSha256')} != fixed
                    or checkpoint['phase'] not in ('prepared', 'restored', 'converted', 'complete')
                    or checkpoint['conversionBeforeSha256'] is not None and not valid_hash(checkpoint['conversionBeforeSha256'])):
                refuse()
        else:
            checkpoint = {**fixed, 'phase': 'prepared', 'conversionBeforeSha256': None}
            publish(checkpoint_path, checkpoint)
        if marker['phase'] == 'complete':
            await self.validate_records(directory)
            return self.complete(directory, marker)
        if checkpoint['phase'] == 'prepared':
            abandoned: dict[str, bytes | None] = {}
            for row in source.manifest['files']:
                name = row['path']
                if name in ('identity-upgrade.json', 'identity-maintenance.json') or name.endswith('/identity-pending.json'):
                    abandoned[name] = source.content(name)
            preserve_entry(destination / 'abandoned-source', abandoned)
            names = sorted({row['path'] for row in before.manifest['files']} |
                           {row['path'] for row in source.manifest['files']}, key=lambda name: name.encode())
            # The active marker is the durable restore intent. Historical
            # source markers/journals are archived data, never active authority.
            for name in names:
                if name == 'identity-maintenance.json':
                    continue
                path = directory / metadata_name(name)
                historical = name == 'identity-upgrade.json' or name.endswith('/identity-pending.json')
                content = None if historical else source.content(name)
                if content is None:
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        remove(path)
                else:
                    archive_parent(directory, name)
                    publish_bytes(path, content, limit=MANIFEST_LIMIT)
            checkpoint = {**checkpoint, 'phase': 'restored'}
            publish(checkpoint_path, checkpoint)
        if checkpoint['phase'] == 'restored':
            if source.manifest['format'] == 1:
                conversion_folder = destination / 'conversion-before'
                if checkpoint['conversionBeforeSha256'] is None:
                    if state_format(directory) != 1:
                        refuse()
                    _, restored = snapshot(directory, metadata_names(directory))
                    conversion = preserve_entry(conversion_folder, restored)
                    checkpoint = {**checkpoint, 'conversionBeforeSha256': conversion.digest}
                    publish(checkpoint_path, checkpoint)
                else:
                    conversion = Archive.load(conversion_folder)
                    if conversion.digest != checkpoint['conversionBeforeSha256']:
                        refuse()
                if (directory / 'identity-upgrade.json').exists() or state_format(directory) == 1:
                    self.convert(directory, conversion, conversion_folder)
                elif state_format(directory) != 2 or root_identity(directory) != desired:
                    refuse()
            elif source.manifest['format'] == 2:
                if state_format(directory) != 2:
                    refuse()
                publish(directory / 'identity.json', {'version': 1, 'generation': desired})
            else:
                refuse()
            checkpoint = {**checkpoint, 'phase': 'converted'}
            publish(checkpoint_path, checkpoint)
        await self.validate_records(directory)
        result = self.complete(directory, marker)
        publish(checkpoint_path, {**checkpoint, 'phase': 'complete'})
        return result

    async def recover(self, directory: Path, before: Archive, marker: Object) -> Object:
        identifier = canonical_id(self.context['subscription'])
        name = f'subscriptions/{identifier}/identity-pending.json'
        archived = before.content(name)
        if archived is None:
            raise Failure('invalid_input', 'Identity recovery requires its preserved pending Subscription journal.')
        original = pending(decode(archived))
        if state_format(directory) != 2 or root_identity(directory) != self.context['generation']:
            refuse()
        await self.validate_records(directory, allow_pending=True)
        # This State object supplies only the existing short lock. The verified
        # exclusive component/local guards remain owned by Maintenance; normal
        # initialization or credential/native work is never dispatched here.
        state = State(directory)
        async with state.lock(identifier, allow_pending=True):
            self.check()
            folder = state.subscription(identifier)
            path = folder / 'identity-pending.json'
            if os.path.lexists(path):
                if pending(read(path)) != original:
                    refuse()
            else:
                current = sidecar(read(folder / 'identity.json'))
                if current != {'version': 1, 'witness': original['witness'], 'state': 'retired'}:
                    refuse()
            identity_path = folder / 'identity.json'
            if os.path.lexists(identity_path):
                current = sidecar(read(identity_path))
                if current['state'] == 'active' and current['witness'] != original['witness']:
                    refuse()
            # Preserve record/auth/credential references exactly. Repeating an
            # interrupted retirement is safe; no old active proof can escape.
            publish(identity_path, {'version': 1, 'witness': original['witness'], 'state': 'retired'})
            if os.path.lexists(path):
                remove(path)
        return self.complete(directory, marker)

    async def execute(self) -> Object:
        self.check()
        operation = self.context['operation']
        if operation not in OPERATIONS:
            raise Failure('unsupported', 'This maintenance operation is not available in this source checkpoint.')
        directory = self.acquire_guard()
        if operation == 'identity-reset' and state_format(directory) != 2:
            raise Failure('invalid_input', 'Identity reset requires valid format 2; upgrade format 1 first.')
        if operation == 'upgrade' and state_format(directory) == 2 and not (directory / 'identity-upgrade.json').exists():
            if root_identity(directory) != self.context['generation']:
                refuse()
        if operation == 'identity-recover':
            if state_format(directory) != 2 or root_identity(directory) != self.context['generation']:
                refuse()
            path = directory / 'subscriptions' / self.context['subscription'] / 'identity-pending.json'
            local = directory / 'identity-maintenance.json'
            resumed = local.exists() and local_marker(read(local))['attempt'] == self.context['attempt']
            if not os.path.lexists(path) and not resumed:
                raise Failure('invalid_input', 'Identity recovery requires a pending Subscription journal.')
        before, marker, source = self.begin_attempt()
        if operation == 'upgrade':
            return await self.upgrade(directory, before, marker)
        if operation == 'identity-reset':
            return await self.reset(directory, before, marker)
        if operation == 'identity-recover':
            return await self.recover(directory, before, marker)
        assert source is not None
        return await self.restore(directory, before, marker, source)

    def finish(self) -> None:
        if not self.closed:
            if self.guard is not None:
                os.close(self.guard)
                self.guard = None
            os.close(self.descriptor)
            self.closed = True
