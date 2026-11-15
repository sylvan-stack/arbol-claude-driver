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
from .identity import canonical_id, pending, sidecar
from .identity_protocol import (
    Authority, MANIFEST_LIMIT, MAX_ENTRIES, Object, Payload, canonical, canonical_uuid, decode, fields, natural, physical, private_directory,
    publish, publish_bytes, read, read_bytes, refuse, sha256, sync_directory, valid_hash, valid_token, validate,
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
            if manifest['format'] not in (1, 2):
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

    def content(self, name: str) -> bytes | None:
        row = next((row for row in self.manifest['files'] if row['path'] == name), None)
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

    async def execute(self) -> Object:
        self.check()
        # Exact operation implementations follow as separately owned leaves;
        # this reserved dispatch never falls back to ordinary state admission.
        raise Failure('unsupported', 'This maintenance operation is not available in this source checkpoint.')

    def finish(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True
