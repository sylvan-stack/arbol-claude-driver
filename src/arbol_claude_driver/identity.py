"""Local format-two identity and lifetime guard, independent of credentials.

A normal invocation owns its independently opened guard until final cleanup and
output. Maintenance reuses this stable inode; no route unlinks a live guard.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .core import Failure
from .identity_protocol import Object, fields, publish, publish_bytes, read, remove, sync_directory, token, valid_token

_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z', re.ASCII)


def canonical_id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise Failure('invalid_input', 'The Subscription identifier is not a safe file name.')
    return value


def parse_expected(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    parts = value.split('.')
    if len(value) != 87 or len(parts) != 2 or not all(valid_token(item) for item in parts):
        raise Failure('invalid_input', 'Give --expect exactly one canonical generation.witness pair.')
    return parts[0], parts[1]


def unsafe() -> Failure:
    return Failure('internal', 'Driver identity state is unsafe or incomplete; preserve it for exclusive maintenance.')


def ensure_directory(path: Path) -> None:
    """Create private owned descendants, refusing every symlink component."""
    path = Path(os.path.abspath(path))
    if path.resolve(strict=False) != path:
        raise unsafe()
    try:
        info = path.lstat()
    except FileNotFoundError:
        if path.parent == path:
            raise unsafe() from None
        if not path.parent.exists():
            ensure_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
            sync_directory(path.parent)
        except FileExistsError:
            pass
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise unsafe()
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
        sync_directory(path)
        sync_directory(path.parent)


def private_file(path: Path) -> None:
    if path.resolve(strict=False) != path:
        raise unsafe()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise unsafe()


def state_format(directory: Path) -> int | None:
    path = directory / 'format'
    if path.resolve(strict=False) != path:
        raise unsafe()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 4096:
            raise unsafe()
        raw = os.read(descriptor, 4097)
        after, named = os.fstat(descriptor), path.lstat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino) or len(raw) != before.st_size):
            raise unsafe()
    finally:
        os.close(descriptor)
    try:
        spelling = raw.decode('ascii').strip()
    except UnicodeError:
        raise unsafe() from None
    if not re.fullmatch(r'[1-9][0-9]*', spelling, re.ASCII):
        raise unsafe()
    if spelling not in ('1', '2'):
        raise Failure('state_newer', 'Driver state is newer than supported format 2.',
                      {'remedy': 'Run the Build that wrote this state, or a newer one.'})
    if spelling == '2':
        private_file(path)
    return int(spelling)


def root_identity(directory: Path) -> str:
    value = read(directory / 'identity.json')
    fields(value, 'version generation')
    if not valid_token(value['generation']):
        raise unsafe()
    return str(value['generation'])


def sidecar(value: Object) -> Object:
    fields(value, 'version witness state')
    if not valid_token(value['witness']) or value['state'] not in ('active', 'retired'):
        raise unsafe()
    return value


def pending(value: Object) -> Object:
    fields(value, 'version operation witness phase')
    if (value['operation'] not in ('create', 'remove') or not valid_token(value['witness'])
            or value['phase'] not in ('prepared', 'committed')):
        raise unsafe()
    return value


@dataclass
class IdentityState:
    directory: Path
    descriptor: int
    format: int
    generation: str | None
    closed: bool = False

    @classmethod
    def open(cls, directory: Path, *, qualified: bool) -> IdentityState:
        # Qualified startup has already verified the component lease. Legacy
        # format-one access uses this extra local guard but gains no proof.
        # This preflight can only refuse unsupported state before mkdir/chmod/
        # guard creation. It grants no authority: format is read again under
        # the guard before any root identity or ordinary state access.
        preflight = state_format(directory)
        if preflight != 1 and not qualified:
            raise Failure('internal', 'Identity state requires qualified normal admission.')
        ensure_directory(directory)
        guard = directory / 'identity.guard'
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if preflight != 2:
            flags |= os.O_CREAT
        # A missing guard in existing format2 cannot be replaced: another
        # invocation could still hold the unlinked inode. Preserve/refuse it.
        descriptor = os.open(guard, flags, 0o600)
        try:
            private_file(guard)
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (guard.lstat().st_dev, guard.lstat().st_ino):
                raise unsafe()
            os.set_inheritable(descriptor, False)
            os.fsync(descriptor)
            sync_directory(directory)
            cls._lock(descriptor, exclusive=False)
            number = state_format(directory)
            if number is None:
                # Upgrade only this owned description to exclusive, then read
                # again: another new invocation may have initialized first.
                cls._lock(descriptor, exclusive=True)
                number = state_format(directory)
                if number is None:
                    if not qualified or any(path.name != 'identity.guard' for path in directory.iterdir()):
                        raise unsafe()
                    fresh_generation = token()
                    # Fence old writers before any proof publication. A crash
                    # leaves format2/incomplete root refused, never regenerated.
                    publish_bytes(directory / 'format', b'2\n')
                    publish(directory / 'identity.json', {'version': 1, 'generation': fresh_generation})
                    number = 2
                cls._lock(descriptor, exclusive=False)
            if number == 2 and not qualified:
                raise Failure('internal', 'Format-2 state requires qualified normal admission.')
            if number == 1:
                # Legacy initialize hardened this marker after validating its
                # supported format; do the same without claiming a proof.
                (directory / 'format').chmod(0o600)
                sync_directory(directory)
            generation = root_identity(directory) if number == 2 else None
            result = cls(directory, descriptor, number, generation)
            result.check()
            return result
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _lock(descriptor: int, *, exclusive: bool) -> None:
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Failure('in_use', 'Driver state is held for maintenance.') from None
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def check(self) -> None:
        if self.closed:
            raise unsafe()
        guard = self.directory / 'identity.guard'
        private_file(guard)
        current, named = os.fstat(self.descriptor), guard.lstat()
        if ((current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)
                or state_format(self.directory) != self.format):
            raise unsafe()
        # Ordinary access never resumes or clears administrative journals.
        for name in ('identity-upgrade.json', 'identity-maintenance.json'):
            try:
                (self.directory / name).lstat()
            except FileNotFoundError:
                continue
            raise unsafe()
        if self.format == 2 and root_identity(self.directory) != self.generation:
            raise unsafe()

    def require_proof(self) -> str:
        self.check()
        if self.format == 1:
            raise Failure('invalid_input', 'Subscription identity requires upgraded Driver state.',
                          {'remedy': 'Upgrade Driver state under exclusive maintenance before using Subscription identity.'})
        if self.generation is None:
            raise unsafe()
        return self.generation

    def subscription(self, identifier: str) -> Path:
        return self.directory / 'subscriptions' / canonical_id(identifier)

    def read_sidecar(self, identifier: str) -> Object | None:
        self.require_proof()
        try:
            return sidecar(read(self.subscription(identifier) / 'identity.json'))
        except FileNotFoundError:
            return None

    def read_pending(self, identifier: str) -> Object | None:
        self.check()
        if self.format != 2:
            return None
        try:
            return pending(read(self.subscription(identifier) / 'identity-pending.json'))
        except FileNotFoundError:
            return None

    def publish_sidecar(self, identifier: str, witness: str, state: str) -> None:
        self.require_proof()
        value = sidecar({'version': 1, 'witness': witness, 'state': state})
        publish(self.subscription(identifier) / 'identity.json', value)

    def journal(self, identifier: str, operation: str, witness: str, phase: str = 'prepared') -> Object:
        self.require_proof()
        value = pending({'version': 1, 'operation': operation, 'witness': witness, 'phase': phase})
        publish(self.subscription(identifier) / 'identity-pending.json', value)
        return value

    def clear_journal(self, identifier: str) -> None:
        self.require_proof()
        remove(self.subscription(identifier) / 'identity-pending.json')

    def finish_create(self, identifier: str, witness: str) -> None:
        self.publish_sidecar(identifier, witness, 'active')
        self.journal(identifier, 'create', witness, 'committed')
        self.clear_journal(identifier)

    def finish_remove(self, identifier: str, witness: str) -> None:
        self.publish_sidecar(identifier, witness, 'retired')
        remove(self.subscription(identifier) / 'record.json')
        self.journal(identifier, 'remove', witness, 'committed')
        self.clear_journal(identifier)

    def capture(self, identifier: str, expected: tuple[str, str] | None = None) -> Object:
        """Caller holds the canonical short lock and has validated its record."""
        generation = self.require_proof()
        if self.read_pending(identifier) is not None:
            raise unsafe()
        current = self.read_sidecar(identifier)
        if current is None or current['state'] == 'retired':
            witness = token()
            self.publish_sidecar(identifier, witness, 'active')
        else:
            witness = str(current['witness'])
        # Re-read durable bytes before emitting any positive proof. The short
        # lock serializes capture/compare with local create/remove settlement.
        if self.read_sidecar(identifier) != {'version': 1, 'witness': witness, 'state': 'active'}:
            raise unsafe()
        self.check()
        if expected is not None and expected != (generation, witness):
            raise Failure('in_use', 'Subscription identity changed.',
                          {'remedy': 'Select the Subscription again and explicitly rebind it.'})
        return {'version': 1, 'driver': 'claude', 'subscription': canonical_id(identifier),
                'generation': generation, 'witness': witness}

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True
