"""Bounded subprocess transport and verified process-group cleanup."""
from __future__ import annotations

import asyncio
import os
import pwd
import re
import shlex
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .core import Env, Failure, Record, clean_environment, epoch, fixed_wait, now
from .state import private_directory, ps_time, read_json, write_json

OLDEST_SUPPORTED = '2.1.289'
ACTUAL_HOME = pwd.getpwuid(os.getuid()).pw_dir


def python_launcher(path: Path, code: str, interpreter: str | None = None) -> None:
    """Exec the selected interpreter even when its packaged path has spaces."""
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(interpreter or sys.executable) + ' -I -c ' + shlex.quote(code) + ' "$@"\n')
    path.chmod(0o700)


def kill_group(pid: int, name: signal.Signals) -> None:
    try:
        os.killpg(pid, name)
    except ProcessLookupError:
        pass


async def spawn(program: str, args: list[str], env: Env, cwd: str | Path) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(program, *args, env=env, cwd=cwd, start_new_session=True, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024)
    except OSError:
        raise Failure('tool_failed', 'The child command could not start.') from None


async def wait_exit(child: asyncio.subprocess.Process, timeout: float) -> None:
    # Process.wait also waits for pipe closure; detached descendants can retain
    # those pipes even after the actual vendor process has exited.
    deadline = time.monotonic() + timeout
    while child.returncode is None:
        if time.monotonic() >= deadline:
            raise TimeoutError
        await asyncio.sleep(min(.005, max(.001, deadline - time.monotonic())))


def close_pipes(child: asyncio.subprocess.Process) -> None:
    cast(asyncio.SubprocessTransport, getattr(child, '_transport')).close()


async def wait_cancelled(cancel: asyncio.Event, operation: asyncio.Task[Record], timeout: float) -> Record:
    stopped = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait([operation, stopped], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            raise Failure('tool_failed', 'The operation was stopped.')
        if operation not in done:
            raise Failure('tool_failed', 'The child command timed out.')
        return await operation
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)


async def child_command(program: str, args: list[str], env: Env, cwd: str | Path, timeout: float = 20, cancel: asyncio.Event | None = None, input_text: str | None = None, capture_stderr: bool = False, on_output: Callable[[str], None] | None = None) -> Record:
    child = await spawn(program, args, env, cwd)
    output = bytearray()
    errors = bytearray()
    size = 0

    async def read(reader: asyncio.StreamReader | None, target: bytearray | None) -> None:
        nonlocal size
        assert reader is not None
        while chunk := await reader.read(65536):
            if target is not None:
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise Failure('tool_failed', 'The child command returned too much output.')
                target.extend(chunk)
                if target is output and on_output:
                    on_output(output.decode('utf-8', 'replace'))

    async def collect() -> Record:
        assert child.stdin is not None
        if input_text:
            child.stdin.write(input_text.encode())
        child.stdin.close()
        out = asyncio.create_task(read(child.stdout, output))
        err = asyncio.create_task(read(child.stderr, errors if capture_stderr else None))
        try:
            await wait_exit(child, timeout)
            kill_group(child.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(asyncio.gather(out, err), max(.1, fixed_wait(500)))
            except TimeoutError:
                close_pipes(child)
            return {'code': child.returncode if child.returncode is not None else 1, 'stdout': output.decode('utf-8', 'replace'), **({'stderr': errors.decode('utf-8', 'replace')} if capture_stderr else {})}
        finally:
            out.cancel()
            err.cancel()
            await asyncio.gather(out, err, return_exceptions=True)

    task = asyncio.create_task(collect())
    try:
        return await wait_cancelled(cancel or asyncio.Event(), task, timeout)
    finally:
        kill_group(child.pid, signal.SIGKILL)
        try:
            await wait_exit(child, max(.1, fixed_wait(500)))
        except TimeoutError:
            pass
        close_pipes(child)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(child.wait(), max(.1, fixed_wait(500)))


def supported_version(version: str) -> bool:
    return re.fullmatch(r'\d+\.\d+\.\d+', version) is not None and tuple(map(int, version.split('.'))) >= (2, 1, 289)


def find_tool(program: str | None, env: Env) -> str | None:
    target = program or 'claude'
    candidates = [Path(target).absolute()] if '/' in target or '\\' in target else [Path(folder or '.') / target for folder in env.get('PATH', '').split(os.pathsep)]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    return None


async def inspect_tool(state_directory: Path, program: str | None, env: Env, cancel: asyncio.Event) -> Record:
    info: Record = {'name': 'claude', 'supported': False, 'oldestSupported': OLDEST_SUPPORTED}
    path = find_tool(program, env)
    if path is None:
        return info
    info['path'] = path
    metadata = Path(path).stat()
    changed = now(metadata.st_mtime)
    cached = read_json(state_directory / 'tool.json')
    if isinstance(cached, dict) and cached.get('path') == path and cached.get('size') == metadata.st_size and cached.get('changedAt') == changed and isinstance(cached.get('version'), str) and re.fullmatch(r'\d+\.\d+\.\d+', cached['version']):
        version = cached['version']
    else:
        root = state_directory / 'tmp'
        private_directory(root)
        with tempfile.TemporaryDirectory(prefix='version-', dir=root) as directory:
            home = str(directory)
            try:
                result = await child_command(path, ['--version'], clean_environment(env, home, str(Path(home) / '.claude')), home, 10, cancel)
            except Failure:
                raise Failure('tool_failed', f'claude could not report its version at {path}.', {'detail': {'basis': 'fields', 'neededVersion': OLDEST_SUPPORTED}}) from None
            match = re.fullmatch(r'\s*(\d+\.\d+\.\d+)\s+\(Claude Code\)\s*', result['stdout'])
            if result['code'] != 0 or not match:
                raise Failure('tool_failed', 'claude returned an unrecognised version record.', {'detail': {'basis': 'fields', 'neededVersion': OLDEST_SUPPORTED}})
            version = match[1]
            write_json(state_directory / 'tool.json', {'path': path, 'size': metadata.st_size, 'changedAt': changed, 'version': version, 'askedAt': now()})
    info.update(version=version, supported=supported_version(version))
    return info


def require_tool(tool: Record) -> str:
    if not tool.get('path'):
        raise Failure('tool_missing', f'claude was not found; version not found, needed {OLDEST_SUPPORTED}.', {'detail': {'basis': 'fields', 'neededVersion': OLDEST_SUPPORTED}, 'remedy': 'Install claude.'})
    if not tool['supported']:
        raise Failure('tool_unsupported', f'claude {tool.get("version", "unknown")} is unsupported; needed {OLDEST_SUPPORTED} or newer.', {'detail': {'basis': 'fields', 'toolVersion': tool.get('version', 'unknown'), 'neededVersion': OLDEST_SUPPORTED}, 'remedy': 'Update claude.'})
    return str(tool['path'])


def run_launchers(folder: Path, env: Env) -> tuple[Path, Path]:
    private_directory(folder)
    candidate = env.get('SHELL', '')
    real = candidate if Path(candidate).name in {'bash', 'zsh'} else '/bin/zsh'
    identity = 'umask 077\nstamp=$(/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C TZ=UTC /bin/ps -p "$$" -o lstart=)\n'
    shell = folder / Path(real).name
    shell.write_text('#!/bin/sh\n' + identity + f'printf \'%s\\t%s\\n\' "$$" "$stamp" >> {shlex.quote(str(folder / "shell-calls"))}\nexport SHELL={shlex.quote(real)}\nexec {shlex.quote(real)} "$@"\n')
    shell.chmod(0o700)
    launcher = folder / 'launch-tool'
    launcher.write_text('#!/bin/sh\n' + identity + f'printf \'%s\\t%s\\n\' "$$" "$stamp" > {shlex.quote(str(folder / "tool-owner"))}\nexec "$@"\n')
    launcher.chmod(0o700)
    return launcher, shell


def noted_owners(path: Path) -> list[Record]:
    if not path.exists():
        return []
    owners = []
    for line in path.read_text().splitlines():
        if not line:
            continue
        pid, stamp = line.split('\t', 1)
        if not re.fullmatch('[1-9][0-9]*', pid):
            raise Failure('internal', 'Malformed process ledger.')
        owners.append({'pid': int(pid), 'startedAt': ps_time(stamp)})
    return owners


def remaining_groups(owners: list[Record], table: list[Record]) -> list[int]:
    remaining = []
    for owner in owners:
        leader = next((item for item in table if item['pid'] == owner['pid']), None)
        if leader and (leader['group'] != owner['pid'] or abs(epoch(leader['startedAt']) - epoch(owner['startedAt'])) > 2):
            continue
        if any(item['group'] == owner['pid'] and not item['zombie'] for item in table):
            remaining.append(owner['pid'])
    return list(dict.fromkeys(remaining))


async def process_table(deadline: float) -> list[Record]:
    child = await asyncio.create_subprocess_exec('/bin/ps', '-axo', 'pid=,pgid=,stat=,lstart=', env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        data, _ = await asyncio.wait_for(child.communicate(), max(.001, min(1, deadline - time.monotonic())))
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    table = []
    for line in data.decode().splitlines():
        match = re.fullmatch(r'\s*(\d+)\s+(\d+)\s+(\S+)\s+(.+)', line)
        if match:
            table.append({'pid': int(match[1]), 'group': int(match[2]), 'zombie': match[3].startswith('Z'), 'startedAt': ps_time(match[4])})
    return table


async def end_groups(owners: list[Record], deadline: float | None = None) -> list[int]:
    deadline = deadline if deadline is not None else time.monotonic() + fixed_wait(15000)
    if not owners:
        return []
    if time.monotonic() >= deadline:
        return list(dict.fromkeys(owner['pid'] for owner in owners))
    remaining = remaining_groups(owners, await process_table(deadline))
    for group in remaining:
        kill_group(group, signal.SIGTERM)
    grace = min(deadline, time.monotonic() + fixed_wait(2000))
    while remaining and time.monotonic() < deadline:
        if time.monotonic() >= grace:
            for group in remaining_groups(owners, await process_table(deadline)):
                kill_group(group, signal.SIGKILL)
        await asyncio.sleep(min(fixed_wait(50), max(.001, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            break
        remaining = remaining_groups(owners, await process_table(deadline))
    return remaining


async def end_run_groups(folder: Path, tool: Record | None = None, deadline: float | None = None) -> list[int]:
    return await end_groups([*noted_owners(folder / 'shell-calls'), *noted_owners(folder / 'tool-owner'), *([tool] if tool else [])], deadline)


def owner_skills(home: Path, owner_home: str) -> None:
    source = Path(owner_home) / '.claude' / 'skills'
    directory = home / 'skills'
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists() and not directory.is_dir():
        raise Failure('internal', 'The Subscription skills entry is not a directory.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    desired = {entry.name for entry in source.iterdir() if entry.name != 'synced' and entry.is_dir()} if source.is_dir() else set()
    for entry in directory.iterdir():
        if entry.name != 'synced' and entry.is_symlink() and entry.name not in desired:
            entry.unlink()
    for name in desired:
        link = directory / name
        target = source / name
        if link.exists() and not link.is_symlink():
            raise Failure('internal', 'An owner skill conflicts with a private skill entry.')
        if link.is_symlink() and os.readlink(link) == str(target):
            continue
        temporary = directory / f'.arbol-skill-{os.urandom(12).hex()}'
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)


def owner_mcp_arguments(owner_home: str) -> list[str]:
    path = Path(owner_home) / '.claude.json'
    return ['--mcp-config', str(path)] if path.is_file() else []
