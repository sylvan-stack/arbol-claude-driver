"""Durable format-one/two state and historical process ownership."""
from __future__ import annotations

import asyncio
import os
import re
import stat
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from .core import Failure, compact, epoch, fixed_wait, now, valid_date, loads
from .models import ProcessOwner
from .identity import IdentityState, ensure_directory
from .identity_protocol import sync_directory

if TYPE_CHECKING:
    from .admission import Admission


def private_directory(path: Path) -> None:
    ensure_directory(path)


def safe_metadata_path(path: Path) -> None:
    if path.resolve(strict=False) != path:
        raise Failure('internal', 'Cannot access unsafe Driver metadata.')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise Failure('internal', 'Cannot access unsafe Driver metadata.')


def prepare_file(path: Path, content: str) -> Path:
    safe_metadata_path(path)
    private_directory(path.parent)
    temporary = path.parent / f'.write-{uuid.uuid4()}'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def atomic_write(path: Path, content: str) -> None:
    temporary = prepare_file(path, content)
    try:
        safe_metadata_path(path)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def durable_delete(path: Path) -> None:
    try:
        safe_metadata_path(path)
        path.unlink()
        sync_directory(path.parent)
    except FileNotFoundError:
        pass


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, compact(value) + '\n')


def read_json(path: Path) -> Any:
    try:
        safe_metadata_path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'r', encoding='utf-8') as handle:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                raise Failure('internal', 'Cannot read unsafe Driver metadata.')
            raw = handle.read()
            after, named = os.fstat(descriptor), path.lstat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
                raise Failure('internal', 'Driver metadata changed while being read.')
        return loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Failure('internal', 'Cannot read Driver metadata.') from None


def parse_owner(value: Any) -> ProcessOwner:
    if not isinstance(value, dict) or type(value.get('pid')) is not int or value['pid'] <= 0 or not valid_date(value.get('startedAt')):
        raise Failure('internal', 'A process owner record is malformed.')
    return {'pid': value['pid'], 'startedAt': value['startedAt']}


def ps_time(value: str) -> str:
    stamp = datetime.strptime(' '.join(value.split()), '%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc)
    return now(stamp.timestamp())


async def process_owner(pid: int) -> ProcessOwner | None:
    child = await asyncio.create_subprocess_exec('/bin/ps', '-p', str(pid), '-o', 'lstart=', env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await asyncio.wait_for(child.communicate(), 5)
    if child.returncode == 1:
        return None
    try:
        return {'pid': pid, 'startedAt': ps_time(output.decode().strip())}
    except ValueError:
        raise Failure('internal', f'Cannot read the start time of process {pid}.') from None


async def owner_alive(owner: ProcessOwner) -> bool:
    current = await process_owner(owner['pid'])
    return current is not None and abs(epoch(current['startedAt']) - epoch(owner['startedAt'])) <= 2


class State:
    _open: ClassVar[set[State]] = set()

    def __init__(self, directory: str | Path):
        self.directory = Path(os.path.abspath(directory))
        self.identity: IdentityState | None = None

    def initialize(self, admission: Admission | None = None) -> None:
        if self.identity is not None:
            self.identity.check()
            return
        qualified = admission is not None and not admission.closed
        if admission is not None:
            admission._lease_current()
        self.identity = IdentityState.open(self.directory, qualified=qualified)
        self._open.add(self)

    def close(self) -> None:
        if self.identity is not None:
            self.identity.close()
        self._open.discard(self)

    @classmethod
    def close_all(cls) -> None:
        # The CLI calls this only after asyncio.run has drained all state tasks
        # and completed final output, before closing component admission.
        for state in tuple(cls._open):
            state.close()

    def subscription(self, identifier: str) -> Path:
        if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', identifier):
            raise Failure('invalid_input', 'The Subscription identifier is not a safe file name.')
        path = self.directory / 'subscriptions' / identifier
        if path.resolve(strict=False) != path:
            raise Failure('internal', 'Cannot access an unsafe Subscription directory.')
        if self.identity is not None:
            self.identity.check()
        return path

    def clear_unfinished_writes(self) -> None:
        def visit(folder: Path, depth: int) -> None:
            if not folder.exists():
                return
            for path in folder.iterdir():
                if path.is_file() and path.name.startswith('.write-'):
                    try:
                        if time.time() - path.stat().st_mtime > 60:
                            path.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
                elif depth and path.is_dir() and not path.is_symlink() and path.name not in {'home', 'tmp'}:
                    visit(path, depth - 1)
        visit(self.directory, 3)

    @asynccontextmanager
    async def lock(self, identifier: str, *, allow_pending: bool = False) -> AsyncIterator[None]:
        directory = self.subscription(identifier)
        private_directory(directory)
        path = directory / 'lock'
        owner = await process_owner(os.getpid())
        if owner is None:
            raise Failure('internal', 'Cannot identify the Driver process.')
        temporary = prepare_file(path, compact({'owner': owner}) + '\n')
        deadline = time.monotonic() + fixed_wait(10000)
        acquired = False
        try:
            while not acquired:
                try:
                    os.link(temporary, path)
                    acquired = True
                except FileExistsError:
                    try:
                        with path.open() as handle:
                            snapshot = os.fstat(handle.fileno())
                            existing = loads(handle.read())
                        taken = parse_owner(existing.get('owner') if isinstance(existing, dict) else None)
                    except FileNotFoundError:
                        continue
                    except ValueError:
                        raise Failure('internal', f'Malformed lock {path}.') from None
                    if not await owner_alive(taken):
                        reclaim = directory / f'.write-reap-{snapshot.st_ino}'
                        reaping = False
                        try:
                            os.link(temporary, reclaim)
                            reaping = True
                            if path.stat().st_ino == snapshot.st_ino:
                                path.unlink()
                        except FileExistsError:
                            reaper = read_json(reclaim)
                            if isinstance(reaper, dict) and not await owner_alive(parse_owner(reaper.get('owner'))):
                                reclaim.unlink(missing_ok=True)
                        except FileNotFoundError:
                            pass
                        finally:
                            if reaping:
                                reclaim.unlink(missing_ok=True)
                    if time.monotonic() >= deadline:
                        raise Failure('internal', f'Subscription lock timed out after {fixed_wait(10000)*1000:g} milliseconds; owner PID {taken["pid"]}, started {taken["startedAt"]}.')
                    await asyncio.sleep(fixed_wait(50))
            if self.identity is not None:
                self.identity.check()
                if not allow_pending and self.identity.read_pending(identifier) is not None:
                    raise Failure('internal', 'Subscription identity settlement requires recovery under exclusive maintenance.')
            yield
        finally:
            if acquired:
                path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
