"""Format-1 durable state and inter-process ownership."""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Failure, compact, epoch, fixed_wait, now, valid_date, loads
from .models import ProcessOwner


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def prepare_file(path: Path, content: str) -> Path:
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
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, compact(value) + '\n')


def read_json(path: Path) -> Any:
    try:
        return loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Failure('internal', f'Cannot read state file {path}.') from None


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
    def __init__(self, directory: str | Path):
        self.directory = Path(os.path.abspath(directory))

    def initialize(self) -> None:
        path = self.directory / 'format'

        def check() -> bool:
            try:
                value = path.read_text().strip()
            except FileNotFoundError:
                return False
            if not re.fullmatch('[1-9][0-9]*', value):
                raise Failure('internal', f'State format in {path} is malformed.')
            if int(value) > 1:
                raise Failure('state_newer', f'State format {value} is newer than supported format 1.', {'remedy': 'Run the Build that wrote this state, or a newer one.'})
            return True

        if check():
            private_directory(self.directory)
            path.chmod(0o600)
            return
        private_directory(self.directory)
        temp = prepare_file(path, '1\n')
        try:
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
        finally:
            temp.unlink()
        check()

    def subscription(self, identifier: str) -> Path:
        if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', identifier):
            raise Failure('invalid_input', 'The Subscription identifier is not a safe file name.')
        return self.directory / 'subscriptions' / identifier

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
    async def lock(self, identifier: str) -> AsyncIterator[None]:
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
            yield
        finally:
            if acquired:
                path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
