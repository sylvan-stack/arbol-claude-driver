"""Cooperative main/helper metadata-writer admission, before Driver state access.

An independently opened lease and durable writer obligation outlive the socket
handshake. EOF, dead parents and closed leases never erase unknown custody.
"""
from __future__ import annotations

import os
import platform
import pwd
import re
import socket
import stat
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .identity_protocol import (
    BUILD, COMPONENTS, DESCRIPTOR, LIMIT, Authority, Object, Payload, canonical,
    canonical_uuid, decode, fields, identity_record, load_payload, physical, private_directory, process_identity,
    publish, read, read_bytes, refuse, relative, sha256,
    token, valid_hash, valid_token, validate,
)

COMPONENT = 'arbol-claude-driver'
NORMAL_FD = 'ARBOL_DRIVER_ADMISSION_FD'
NORMAL_CONTEXT = 'ARBOL_DRIVER_ADMISSION_CONTEXT'
MAINTENANCE_FD = 'ARBOL_DRIVER_MAINTENANCE_FD'
MAINTENANCE_CONTEXT = 'ARBOL_DRIVER_MAINTENANCE_CONTEXT'
AUTHORITY_ENV = (NORMAL_FD, NORMAL_CONTEXT, MAINTENANCE_FD, MAINTENANCE_CONTEXT)
_CURRENT: Admission | None = None


def current_admission() -> Admission:
    if _CURRENT is None or _CURRENT.closed:
        refuse()
    return _CURRENT


def without_authority(environment: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key not in AUTHORITY_ENV}


def descriptor_number(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r'[1-9][0-9]{0,8}', value) is None or int(value) < 3:
        refuse()
    return int(value)


def boot_uuid() -> str:
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], check=True,
                            capture_output=True, timeout=5, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    value = result.stdout.decode('ascii', 'strict').strip().lower()
    if not canonical_uuid(value):
        refuse()
    return value


def _identity(value: object) -> Object:
    return identity_record(value)


def normal_context(value: Object) -> None:
    fields(value, 'version kind role registry registrySha256 component epoch boot invocation writer parent process handoff nonce payloadSha256 acceptanceSha256 entry')
    if value['kind'] != 'driver-admission-context' or value['component'] != COMPONENT or value['role'] not in ('main', 'helper'):
        refuse()
    for key in ('epoch', 'invocation', 'writer', 'handoff', 'nonce'):
        if not valid_token(value[key]):
            refuse()
    for key in ('registrySha256', 'payloadSha256', 'acceptanceSha256'):
        if not valid_hash(value[key]):
            refuse()
    if not canonical_uuid(value['boot']) or not isinstance(value['registry'], str):
        refuse()
    relative(value['entry'])
    _identity(value['process'])
    parent = value['parent']
    if not isinstance(parent, dict) or set(parent) != {'pid', 'startedAt', 'writer'} or not valid_token(parent['writer']):
        refuse()
    _identity({key: parent[key] for key in ('pid', 'startedAt')})


def _writer_path(authority: Authority, invocation: str, writer: str) -> Path:
    if not valid_token(invocation) or not valid_token(writer):
        refuse()
    return authority.folder / 'writers' / invocation / (writer + '.json')


def _writer(authority: Authority, invocation: str, writer: str) -> Object:
    value = read(_writer_path(authority, invocation, writer))
    validate('writer', value)
    if value['invocation'] != invocation or value['writer'] != writer or value['component'] != authority.component:
        refuse()
    return value


def _handoff_path(authority: Authority, handoff: str) -> Path:
    if not valid_token(handoff):
        refuse()
    return authority.folder / 'handoffs' / (handoff + '.json')


def _handoff(authority: Authority, context: Object, digest: str, phase: str) -> Object:
    value = read(_handoff_path(authority, context['handoff']))
    validate('handoff', value)
    expected = {'version': 1, 'handoff': context['handoff'], 'component': COMPONENT,
                'invocation': context['invocation'], 'parent': context['parent']['writer'],
                'child': context['writer'], 'epoch': context['epoch'], 'payloadSha256': context['payloadSha256'],
                'phase': phase, 'nonce': context['nonce'], 'contextSha256': digest,
                'role': context['role'], 'entry': context['entry'], 'acceptanceSha256': context['acceptanceSha256']}
    if phase == 'accepted':
        expected['challenge'] = value.get('challenge')
    if value != expected:
        refuse()
    return value


def verify_acceptance(authority: Authority, payload: Payload, acceptance_digest: str) -> Object:
    """Resolve only the fixed private registry authority, never a payload declaration."""
    qualification = authority.read('qualification')
    if qualification != {'version': 1, 'component': COMPONENT, 'acceptanceSha256': acceptance_digest}:
        refuse()
    raw = read_bytes(authority.shared / 'acceptances' / (acceptance_digest + '.json'))
    acceptance = decode(raw)
    fields(acceptance, 'version kind status scope contract providerPayloadSha256 producerPayloadSha256 gateSha256 platform evidenceSha256')
    if (sha256(raw) != acceptance_digest or acceptance['kind'] != 'driver-qualification'
            or acceptance['status'] not in ('candidate', 'qualified')
            or acceptance['scope'] != str(authority.registry) or acceptance['contract'] != 'DRV-browser-identity-1'
            or acceptance['producerPayloadSha256'] != payload.digest):
        refuse()
    for name in ('providerPayloadSha256', 'producerPayloadSha256', 'gateSha256', 'evidenceSha256'):
        if not valid_hash(acceptance[name]):
            refuse()
    if acceptance['platform'] != {'system': 'Darwin', 'release': platform.release(), 'machine': platform.machine()} or platform.system() != 'Darwin':
        refuse()
    installed = Path(pwd.getpwuid(os.getuid()).pw_dir) / 'Library/Application Support/Arbol/builds'
    if acceptance['status'] == 'candidate' and authority.registry == installed:
        refuse()
    gate_raw = read_bytes(authority.shared / 'gate.json')
    gate = decode(gate_raw)
    fields(gate, 'version kind providerPayloadSha256 components')
    if (sha256(gate_raw) != acceptance['gateSha256'] or gate['kind'] != 'driver-gate'
            or gate['providerPayloadSha256'] != acceptance['providerPayloadSha256']
            or not isinstance(gate['components'], list) or len(gate['components']) != len(COMPONENTS)):
        refuse()
    for name, row in zip(COMPONENTS, gate['components'], strict=True):
        if not isinstance(row, dict) or set(row) != {'component', 'launcher', 'launcherSha256'} or row['component'] != name or not valid_hash(row['launcherSha256']):
            refuse()
        launcher = physical(row['launcher'], directory=False)
        info = launcher.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022 or not info.st_mode & 0o111:
            refuse()
        # Launchers need executable modes, unlike private protocol documents.
        fd = os.open(launcher, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                import hashlib
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            current = os.fstat(fd)
            named = launcher.lstat()
            if (digest != row['launcherSha256'] or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) !=
                    (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)):
                refuse()
        finally:
            os.close(fd)
    provider_folder = private_directory(authority.registry / 'arbol')
    candidates = list(provider_folder.iterdir())
    if len(candidates) > 100000:
        refuse()
    matched = 0
    for build_folder in candidates:
        if BUILD.fullmatch(build_folder.name) is None:
            continue
        root = build_folder / 'driver-payload'
        if not (root / DESCRIPTOR).exists():
            continue
        descriptor_raw = read_bytes(root / DESCRIPTOR)
        if sha256(descriptor_raw) == gate['providerPayloadSha256']:
            provider = load_payload(root, 'arbol', build_folder.name)
            if provider.root != authority.registry / 'arbol' / provider.build / 'driver-payload':
                refuse()
            matched += 1
    if matched != 1:
        refuse()
    return acceptance


class Frames:
    def __init__(self, descriptor: int):
        self.socket = socket.socket(fileno=descriptor)
        if self.socket.family != socket.AF_UNIX or self.socket.type != socket.SOCK_STREAM:
            refuse()
        os.set_inheritable(descriptor, False)
        self.deadline = time.monotonic() + 20

    def _timeout(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            refuse()
        self.socket.settimeout(remaining)

    def _receive(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            self._timeout()
            part = self.socket.recv(size - len(result))
            if not part:
                refuse()
            result.extend(part)
        return bytes(result)

    def receive(self) -> Object:
        length, = struct.unpack('!I', self._receive(4))
        if not 0 < length <= LIMIT:
            refuse()
        return decode(self._receive(length))

    def send(self, value: Object) -> None:
        raw = canonical(value)
        if len(raw) > LIMIT:
            refuse()
        self._timeout()
        self.socket.sendall(struct.pack('!I', len(raw)) + raw)

    def no_extra(self) -> None:
        self.socket.setblocking(False)
        try:
            if self.socket.recv(1, socket.MSG_PEEK):
                refuse()
        except BlockingIOError:
            pass

    def close(self) -> None:
        self.socket.close()


@dataclass
class Admission:
    authority: Authority
    payload: Payload
    context: Object
    context_digest: str
    context_file: Path
    lease: int
    closed: bool = False
    transfers: set[str] = field(default_factory=set)

    def _lease_current(self) -> None:
        current = os.fstat(self.lease)
        named = (self.authority.folder / 'admission.lock').lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o600
                or (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)):
            refuse()

    def _check(self) -> None:
        context = self.context
        if sha256(read_bytes(self.context_file)) != self.context_digest:
            refuse()
        control = self.authority.read('control')
        allowed = ('normal',) if context['role'] == 'main' else ('normal', 'draining')
        if control['epoch'] != context['epoch'] or control['phase'] not in allowed or boot_uuid() != context['boot']:
            refuse()
        deployment_path = self.authority.shared / 'deployment.json'
        if context['role'] == 'main' and deployment_path.exists():
            deployment = read(deployment_path)
            validate('deployment', deployment)
            if deployment['phase'] != 'complete':
                refuse()
        if sha256(canonical(self.authority.identity)) != context['registrySha256']:
            refuse()
        if context['process'] != process_identity(os.getpid()) or context['parent']['pid'] != os.getppid():
            refuse()
        parent_identity = {key: context['parent'][key] for key in ('pid', 'startedAt')}
        if process_identity(context['parent']['pid']) != parent_identity:
            refuse()
        parent = _writer(self.authority, context['invocation'], context['parent']['writer'])
        if (parent['phase'] != 'active' or parent['epoch'] != context['epoch'] or parent['boot'] != context['boot']
                or parent['payloadSha256'] != self.payload.digest
                or {key: parent[key] for key in ('pid', 'startedAt')} != parent_identity):
            refuse()
        self._lease_current()
        verify_acceptance(self.authority, self.payload, context['acceptanceSha256'])

    @classmethod
    def receive(cls, payload: Payload, entry: str, environment: dict[str, str]) -> Admission:
        if (any(key in environment for key in (MAINTENANCE_FD, MAINTENANCE_CONTEXT))
                or not all(key in environment for key in (NORMAL_FD, NORMAL_CONTEXT))):
            refuse()
        frames = Frames(descriptor_number(environment[NORMAL_FD]))
        lease = -1
        try:
            offer = frames.receive()
            fields(offer, 'version type contextSha256')
            if offer['type'] != 'offer' or not valid_hash(offer['contextSha256']):
                refuse()
            context_file = physical(environment[NORMAL_CONTEXT], directory=False)
            raw = read_bytes(context_file)
            context = decode(raw)
            normal_context(context)
            digest = sha256(raw)
            authority = Authority(physical(context['registry']), COMPONENT)
            if (not context_file.is_relative_to(authority.folder)
                    or digest != offer['contextSha256'] or context['payloadSha256'] != payload.digest
                    or context['entry'] != entry or payload.component != COMPONENT
                    or payload.root != authority.registry / COMPONENT / payload.build / 'driver-payload'
                    or (context['role'] == 'main' and entry != payload.descriptor['entry'])
                    or (context['role'] == 'helper' and entry not in payload.descriptor['helpers'])):
                refuse()
            with authority.mutex():
                lease = authority.lease()
                admission = cls(authority, payload, context, digest, context_file, lease)
                admission._check()
                _handoff(authority, context, digest, 'pending')
                path = _writer_path(authority, context['invocation'], context['writer'])
                if path.exists():
                    refuse()
                writer = {'version': 1, 'invocation': context['invocation'], 'writer': context['writer'],
                          'parent': context['parent']['writer'], 'component': COMPONENT, 'epoch': context['epoch'],
                          'payloadSha256': payload.digest, 'boot': context['boot'], **context['process'],
                          'phase': 'active', 'contextSha256': digest}
                validate('writer', writer)
                publish(path, writer)
            challenge = token()
            frame = {'version': 1, 'handoff': context['handoff'], 'nonce': context['nonce'],
                     'contextSha256': digest, 'challenge': challenge}
            frames.send({**frame, 'type': 'ready'})
            go = frames.receive()
            fields(go, 'version type handoff nonce contextSha256 challenge')
            if go != {**frame, 'type': 'go'}:
                refuse()
            with authority.mutex():
                admission._check()
                accepted = _handoff(authority, context, digest, 'accepted')
                if accepted['challenge'] != challenge or _writer(authority, context['invocation'], context['writer']) != writer:
                    refuse()
            frames.no_extra()
            frames.send({**frame, 'type': 'started'})
            global _CURRENT
            _CURRENT = admission
            return admission
        except BaseException:
            # A published active obligation deliberately remains unresolved.
            if lease >= 0:
                os.close(lease)
            raise
        finally:
            frames.close()

    def finish(self) -> None:
        """Call only after all state tasks, cleanup and final output have finished."""
        if self.closed:
            return
        if self.transfers:
            refuse()
        with self.authority.mutex():
            self._lease_current()
            control = self.authority.read('control')
            if control['epoch'] != self.context['epoch'] or control['phase'] not in ('normal', 'draining'):
                refuse()
            current = _writer(self.authority, self.context['invocation'], self.context['writer'])
            if (current['phase'] != 'active' or current['contextSha256'] != self.context_digest
                    or {key: current[key] for key in ('pid', 'startedAt')} != self.context['process']):
                refuse()
            for path in (self.authority.folder / 'handoffs').iterdir():
                if path.name.endswith('.context.json') or not path.name.endswith('.json'):
                    continue
                handoff = read(path)
                validate('handoff', handoff)
                if handoff['parent'] == self.context['writer']:
                    if handoff['phase'] != 'accepted':
                        refuse()
                    child = _writer(self.authority, handoff['invocation'], handoff['child'])
                    if child['contextSha256'] != handoff['contextSha256']:
                        refuse()
            publish(_writer_path(self.authority, self.context['invocation'], self.context['writer']),
                    {**current, 'phase': 'terminal'})
        os.close(self.lease)
        self.closed = True

    def prepare_helper(self, entry: str) -> HelperHandoff:
        """Preregister while admitted; the caller must spawn only this immutable helper."""
        if self.closed or entry not in self.payload.descriptor['helpers']:
            refuse()
        handoff, child, nonce = token(), token(), token()
        with self.authority.mutex():
            self._lease_current()
            control = self.authority.read('control')
            current = _writer(self.authority, self.context['invocation'], self.context['writer'])
            if (control['epoch'] != self.context['epoch'] or control['phase'] not in ('normal', 'draining')
                    or current['phase'] != 'active' or current['contextSha256'] != self.context_digest
                    or boot_uuid() != self.context['boot']):
                refuse()
            pending = {'version': 1, 'handoff': handoff, 'component': COMPONENT,
                       'invocation': self.context['invocation'], 'parent': self.context['writer'],
                       'child': child, 'epoch': self.context['epoch'], 'payloadSha256': self.payload.digest,
                       'phase': 'pending', 'nonce': nonce, 'contextSha256': None, 'role': 'helper',
                       'entry': entry, 'acceptanceSha256': self.context['acceptanceSha256']}
            validate('handoff', pending)
            publish(_handoff_path(self.authority, handoff), pending)
            self.transfers.add(handoff)
        parent_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        return HelperHandoff(self, pending, Frames(parent_socket.detach()), child_socket)


@dataclass
class HelperHandoff:
    """One-use parent endpoint. Failed or abandoned spawn stays a durable obligation."""
    parent: Admission
    pending: Object
    frames: Frames
    child_socket: socket.socket
    completed: bool = False

    @property
    def context_path(self) -> Path:
        return self.parent.authority.folder / 'handoffs' / (str(self.pending['handoff']) + '.context.json')

    @property
    def prefix(self) -> list[str]:
        return self.parent.payload.helper_prefix(self.pending['entry'])

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.child_socket.fileno(),)

    def environment(self, inherited: dict[str, str]) -> dict[str, str]:
        return {**without_authority(inherited), NORMAL_FD: str(self.child_socket.fileno()), NORMAL_CONTEXT: str(self.context_path)}

    def acknowledge(self, pid: int) -> None:
        """After exact direct spawn. The parent retains its own lease on every failure."""
        if self.completed or self.pending['handoff'] not in self.parent.transfers:
            refuse()
        parent = self.parent
        self.child_socket.close()
        try:
            context = {'version': 1, 'kind': 'driver-admission-context', 'role': 'helper',
                       'registry': str(parent.authority.registry), 'registrySha256': sha256(canonical(parent.authority.identity)),
                       'component': COMPONENT, 'epoch': self.pending['epoch'], 'boot': parent.context['boot'],
                       'invocation': self.pending['invocation'], 'writer': self.pending['child'],
                       'parent': {'writer': parent.context['writer'], **process_identity(os.getpid())},
                       'process': process_identity(pid), 'handoff': self.pending['handoff'],
                       'nonce': self.pending['nonce'], 'payloadSha256': parent.payload.digest,
                       'acceptanceSha256': self.pending['acceptanceSha256'], 'entry': self.pending['entry']}
            normal_context(context)
            digest = sha256(canonical(context))
            with parent.authority.mutex():
                current = read(_handoff_path(parent.authority, self.pending['handoff']))
                if current != self.pending or self.context_path.exists():
                    refuse()
                publish(self.context_path, context)
                self.pending = {**self.pending, 'contextSha256': digest}
                publish(_handoff_path(parent.authority, self.pending['handoff']), self.pending)
            self.frames.send({'version': 1, 'type': 'offer', 'contextSha256': digest})
            ready = self.frames.receive()
            fields(ready, 'version type handoff nonce contextSha256 challenge')
            frame = {'version': 1, 'handoff': context['handoff'], 'nonce': context['nonce'],
                     'contextSha256': digest, 'challenge': ready['challenge']}
            if ready != {**frame, 'type': 'ready'} or not valid_token(ready['challenge']):
                refuse()
            with parent.authority.mutex():
                control = parent.authority.read('control')
                own = _writer(parent.authority, context['invocation'], parent.context['writer'])
                child = _writer(parent.authority, context['invocation'], context['writer'])
                expected = {'version': 1, 'invocation': context['invocation'], 'writer': context['writer'],
                            'parent': parent.context['writer'], 'component': COMPONENT, 'epoch': context['epoch'],
                            'payloadSha256': parent.payload.digest, 'boot': context['boot'], **context['process'],
                            'phase': 'active', 'contextSha256': digest}
                if (control['phase'] not in ('normal', 'draining') or control['epoch'] != context['epoch']
                        or boot_uuid() != context['boot'] or own['phase'] != 'active'
                        or own['contextSha256'] != parent.context_digest or child != expected
                        or process_identity(pid) != context['process']):
                    refuse()
                _handoff(parent.authority, context, digest, 'pending')
                accepted = {**self.pending, 'phase': 'accepted', 'challenge': ready['challenge']}
                validate('handoff', accepted)
                publish(_handoff_path(parent.authority, context['handoff']), accepted)
            self.frames.send({**frame, 'type': 'go'})
            started = self.frames.receive()
            fields(started, 'version type handoff nonce contextSha256 challenge')
            if started != {**frame, 'type': 'started'}:
                refuse()
            self.frames.no_extra()
            with parent.authority.mutex():
                if _handoff(parent.authority, context, digest, 'accepted') != accepted:
                    refuse()
                registered = _writer(parent.authority, context['invocation'], context['writer'])
                if registered != expected and registered != {**expected, 'phase': 'terminal'}:
                    refuse()
            parent.transfers.remove(context['handoff'])
            self.completed = True
        finally:
            self.frames.close()
