"""Prospective full-inventory H1 support; not wired into a gate or executed.

The fixed admitted Suite owns its admission until its caller has settled every
local resource and final output. Reports here are immutable local observations,
never a complete check result or permission to maintain a subject.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import select
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, TracebackType
from typing import Any, ClassVar, Literal

from arbol_claude_driver.admission import (
    Admission, HelperHandoff, _join_owned, boot_uuid, current_admission, normal_context, verify_acceptance,
)
from arbol_claude_driver.identity_protocol import (
    BUILD, DESCRIPTOR, Object, canonical, decode, fields, inspect_process, load_payload, process_identity,
    read_bytes, sha256, valid_hash, valid_token, validate,
)

COMPONENT = "arbol-claude-driver"
H1_ID = "tests/top/test_top_subscription_identity.py::HelperSpanTests.test_helper_independent_lease"
ACTOR = "tests/identity_actor.py"
SELECTOR = "anthropic-2"
WINDOW = "H1-independent-lease"
_DOCUMENT_LIMIT = 1024 * 1024
_FRAME_LIMIT = 4096


class SpanFailure(ValueError):
    """A failed local observation; retained writer obligations are not cleared."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SpanFailure(message)


def _ids(values: tuple[str, ...]) -> None:
    require(0 < len(values) <= 4096 and len(set(values)) == len(values), "Invalid complete inventory identities.")
    require(all(isinstance(value, str) and 0 < len(value.encode("utf-8", "strict")) <= 512 for value in values),
            "Invalid bounded inventory identity.")


def _array_bytes(values: tuple[str, ...]) -> bytes:
    return (json.dumps(list(values), ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()


def _document(raw: bytes, limit: int = _FRAME_LIMIT) -> Object:
    return decode(raw, limit=limit)


def _remaining(end: float) -> float:
    result = end - time.monotonic()
    require(result > 0, "The fixed helper-span deadline expired.")
    return result


def _directory(info: os.stat_result) -> None:
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "Fixture directory is not physical, private and owned.")


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


@dataclass
class Directory:
    """A retained directory description with an independently checked name."""

    path: Path
    descriptor: int
    identity: tuple[int, int]

    @classmethod
    def root(cls, path: Path) -> Directory:
        require(path.is_absolute() and path.resolve(strict=True) == path, "Fixture registry is not physical.")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            _directory(info)
            require(_same(info, path.lstat()), "Fixture registry name changed.")
            return cls(path, descriptor, (info.st_dev, info.st_ino))
        except BaseException:
            os.close(descriptor)
            raise

    def verify(self) -> None:
        info = os.fstat(self.descriptor)
        _directory(info)
        require((info.st_dev, info.st_ino) == self.identity and _same(info, self.path.lstat()),
                "Retained fixture directory was replaced.")

    def child(self, name: str, *, fresh: bool = False) -> Directory:
        require(bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name,
                "Invalid fixed fixture component.")
        self.verify()
        try:
            os.mkdir(name, 0o700, dir_fd=self.descriptor)
            os.fsync(self.descriptor)
        except FileExistsError:
            if fresh:
                raise SpanFailure("Fixture case already exists.") from None
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=self.descriptor)
        try:
            info = os.fstat(descriptor)
            _directory(info)
            require(_same(info, os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)),
                    "Fixture child directory name changed.")
            result = Directory(self.path / name, descriptor, (info.st_dev, info.st_ino))
            self.verify()
            result.verify()
            return result
        except BaseException:
            os.close(descriptor)
            raise

    def exclusive(self, name: str, raw: bytes, *, limit: int = _DOCUMENT_LIMIT) -> None:
        require(bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", name)) and len(raw) <= limit,
                "Invalid fixed bounded evidence publication.")
        self.verify()
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.descriptor)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                require(written > 0, "Evidence write made no progress.")
                view = view[written:]
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                    and _same(info, os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)),
                    "Evidence publication name changed.")
            os.fsync(self.descriptor)
            self.verify()
        finally:
            os.close(descriptor)

    def read(self, name: str, *, limit: int = _FRAME_LIMIT) -> bytes:
        require(bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", name)), "Invalid fixed evidence basename.")
        self.verify()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=self.descriptor)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_size <= limit
                    and _same(info, os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)),
                    "Retained private evidence is linked, unsafe or replaced.")
            raw = bytearray()
            while len(raw) <= limit:
                part = os.read(descriptor, min(65536, limit + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            after = os.fstat(descriptor)
            require(len(raw) <= limit and _same(info, after) and after.st_nlink == 1
                    and after.st_uid == os.getuid() and stat.S_IMODE(after.st_mode) == 0o600
                    and (info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
                    and _same(after, os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)),
                    "Retained private evidence changed while reading.")
            self.verify()
            return bytes(raw)
        finally:
            os.close(descriptor)

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            os.close(descriptor)


def _payload_bytes(admission: Admission, relative: str, *, limit: int) -> bytes:
    """M/D verification precedes these fixed packaged-source observations."""
    path = admission.payload.root / relative
    require(path.resolve(strict=True) == path, "Packaged test input is not physical.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022
                and info.st_nlink == 1 and info.st_size <= limit, "Unsafe packaged test input.")
        result = bytearray()
        while len(result) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not part:
                break
            result.extend(part)
        after = os.fstat(descriptor)
        require(len(result) <= limit and _same(info, after) and _same(info, path.lstat())
                and (info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                "Packaged test input changed during observation.")
        return bytes(result)
    finally:
        os.close(descriptor)


def _suite_current(admission: Admission) -> Object:
    """Use the real Suite object; do not demand its transferred Guardian stay active."""
    payload = load_payload(admission.payload.root, COMPONENT, admission.payload.build)
    require(payload.digest == admission.payload.digest and not admission.closed,
            "The actual Suite payload or admission changed.")
    context = admission.context
    normal_context(context)
    require(context["role"] == "helper" and context["entry"] == "scripts/check_build.py"
            and context["component"] == COMPONENT and context["payloadSha256"] == payload.digest
            and context["process"] == process_identity(os.getpid()) and context["parent"]["pid"] == os.getppid()
            and {key: context["parent"][key] for key in ("pid", "startedAt")} == process_identity(os.getppid())
            and sha256(read_bytes(admission.context_file)) == admission.context_digest,
            "Not the actual admitted fixed whole-unit Suite.")
    with admission.authority.mutex():
        admission._lease_current()
        control = admission.authority.read("control")
        require(control["epoch"] == context["epoch"] and control["phase"] in ("normal", "draining")
                and boot_uuid() == context["boot"], "Suite epoch or actual boot changed.")
        verify_acceptance(admission.authority, payload, context["acceptanceSha256"])
        writer = _writer(admission, context)
        require(writer == _expected_writer(context, admission.context_digest, "active"), "Suite is no longer active.")
    return context


def _writer(admission: Admission, context: Object) -> Object:
    path = admission.authority.folder / "writers" / context["invocation"] / (context["writer"] + ".json")
    writer = _document(read_bytes(path))
    validate("writer", writer)
    return writer


def _expected_writer(context: Object, digest: str, phase: str) -> Object:
    return {"version": 1, "invocation": context["invocation"], "writer": context["writer"],
            "parent": context["parent"]["writer"], "component": COMPONENT, "epoch": context["epoch"],
            "payloadSha256": context["payloadSha256"], "boot": context["boot"], **context["process"],
            "phase": phase, "contextSha256": digest}


@dataclass
class Fixture:
    admission: Admission
    case: str
    directories: list[Directory]
    root: Directory

    @classmethod
    def create(cls, admission: Admission, case: str) -> Fixture:
        require(re.fullmatch(r"[0-9a-f]{32}", case, re.ASCII) is not None, "Invalid fixed case namespace.")
        context = _suite_current(admission)
        require(valid_token(context["invocation"]) and valid_token(context["writer"]), "Invalid actual Suite tokens.")
        directories: list[Directory] = []
        try:
            root = Directory.root(admission.authority.registry)
            directories.append(root)
            for name in (".driver-qualification", "fixtures", context["invocation"], case):
                root = root.child(name, fresh=name == case)
                directories.append(root)
            result = cls(admission, case, directories, root)
            owner = {"version": 1, "case": case, "component": COMPONENT, "invocation": context["invocation"],
                     "parentWriter": context["writer"], "payloadSha256": admission.payload.digest}
            root.exclusive(".fixture-owner.json", canonical(owner), limit=_FRAME_LIMIT)
            for name in ("state", "vendor", "local-evidence"):
                child = root.child(name, fresh=True)
                directories.append(child)
            return result
        except BaseException as error:
            for directory in reversed(directories):
                try:
                    directory.close()
                except BaseException as cleanup:
                    error.add_note(f"Fixture descriptor cleanup failed: {cleanup!r}.")
            raise

    def verify(self) -> None:
        for directory in self.directories:
            directory.verify()
        expected = {"version": 1, "case": self.case, "component": COMPONENT,
                    "invocation": self.admission.context["invocation"],
                    "parentWriter": self.admission.context["writer"], "payloadSha256": self.admission.payload.digest}
        require(self.root.read(".fixture-owner.json") == canonical(expected), "Fixture marker changed.")

    def retain(self, name: str, raw: bytes, *, limit: int = _DOCUMENT_LIMIT) -> str:
        self.verify()
        self.root.exclusive(name, raw, limit=limit)
        return sha256(raw)

    def close(self) -> None:
        errors: list[BaseException] = []
        for directory in reversed(self.directories):
            try:
                directory.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("Fixture descriptor cleanup failed.", errors)


@dataclass(frozen=True)
class ClaudeInvocation:
    process: asyncio.subprocess.Process
    owner: Object


@dataclass(frozen=True)
class ClaudeCapture:
    exit: int
    stdout: bytes
    stderr: bytes
    owner: Object


class ClaudeLocalSubject:
    """Run public local commands in the fixed Suite's isolated candidate registry."""

    secret = "abr38-disposable-claude-sentinel"

    def __init__(self, *, guarded_settings: bool = False) -> None:
        admission = current_admission()
        context = _suite_current(admission)
        require(sys.argv[1:] == ["unit"]
                and Path(sys.argv[0]) == admission.payload.root / "scripts/check_build.py",
                "Local proof needs the actual full unit runner.")
        acceptance = verify_acceptance(admission.authority, admission.payload, context["acceptanceSha256"])
        require(acceptance["status"] == "candidate", "Local fixtures require the candidate registry.")
        registry = admission.authority.registry
        candidates = [folder for folder in (registry / "arbol").iterdir() if BUILD.fullmatch(folder.name)
                      and (folder / "driver-payload" / DESCRIPTOR).is_file()
                      and sha256(read_bytes(folder / "driver-payload" / DESCRIPTOR))
                      == acceptance["providerPayloadSha256"]]
        require(len(candidates) == 1, "Could not resolve the exact accepted provider.")
        provider = load_payload(candidates[0] / "driver-payload", "arbol", candidates[0].name)
        self.prefix = [*provider.prefix, "--qualification-launch", str(registry), COMPONENT,
                       admission.payload.build, "driver", "--"]
        self.fixture = Fixture.create(admission, uuid.uuid4().hex)
        self.state = self.fixture.root.path / "state"
        try:
            home = self._child(self.fixture.root, "home")
            config = self._child(home, ".claude")
            settings = self._child(home, ".arbol")
            vendor = self._child(self.fixture.root, "vendor", fresh=False)
            config.exclusive("preserve.txt", (self.secret + "\n").encode())
            vendor.exclusive("claude", b'#!/bin/sh\nprintf "called\\n" > "${0%/*}/called"\nexit 97\n')
            descriptor = os.open("claude", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=vendor.descriptor)
            try:
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
            settings.exclusive("arbol-claude-driver.json", b"{unusable settings\n" if guarded_settings else
                               canonical({"toolPath": str(vendor.path / "claude")}))
            self.environment = {"HOME": str(home.path),
                                "PATH": str(vendor.path) + ":/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}
            self.observed_inputs = self.image(home.path)
            self.vendor_marker = vendor.path / "called"
            self.home = home.path
        except BaseException:
            self.fixture.close()
            raise

    def _child(self, parent: Directory, name: str, *, fresh: bool = True) -> Directory:
        directory = parent.child(name, fresh=fresh)
        self.fixture.directories.append(directory)
        return directory

    def close(self) -> None:
        self.fixture.close()

    def check(self) -> None:
        self.fixture.verify()
        require(not os.path.lexists(self.vendor_marker), "The public command started the private Claude stand-in.")
        require(self.image(self.home) == self.observed_inputs, "The public command changed native or settings files.")

    @staticmethod
    def image(root: Path) -> Object:
        """Independent bounded byte and inode observations of an owned private tree."""
        image: Object = {}
        for folder, subfolders, filenames in os.walk(root, followlinks=False):
            parent = Path(folder)
            record = Directory.root(parent)
            try:
                image[parent.relative_to(root).as_posix()] = list(record.identity)
                for name in subfolders:
                    info = (parent / name).lstat()
                    _directory(info)
                for name in filenames:
                    path = parent / name
                    info = path.lstat()
                    raw = record.read(name)
                    image[path.relative_to(root).as_posix()] = [info.st_dev, info.st_ino, sha256(raw)]
            finally:
                record.close()
            require(len(image) <= 256, "Local proof image exceeded its private fixture bound.")
        return image

    async def spawn(self, operation: str, *arguments: str) -> ClaudeInvocation:
        self.check()
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *self.prefix, "--state-dir", str(self.state), "subscriptions", operation, *arguments,
            cwd=self.fixture.root.path, env=self.environment, close_fds=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        ))
        child: asyncio.subprocess.Process | None = None
        owner: Object | None = None
        try:
            child = await asyncio.shield(spawning)
            assert child.stdin is not None
            child.stdin.close()
            observed = inspect_process(child.pid)
            require(observed is not None and observed.parent == os.getpid(),
                    "The provider is no longer our verified child.")
            assert observed is not None
            owner = observed.owner
            return ClaudeInvocation(child, owner)
        except BaseException as original:
            try:
                if child is None:
                    child = await _join_owned(spawning)
                if child.stdin is not None:
                    child.stdin.close()
                cleanup = self.stop(ClaudeInvocation(child, owner)) if owner is not None else self._reap(child)
                await _join_owned(asyncio.create_task(cleanup))
            except BaseException as error:
                original.add_note("Provider spawn cleanup unresolved: " + type(error).__name__)
            raise

    async def stop(self, invocation: ClaudeInvocation) -> None:
        child = invocation.process
        if child.returncode is None:
            observed = inspect_process(child.pid)
            if observed is not None and observed.owner == invocation.owner and observed.parent == os.getpid():
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        await self._reap(child)

    @staticmethod
    async def _reap(child: asyncio.subprocess.Process) -> None:
        async def discard(stream: asyncio.StreamReader) -> None:
            while await stream.read(4096):
                pass

        async with asyncio.timeout(20), asyncio.TaskGroup() as group:
            if child.stdout is not None:
                group.create_task(discard(child.stdout))
            if child.stderr is not None:
                group.create_task(discard(child.stderr))
            group.create_task(child.wait())

    async def collect(self, invocation: ClaudeInvocation, name: str) -> ClaudeCapture:
        child = invocation.process
        assert child.stdout is not None and child.stderr is not None

        async def drain(stream: asyncio.StreamReader) -> bytes:
            raw = bytearray()
            while block := await stream.read(4096):
                raw.extend(block)
                require(len(raw) <= 4096, "Public local output exceeded its bound.")
            return bytes(raw)

        async with asyncio.timeout(100), asyncio.TaskGroup() as group:
            stdout = group.create_task(drain(child.stdout))
            stderr = group.create_task(drain(child.stderr))
            exit_code = group.create_task(child.wait())
        result = ClaudeCapture(exit_code.result(), stdout.result(), stderr.result(), invocation.owner)
        require(self.secret.encode() not in result.stdout + result.stderr, "The fixture sentinel escaped.")
        self._terminal(result.owner)
        self.fixture.retain(name + ".json", canonical({"exit": result.exit, "owner": result.owner,
                            "stdoutHex": result.stdout.hex(), "stderrHex": result.stderr.hex()}), limit=32768)
        self.check()
        return result

    def _terminal(self, owner: Object) -> None:
        authority = self.fixture.admission.authority
        with authority.mutex():
            roots = list((authority.folder / "writers").iterdir())
            require(len(roots) <= 4096, "The local writer inventory exceeded its bound.")
            found: list[list[Object]] = []
            for directory in roots:
                rows = list(directory.iterdir())
                require(len(rows) <= 4096, "A local invocation's writer inventory exceeded its bound.")
                writers = [decode(read_bytes(row)) for row in rows]
                for writer in writers:
                    validate("writer", writer)
                if any(writer["parent"] is None and
                       {key: writer[key] for key in ("pid", "startedAt")} == owner for writer in writers):
                    found.append(writers)
            require(len(found) == 1 and len(found[0]) == 2 and all(row["phase"] == "terminal" for row in found[0]),
                    "The public provider and ordinary MAIN must retain terminal records after exit.")
            root = next(row for row in found[0] if row["parent"] is None)
            main = next(row for row in found[0] if row["parent"] is not None)
            require(main["parent"] == root["writer"] and main["invocation"] == root["invocation"],
                    "The local MAIN is not bound to the observed provider.")

    async def request(self, evidence: str, operation: str, *arguments: str) -> ClaudeCapture:
        invocation = await self.spawn(operation, *arguments)
        try:
            result = await self.collect(invocation, evidence)
        except BaseException as original:
            try:
                await _join_owned(asyncio.create_task(self.stop(invocation)))
            except BaseException as error:
                original.add_note("Provider cleanup unresolved: " + type(error).__name__)
            raise
        await self.stop(invocation)
        return result


@dataclass
class SuiteStreams:
    """Call before the first local case or descendant spawn; no admission FD is duplicated."""

    fixture: Fixture
    control: tuple[int, int, int]
    stdio_inheritable: tuple[bool, bool, bool]
    captures: tuple[tuple[int, int], tuple[int, int]]
    closed: bool = False
    exchanged: bool = False

    @classmethod
    def separate(cls, fixture: Fixture) -> SuiteStreams:
        fixture.verify()
        require(not fixture.admission.closed, "Suite admission already ended.")
        sys.stdout.flush()
        sys.stderr.flush()
        controls: list[int] = []
        targets: list[int] = []
        changed: list[int] = []
        original_flags = (os.get_inheritable(0), os.get_inheritable(1), os.get_inheritable(2))
        result: SuiteStreams | None = None
        failed: BaseException | None = None
        capture_identities: list[tuple[int, int]] = []
        try:
            for number in (0, 1, 2):
                descriptor = os.dup(number)
                os.set_inheritable(descriptor, False)
                controls.append(descriptor)
            targets.append(os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC))
            for name in ("suite-local.stdout", "suite-local.stderr"):
                fixture.root.exclusive(name, b"")
                descriptor = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=fixture.root.descriptor)
                targets.append(descriptor)
                info = os.fstat(descriptor)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                        and _same(info, os.stat(name, dir_fd=fixture.root.descriptor, follow_symlinks=False)),
                        "Suite local capture was replaced.")
                capture_identities.append((info.st_dev, info.st_ino))
            for number, descriptor in enumerate(targets):
                # Normal child stdio inherits only these owned data sinks.
                # The separate observer control duplicates remain CLOEXEC.
                os.dup2(descriptor, number, inheritable=True)
                changed.append(number)
            result = cls(fixture, (controls[0], controls[1], controls[2]), original_flags,
                         (capture_identities[0], capture_identities[1]))
        except BaseException as error:
            failed = error
            for number in changed:
                try:
                    os.dup2(controls[number], number, inheritable=original_flags[number])
                except BaseException as cleanup:
                    error.add_note(f"Suite stdio restoration failed: {cleanup!r}.")
            for descriptor in controls:
                try:
                    os.close(descriptor)
                except BaseException as cleanup:
                    error.add_note(f"Suite control close failed: {cleanup!r}.")
            raise
        finally:
            close_errors: list[BaseException] = []
            for descriptor in targets:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    close_errors.append(error)
            if close_errors:
                if failed is not None:
                    for close_error in close_errors:
                        failed.add_note(f"Suite temporary descriptor cleanup failed: {close_error!r}.")
                else:
                    if result is not None:
                        try:
                            result.close()
                        except BaseException as error:
                            close_errors.append(error)
                    raise BaseExceptionGroup("Suite temporary descriptor cleanup failed.", close_errors)
        if result is None:
            raise AssertionError("Suite separation did not return an owned result.")
        return result

    def exchange(self, case: str, report_digest: str, *, end: float) -> None:
        require(not self.closed and not self.exchanged and case == self.fixture.case and valid_hash(report_digest),
                "Invalid or repeated fixed Suite exchange.")
        _suite_current(self.fixture.admission)
        self.fixture.verify()
        raw = canonical({"version": 1, "kind": "helper-span-ready", "case": case, "reportSha256": report_digest})
        view = memoryview(raw)
        while view:
            require(bool(select.select([], [self.control[1]], [], _remaining(end))[1]), "Suite ready write expired.")
            written = os.write(self.control[1], view)
            require(written > 0, "Suite ready write failed.")
            view = view[written:]
        response = bytearray()
        while b"\n" not in response:
            require(bool(select.select([self.control[0]], [], [], _remaining(end))[0]), "Suite continue expired.")
            part = os.read(self.control[0], _FRAME_LIMIT + 1 - len(response))
            require(bool(part), "Suite control EOF is failure.")
            response.extend(part)
            require(len(response) <= _FRAME_LIMIT, "Suite control exceeds its fixed bound.")
        self.fixture.retain("suite-control.json", bytes(response), limit=_FRAME_LIMIT)
        document = _document(bytes(response))
        fields(document, "version kind case command")
        require(document == {"version": 1, "kind": "helper-span-control", "case": case,
                             "command": "continue"}, "Invalid fixed Suite continue.")
        self.exchanged = True
        self.require_no_extra()

    def require_no_extra(self) -> None:
        require(not self.closed, "Suite streams already closed.")
        if select.select([self.control[0]], [], [], 0)[0]:
            require(not os.read(self.control[0], 1), "Extra Suite control frame.")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except BaseException as error:
                errors.append(error)
        for number, name, expected in ((1, "suite-local.stdout", self.captures[0]),
                                       (2, "suite-local.stderr", self.captures[1])):
            try:
                info = os.fstat(number)
                require((info.st_dev, info.st_ino) == expected and info.st_size <= 4 * 1024 * 1024
                        and info.st_nlink == 1 and _same(info, os.stat(name, dir_fd=self.fixture.root.descriptor,
                                                                      follow_symlinks=False)),
                        "Suite local capture changed or exceeded the full-run output bound.")
                os.fsync(number)
            except BaseException as error:
                errors.append(error)
        for number, descriptor in enumerate(self.control):
            try:
                os.dup2(descriptor, number, inheritable=self.stdio_inheritable[number])
            except BaseException as error:
                errors.append(error)
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("Suite stdio closure failed.", errors)

    def __enter__(self) -> SuiteStreams:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> Literal[False]:
        try:
            self.close()
        except BaseException as cleanup:
            if error is None:
                raise
            error.add_note(f"Suite stream cleanup failed: {cleanup!r}.")
        return False


@dataclass(frozen=True)
class LocalResult:
    case_id: str
    status: str
    evidence: bytes | None


@dataclass
class LocalResults:
    """The later full runner supplies its ACTUAL per-ID assertion/result bytes.

    This API neither runs nor selects tests. Status is never inferred from child
    exit alone. Complete-body review must verify the source-bound result adapter.
    """

    fixture: Fixture
    order: tuple[str, ...]
    pending_external: tuple[str, ...]
    inventory_digest: str = field(init=False)
    rows: list[Object] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        _ids(self.order)
        _ids(self.pending_external)
        require(self.order[-1] == H1_ID and "H1-span-completion" in self.pending_external,
                "H1 is not the fixed last full-inventory case.")
        admission = self.fixture.admission
        _suite_current(admission)
        raw = _payload_bytes(admission, "test-tiers.json", limit=_DOCUMENT_LIMIT)
        inventory = _inventory(raw)
        require(isinstance(inventory.get("tests"), list), "Packaged inventory has no tests array.")
        actual: list[str] = []
        for row in inventory["tests"]:
            require(isinstance(row, dict), "Malformed packaged inventory entry.")
            if row.get("tier") == "top":
                require(row.get("status") == "active" and isinstance(row.get("id"), str),
                        "Mandatory top-tier entry is disabled or malformed.")
                actual.append(row["id"])
        require(len(set(actual)) == len(actual) and set(actual) == set(self.order)
                and len(actual) == len(self.order), "Full original local inventory was shortened or extended.")
        self.inventory_digest = sha256(raw)

    def record(self, result: LocalResult) -> None:
        index = len(self.rows)
        require(index < len(self.order) and result.case_id == self.order[index], "Local result order differs.")
        require(result.status in ("passed", "h1-preconditions-done", "failed", "skipped", "unavailable", "pending"),
                "Unknown local result disposition.")
        is_h1 = result.case_id == H1_ID
        require(result.status != "h1-preconditions-done" or is_h1, "Only H1 has provisional preconditions.")
        require(not is_h1 or result.status != "passed", "H1's whole span is still owed.")
        require((result.evidence is None) == (result.status == "pending"), "Local evidence and execution disagree.")
        result_digest: str | None = None
        if result.evidence is not None:
            require(bool(result.evidence) and len(result.evidence) <= 65536, "Invalid actual per-ID evidence bound.")
            evidence_directory = self.fixture.directories[-1]
            require(evidence_directory.path == self.fixture.root.path / "local-evidence", "Wrong result directory.")
            evidence_directory.exclusive(f"{index:04d}.result", result.evidence, limit=65536)
            result_digest = sha256(result.evidence)
        self.rows.append({"id": result.case_id, "localStatus": result.status,
                          "helperSpanStatus": "pending" if is_h1 else "not-applicable", "resultSha256": result_digest})

    def publish(self) -> tuple[str, str]:
        require(len(self.rows) == len(self.order), "Local inventory has unfinished unrecorded entries.")
        admission = self.fixture.admission
        context = _suite_current(admission)
        local = {"version": 1, "kind": "abr38-helper-span-local-results", "component": COMPONENT,
                 "inventorySha256": self.inventory_digest, "selectedSha256": sha256(_array_bytes(self.order)),
                 "h1CaseId": H1_ID, "rows": self.rows}
        local_digest = self.fixture.retain("local-results.json", canonical(local))
        base = {"version": 1, "kind": "abr38-inner-phase-only", "component": COMPONENT,
                "build": admission.payload.build, "payloadSha256": admission.payload.digest,
                "invocation": context["invocation"], "writer": context["writer"],
                "contextSha256": admission.context_digest, "selected": list(self.order),
                "pendingExternal": list(self.pending_external), "inventorySha256": self.inventory_digest}
        base_digest = self.fixture.retain("phase-report.json", canonical(base))
        return base_digest, local_digest

    def require_ready(self) -> None:
        require(len(self.rows) == len(self.order), "The original full local inventory is unfinished.")
        for case_id, row in zip(self.order, self.rows, strict=True):
            expected = "h1-preconditions-done" if case_id == H1_ID else "passed"
            require(row["localStatus"] == expected and valid_hash(row["resultSha256"]),
                    "Failed/skipped/unavailable/pending local work cannot arm H1.")


def _inventory(raw: bytes) -> Object:
    # Packaged inventories are human-formatted, unlike canonical report documents.
    return decode(raw, limit=_DOCUMENT_LIMIT, exact=False)


def _retain_start_failure(fixture: Fixture, child: subprocess.Popen[bytes], *,
                          cleanup_end: float, original: BaseException) -> None:
    """Drain real pre-Result pipes without requiring another selector allocation.

    Every read is nonblocking. Both pipes, actual owned wait, every close and
    each evidence write get independent attempts under the caller's one bound.
    Overflow is drained and counted, while only the bounded actual prefix stays
    in memory. An unread tail is explicitly truncated/unresolved, never empty.
    """
    prefixes = {name: bytearray() for name in ("stdout", "stderr")}
    streams = {"stdout": child.stdout, "stderr": child.stderr}
    observations: dict[str, Object] = {
        name: {"present": stream is not None, "eof": False, "bytesObserved": 0}
        for name, stream in streams.items()
    }
    pending: dict[str, int] = {}
    cleanup_errors: list[str] = []

    def failed(operation: str, error: BaseException) -> None:
        note = f"Early H1 {operation} failed: {error!r}."
        original.add_note(note)
        cleanup_errors.append(note)

    for name, stream in streams.items():
        try:
            if stream is None:
                raise SpanFailure("The actual child output pipe is missing.")
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            pending[name] = descriptor
        except BaseException as error:
            failed(f"{name} nonblocking observation setup", error)
    reaped = False
    can_poll = True
    try:
        while True:
            if time.monotonic() >= cleanup_end:
                break
            progress = False
            for name, descriptor in tuple(pending.items()):
                if time.monotonic() >= cleanup_end:
                    break
                try:
                    part = os.read(descriptor, 65536)
                    if not part:
                        observations[name]["eof"] = True
                        del pending[name]
                    else:
                        observations[name]["bytesObserved"] += len(part)
                        available = max(0, _FRAME_LIMIT + 1 - len(prefixes[name]))
                        prefixes[name].extend(part[:available])
                    progress = True
                except BlockingIOError:
                    pass
                except BaseException as error:
                    failed(f"{name} read", error)
                    del pending[name]
            exited = False
            if can_poll:
                try:
                    exited = child.poll() is not None
                except BaseException as error:
                    failed("owned child poll", error)
                    can_poll = False
            if not pending and (exited or not can_poll):
                break
            remaining = cleanup_end - time.monotonic()
            if remaining <= 0:
                break
            if not progress:
                # No new worker, selector, timeout or extended retry budget.
                time.sleep(min(0.01, remaining))
        try:
            child.wait(timeout=max(0, cleanup_end - time.monotonic()))
            reaped = True
        except BaseException as error:
            failed("actual owned child wait/reap", error)
    except BaseException as error:
        failed("bounded failed-output drain", error)
        # A drain/cancellation error must not suppress the owned wait attempt.
        try:
            child.wait(timeout=max(0, cleanup_end - time.monotonic()))
            reaped = True
        except BaseException as wait_error:
            failed("actual owned child wait/reap", wait_error)
    finally:
        for name, stream in streams.items():
            if stream is not None:
                try:
                    stream.close()
                except BaseException as error:
                    failed(f"{name} close", error)
        for name, prefix in prefixes.items():
            observation = observations[name]
            observation.update(prefixBytes=len(prefix), prefixSha256=sha256(bytes(prefix)),
                               overflow=observation["bytesObserved"] > _FRAME_LIMIT,
                               truncated=(observation["bytesObserved"] > len(prefix)
                                          or observation["present"] and not observation["eof"]))
            if observation["overflow"] or observation["truncated"]:
                original.add_note(f"Early H1 {name} overflow={observation['overflow']}, "
                                  f"truncated={observation['truncated']}; preserve its actual failed prefix.")
            try:
                fixture.retain(f"actor-failed.{name}", bytes(prefix), limit=_FRAME_LIMIT + 1)
            except BaseException as error:
                failed(f"{name} failed-prefix retention", error)
        try:
            # Full original exceptions/notes remain on the re-raised exception;
            # this finite private summary separately retains observation facts.
            fixture.retain("actor-start-failure.json", canonical({
                "version": 1, "kind": "abr38-early-helper-failure-output", "streams": observations,
                "ownedChildReaped": reaped, "returncode": child.returncode if reaped else None,
                "deadlineExpired": time.monotonic() >= cleanup_end,
                "errors": [{"prefix": note[:1024], "truncated": len(note) > 1024}
                           for note in cleanup_errors],
            }), limit=16 * 1024)
        except BaseException as error:
            failed("failed-output summary retention", error)


@dataclass
class H1Child:
    fixture: Fixture
    child: subprocess.Popen[bytes]
    ticket: HelperHandoff
    control: socket.socket
    owner: Object | None
    end: float
    selector: selectors.BaseSelector
    output: dict[str, bytearray] = field(default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()})
    ready_raw: bytes | None = None
    ready_at: float | None = None
    helper_context: Object | None = None
    helper_context_raw: bytes | None = None
    ack_raw: bytes | None = None
    reaped: bool = False
    settled: bool = False
    endpoints_closed: bool = False

    @classmethod
    def start(cls, fixture: Fixture) -> H1Child:
        fixture.verify()
        admission = fixture.admission
        _suite_current(admission)
        parent_control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        ticket: HelperHandoff | None = None
        child: subprocess.Popen[bytes] | None = None
        result: H1Child | None = None
        verified_owner: Object | None = None
        end = time.monotonic() + 60
        try:
            parent_control.set_inheritable(False)
            child_control.set_inheritable(False)
            ticket = admission.prepare_helper(ACTOR)
            handshake = ticket.pass_fds
            require(len(handshake) == 1 and len({*handshake, child_control.fileno(), parent_control.fileno(), admission.lease}) == 4
                    and 3 <= child_control.fileno() <= 65535, "Actor control aliases an authority descriptor.")
            child = subprocess.Popen([*ticket.prefix, "lease-hold", SELECTOR, WINDOW, fixture.case,
                                      str(child_control.fileno())], env=ticket.environment(dict(os.environ)),
                                     cwd=fixture.root.path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, close_fds=True,
                                     pass_fds=(*handshake, child_control.fileno()), start_new_session=True)
            child_control.close()
            # Capture actual direct-child ownership before any fallible report IO.
            owner = inspect_process(child.pid)
            if owner is None or owner.parent != os.getpid():
                raise SpanFailure("Cannot independently own the actual H1 child.")
            verified_owner = owner.owner
            result = cls(fixture, child, ticket, parent_control, verified_owner, end, selectors.DefaultSelector())
            if child.stdout is None or child.stderr is None:
                raise SpanFailure("H1 output pipes are missing.")
            for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
                os.set_blocking(stream.fileno(), False)
                result.selector.register(stream, selectors.EVENT_READ, name)
            parent_control.setblocking(False)
            result.selector.register(parent_control, selectors.EVENT_READ, "control")
            # Synchronous acknowledge has no detached worker; every return or
            # exception has completed its actual finally/Frames close before us.
            ticket.acknowledge(child.pid)
            require(ticket.completed, "Actual helper acknowledgement did not finish.")
            result._verify_helper("active")
            result.ack_raw = canonical({"version": 1, "kind": "abr38-actual-helper-ack", "pid": child.pid,
                                        "contextSha256": sha256(result.helper_context_raw or b""),
                                        "handoffSha256": sha256(read_bytes(admission.authority.folder / "handoffs" /
                                                                          (ticket.pending["handoff"] + ".json")))})
            fixture.retain("helper-ack.json", result.ack_raw)
            result._await_ready()
            return result
        except BaseException as error:
            if result is not None:
                for cleanup in result.abort():
                    error.add_note(cleanup)
            else:
                # Retain independently verified ownership even when selector
                # allocation fails. An unverified claim receives no signal.
                cleanup_end = min(end, time.monotonic() + 20)
                for close in (parent_control.close, child_control.close):
                    try:
                        close()
                    except BaseException as cleanup:
                        error.add_note(f"Actor control cleanup failed: {cleanup!r}.")
                if child is not None:
                    try:
                        if child.poll() is None and verified_owner is not None:
                            current = inspect_process(child.pid)
                            require(current is not None and current.owner == verified_owner,
                                    "H1 cleanup identity changed; no signal sent.")
                            os.kill(child.pid, signal.SIGTERM)
                    except BaseException as cleanup:
                        error.add_note(f"Verified child cancellation unresolved: {cleanup!r}.")
                    try:
                        _retain_start_failure(fixture, child, cleanup_end=cleanup_end, original=error)
                    except BaseException as cleanup:
                        error.add_note(f"Early failed-output observation unresolved: {cleanup!r}.")
                        # Even an unexpected observer failure must not suppress
                        # either independent owned pipe-close attempt.
                        for failed_stream in (child.stdout, child.stderr):
                            if failed_stream is not None:
                                try:
                                    failed_stream.close()
                                except BaseException as close_error:
                                    error.add_note(f"Actor output closure failed: {close_error!r}.")
                if ticket is not None:
                    for close in (ticket.child_socket.close, ticket.frames.close):
                        try:
                            close()
                        except BaseException as cleanup:
                            error.add_note(f"Actual ticket closure failed: {cleanup!r}.")
            raise

    def _verify_helper(self, phase: str) -> None:
        admission = self.fixture.admission
        suite = _suite_current(admission)
        raw = read_bytes(self.ticket.context_path)
        context = _document(raw)
        normal_context(context)
        require(context["role"] == "helper" and context["entry"] == ACTOR
                and context["registry"] == suite["registry"] and context["registrySha256"] == suite["registrySha256"]
                and context["component"] == COMPONENT and context["invocation"] == suite["invocation"]
                and context["epoch"] == suite["epoch"] and context["boot"] == suite["boot"]
                and context["payloadSha256"] == admission.payload.digest
                and context["acceptanceSha256"] == suite["acceptanceSha256"]
                and context["parent"] == {"writer": suite["writer"], **suite["process"]}
                and context["process"] == self.owner and context["writer"] == self.ticket.pending["child"]
                and context["handoff"] == self.ticket.pending["handoff"], "H1 actual context binding differs.")
        with admission.authority.mutex():
            handoff = _document(read_bytes(admission.authority.folder / "handoffs" / (context["handoff"] + ".json")))
            validate("handoff", handoff)
            require(handoff == {**self.ticket.pending, "phase": "accepted", "challenge": handoff["challenge"]}
                    and handoff["phase"] == "accepted"
                    and valid_token(handoff["challenge"]) and handoff["contextSha256"] == sha256(raw),
                    "H1 handoff is not the exact actual accepted transfer.")
            require(_writer(admission, context) == _expected_writer(context, sha256(raw), phase),
                    "H1 actual writer disposition differs.")
        if phase == "active":
            require(self.owner is not None, "H1 ownership was not verified.")
            process = inspect_process(self.child.pid)
            require(process is not None and process.owner == self.owner and process.parent == os.getpid(),
                    "H1 current kernel birth/direct parent differs.")
        else:
            require(self.reaped and self.child.returncode is not None, "H1 terminal check precedes actual direct-child reap.")
        if self.helper_context_raw is not None:
            require(raw == self.helper_context_raw, "Original H1 context changed.")
        self.helper_context, self.helper_context_raw = context, raw

    def _drain(self, key: selectors.SelectorKey) -> bytes:
        require(key.data in ("stdout", "stderr"), "Not an actor output pipe.")
        size = _FRAME_LIMIT + 1 - len(self.output[key.data])
        require(size > 0, "Actor output already exceeded its fixed bound.")
        part = os.read(key.fd, size)
        if not part:
            self.selector.unregister(key.fileobj)
            return b""
        self.output[key.data].extend(part)
        require(len(self.output[key.data]) <= _FRAME_LIMIT, "Actor output exceeds the fixed 4096-byte bound.")
        return part

    def _await_ready(self) -> None:
        raw = bytearray()
        ready_end = min(self.end - 20, time.monotonic() + 10)
        while b"\n" not in raw:
            events = self.selector.select(_remaining(ready_end))
            require(bool(events), "Actual H1 readiness expired.")
            for key, _ in events:
                if key.data == "control":
                    part = self.control.recv(_FRAME_LIMIT + 1 - len(raw))
                    require(bool(part), "H1 control EOF before readiness.")
                    raw.extend(part)
                    require(len(raw) <= _FRAME_LIMIT, "H1 ready frame exceeds its fixed bound.")
                else:
                    self._drain(key)
        self.ready_at = time.monotonic()
        self.ready_raw = bytes(raw)
        expected = {"version": 1, "kind": "actor-ready", "case": self.fixture.case,
                    "action": "lease-hold", "window": WINDOW}
        ready = _document(self.ready_raw)
        fields(ready, "version kind case action window")
        require(ready == expected, "H1 ready differs from the fixed actor grammar.")
        self._verify_helper("active")
        self.fixture.retain("actor-ready.json", self.ready_raw, limit=_FRAME_LIMIT)
        self._no_control_extra()
        identities: list[Object] = []
        opened: list[Directory] = []
        try:
            current = self.fixture.root
            for name in ("state", "subscriptions", SELECTOR):
                # Observation only: readiness must already have created these.
                current.verify()
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=current.descriptor)
                try:
                    info = os.fstat(descriptor)
                    _directory(info)
                    current = Directory(current.path / name, descriptor, (info.st_dev, info.st_ino))
                    opened.append(current)
                    current.verify()
                except BaseException:
                    if not opened or opened[-1].descriptor != descriptor:
                        os.close(descriptor)
                    raise
                identities.append({"path": str(current.path), "device": info.st_dev, "inode": info.st_ino,
                                   "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)})
            self.fixture.retain("short-lock-directories.json", canonical({"version": 1, "directories": identities}))
        finally:
            original = sys.exception()
            close_errors: list[BaseException] = []
            for directory in reversed(opened):
                try:
                    directory.close()
                except BaseException as error:
                    close_errors.append(error)
            if close_errors:
                if original is not None:
                    for directory_error in close_errors:
                        original.add_note(f"Short-lock directory cleanup failed: {directory_error!r}.")
                else:
                    raise BaseExceptionGroup("Short-lock directory cleanup failed.", close_errors)

    def _no_control_extra(self) -> None:
        try:
            part = self.control.recv(1, socket.MSG_PEEK)
            require(not part, "Extra actor control data.")
            # An EOF while still active is failure, not continue.
            require(self.reaped, "Actor control EOF before actual settlement.")
        except BlockingIOError:
            pass

    def publish_and_continue(self, results: LocalResults, streams: SuiteStreams) -> None:
        require(results.fixture is self.fixture and streams.fixture is self.fixture and not self.settled
                and self.ready_at is not None and self.ready_raw is not None, "H1 span is not actually ready.")
        results.require_ready()
        self._verify_helper("active")
        base_digest, local_digest = results.publish()
        admission = self.fixture.admission
        context = admission.context
        helper = self.helper_context
        helper_raw, ready_raw, ready_at = self.helper_context_raw, self.ready_raw, self.ready_at
        if helper is None or helper_raw is None or ready_raw is None or ready_at is None:
            raise SpanFailure("H1 context/readiness is missing.")
        lock = os.fstat(admission.lease)
        named = (admission.authority.folder / "admission.lock").lstat()
        require(_same(lock, named) and stat.S_ISREG(lock.st_mode) and lock.st_uid == os.getuid()
                and stat.S_IMODE(lock.st_mode) == 0o600, "Original actual Suite admission inode changed.")
        report = {"version": 1, "kind": "abr38-preterminal-helper-span", "case": self.fixture.case,
                  "component": COMPONENT, "phaseReportSha256": base_digest, "localResultsSha256": local_digest,
                  "inventorySha256": results.inventory_digest,
                  "suite": {"invocation": context["invocation"], "writer": context["writer"],
                            "contextSha256": admission.context_digest, **context["process"]},
                  "helper": {"entry": ACTOR, "invocation": helper["invocation"], "writer": helper["writer"],
                             "handoff": helper["handoff"], "contextSha256": sha256(helper_raw),
                             **helper["process"]}, "actorReadySha256": sha256(ready_raw),
                  "admissionLock": {"device": lock.st_dev, "inode": lock.st_ino},
                  "pendingExternal": list(results.pending_external)}
        report_digest = self.fixture.retain("helper-span.json", canonical(report), limit=16 * 1024)
        exchange_end = min(self.end - 20, ready_at + 8)
        streams.exchange(self.fixture.case, report_digest, end=exchange_end)
        self._send_continue(end=exchange_end)
        self._settle()
        streams.require_no_extra()

    def _send_continue(self, *, end: float) -> None:
        self._no_control_extra()
        raw = canonical({"version": 1, "kind": "actor-control", "case": self.fixture.case, "command": "continue"})
        view = memoryview(raw)
        while view:
            require(bool(select.select([], [self.control], [], _remaining(end))[1]), "Actor continue expired.")
            written = self.control.send(view)
            require(written > 0, "Actor continue write failed.")
            view = view[written:]

    def _settle(self) -> None:
        normal_end = self.end - 20
        self.selector.unregister(self.control)
        while self.selector.get_map() or self.child.poll() is None:
            remaining = _remaining(normal_end)
            for key, _ in self.selector.select(min(0.05, remaining)):
                self._drain(key)
        self.child.wait(timeout=_remaining(normal_end))
        self.reaped = True
        stdout, stderr = bytes(self.output["stdout"]), bytes(self.output["stderr"])
        self.fixture.retain("actor.stdout", stdout, limit=_FRAME_LIMIT)
        self.fixture.retain("actor.stderr", stderr, limit=_FRAME_LIMIT)
        actor_result = _document(stdout)
        fields(actor_result, "version kind case action outcome")
        require(self.child.returncode == 0 and actor_result ==
                {"version": 1, "kind": "actor-result", "case": self.fixture.case,
                 "action": "lease-hold", "outcome": "completed"}, "Actual H1 result/exit did not complete.")
        self._verify_helper("terminal")
        self._no_control_extra()
        helper_context = self.helper_context
        if helper_context is None:
            raise SpanFailure("Actual H1 context is missing.")
        terminal = _writer(self.fixture.admission, helper_context)
        self.fixture.retain("helper-terminal.json", canonical(terminal))
        self.fixture.retain("helper-settlement.json", canonical({"version": 1, "kind": "abr38-direct-helper-settlement",
                            "pid": self.child.pid, "startedAt": (self.owner or {})["startedAt"],
                            "returncode": self.child.returncode, "stdoutSha256": sha256(stdout),
                            "stderrSha256": sha256(stderr), "contextSha256": sha256(self.helper_context_raw or b""),
                            "terminalSha256": sha256(canonical(terminal)), "ackSha256": sha256(self.ack_raw or b"")}))
        self._close_endpoints()
        self.settled = True
        # Suite finish belongs to the later full runner, after ALL resources and
        # final output. A local H1 settlement never closes this admission.
        _suite_current(self.fixture.admission)

    def _close_endpoints(self) -> None:
        if self.endpoints_closed:
            return
        errors: list[BaseException] = []
        for close in (self.control.close, self.selector.close, self.ticket.child_socket.close, self.ticket.frames.close):
            try:
                close()
            except BaseException as error:
                errors.append(error)
        for stream in (self.child.stdout, self.child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as error:
                    errors.append(error)
        if errors:
            raise BaseExceptionGroup("H1 endpoint closure failed.", errors)
        self.endpoints_closed = True

    def abort(self) -> list[str]:
        """One common remaining bound; no SIGKILL, synthetic terminal or lost error."""
        errors: list[str] = []
        cleanup_end = min(self.end, time.monotonic() + 20)
        try:
            self.selector.unregister(self.control)
        except (KeyError, ValueError):
            pass
        except BaseException as error:
            errors.append(f"Actor control deregistration failed: {error!r}.")
        try:
            self.control.close()  # Failure EOF, never a forged positive continue.
        except BaseException as error:
            errors.append(f"Actor failure EOF failed: {error!r}.")
        try:
            if self.child.poll() is None:
                process = inspect_process(self.child.pid)
                require(self.owner is not None and process is not None and process.owner == self.owner,
                        "H1 cleanup identity is unverifiable; no signal sent.")
                os.kill(self.child.pid, signal.SIGTERM)
        except BaseException as error:
            errors.append(f"Verified H1 cancellation failed: {error!r}.")
        try:
            # Drain actual child output while awaiting the exact direct child.
            try:
                self.selector.unregister(self.control)
            except (KeyError, ValueError):
                pass
            while time.monotonic() < cleanup_end and (self.selector.get_map() or self.child.poll() is None):
                try:
                    for key, _ in self.selector.select(min(0.05, max(0, cleanup_end - time.monotonic()))):
                        self._drain(key)
                except BaseException as error:
                    errors.append(f"Actor failed-output observation failed: {error!r}.")
                    break
            self.child.wait(timeout=max(0, cleanup_end - time.monotonic()))
            self.reaped = True
        except BaseException as error:
            errors.append(f"Actual owned H1 reap unresolved: {error!r}.")
        for name in ("stdout", "stderr"):
            try:
                self.fixture.retain(f"actor-failed.{name}", bytes(self.output[name]), limit=_FRAME_LIMIT + 1)
            except BaseException as error:
                errors.append(f"Actor failed-output retention failed: {error!r}.")
        try:
            self._close_endpoints()
        except BaseException as error:
            errors.append(f"Actor endpoint cleanup failed: {error!r}.")
        return errors

    def __enter__(self) -> H1Child:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> Literal[False]:
        if not self.settled:
            errors = self.abort()
            if error is not None:
                for note in errors:
                    error.add_note(note)
            else:
                raise SpanFailure("H1 span did not settle; " + "; ".join(errors))
        return False


@dataclass(frozen=True)
class TerminalHelperWitness:
    """Actual direct-owner objects and retained bytes, never a success flag.

    The fixed source-admitted adapter supplies its complete pinned helper order.
    A nested/async helper without actual-owner receipts is NOT replaced by
    terminal metadata or absence: graph equality below makes that unavailable.
    """

    ticket: HelperHandoff
    child: subprocess.Popen[bytes]
    owner: Object
    context_raw: bytes
    terminal_raw: bytes
    result_raw: bytes
    ack_raw: bytes

    @classmethod
    def from_h1(cls, child: H1Child) -> TerminalHelperWitness:
        require(child.settled and child.reaped and child.endpoints_closed,
                "Positive H1 resources have not actually settled.")
        child._verify_helper("terminal")
        context, raw, ack, owner = child.helper_context, child.helper_context_raw, child.ack_raw, child.owner
        if context is None or raw is None or ack is None or owner is None:
            raise SpanFailure("H1 actual owner/context/ACK evidence is missing.")
        terminal = read_bytes(child.fixture.admission.authority.folder / "writers" / context["invocation"] /
                              (context["writer"] + ".json"))
        return cls(child.ticket, child.child, dict(owner), raw, terminal, bytes(child.output["stdout"]), ack)

    def verify(self, admission: Admission, expected_entry: str) -> Object:
        from arbol_claude_driver.identity_protocol import identity_record

        require(type(self.ticket) is HelperHandoff and self.ticket.parent is admission
                and self.ticket.completed and type(self.child) is subprocess.Popen,
                "Terminal helper lacks its genuine actual owner objects.")
        identity_record(self.owner)
        require(self.owner["pid"] == self.child.pid, "Terminal helper PID differs from its actual child.")
        status = self.child.wait(timeout=0)
        require(type(status) is int and self.child.returncode == status,
                "Terminal helper does not have its actual direct-child wait disposition.")
        live = inspect_process(self.child.pid)
        require(live is None or live.owner != self.owner, "Terminal helper identity is still live.")
        require(self.child.stdout is not None and self.child.stdout.closed
                and self.child.stderr is not None and self.child.stderr.closed
                and self.ticket.child_socket.fileno() == -1 and self.ticket.frames.socket.fileno() == -1,
                "Terminal helper output/handshake resources are not actually closed.")
        require(0 < len(self.result_raw) <= 65536 and 0 < len(self.ack_raw) <= _FRAME_LIMIT,
                "Terminal helper result/ACK evidence is missing or unbounded.")
        context = _document(self.context_raw)
        from arbol_claude_driver.admission import normal_context
        normal_context(context)
        suite = admission.context
        require(expected_entry in admission.payload.descriptor["helpers"] and context["entry"] == expected_entry
                and context["role"] == "helper" and context["component"] == COMPONENT
                and context["registry"] == suite["registry"] and context["registrySha256"] == suite["registrySha256"]
                and context["invocation"] == suite["invocation"] and context["epoch"] == suite["epoch"]
                and context["boot"] == suite["boot"] and context["payloadSha256"] == admission.payload.digest
                and context["acceptanceSha256"] == suite["acceptanceSha256"] and context["process"] == self.owner
                and context["parent"] == {"writer": suite["writer"], **suite["process"]}
                and context["handoff"] == self.ticket.pending["handoff"]
                and context["writer"] == self.ticket.pending["child"], "Actual terminal helper context differs.")
        require(read_bytes(self.ticket.context_path) == self.context_raw, "Original terminal helper context changed.")
        handoff_raw = read_bytes(admission.authority.folder / "handoffs" / (context["handoff"] + ".json"))
        handoff = _document(handoff_raw)
        validate("handoff", handoff)
        require(handoff == {**self.ticket.pending, "phase": "accepted", "challenge": handoff["challenge"]}
                and valid_token(handoff["challenge"]) and handoff["contextSha256"] == sha256(self.context_raw),
                "Terminal helper lacks its actual accepted transfer.")
        terminal = _document(self.terminal_raw)
        validate("writer", terminal)
        require(terminal == _expected_writer(context, sha256(self.context_raw), "terminal")
                and read_bytes(admission.authority.folder / "writers" / context["invocation"] /
                               (context["writer"] + ".json")) == self.terminal_raw,
                "Terminal helper lacks unchanged genuine terminal evidence.")
        ack = _document(self.ack_raw)
        fields(ack, "version kind pid contextSha256 handoffSha256")
        require(ack == {"version": 1, "kind": "abr38-actual-helper-ack", "pid": self.child.pid,
                        "contextSha256": sha256(self.context_raw), "handoffSha256": sha256(handoff_raw)}
                and type(ack["pid"]) is int, "Actual source-bound ACK receipt differs.")
        return {"invocation": context["invocation"], "writer": context["writer"], "handoff": context["handoff"],
                "contextSha256": sha256(self.context_raw), **self.owner, "waitStatus": status,
                "resultSha256": sha256(self.result_raw), "terminalRecordSha256": sha256(self.terminal_raw),
                "ackEvidenceSha256": sha256(self.ack_raw)}


class TerminalCloseScope:
    """The future fixed runner's SOLE owning Admission context, not a second finish.

    Its adapter must remove the competing bootstrap finally. Body/integration
    admission still precedes that wiring. arm() consumes source-level actual
    callback bytes and owner objects, never external modes/paths/success flags.
    Once armed, scope exit owns exactly one real finish and exits quietly; a
    failed/unarmed body preserves active/unknown custody rather than faking it.
    """

    _claimed: ClassVar[bool] = False

    def __init__(self, fixture: Fixture, results: LocalResults, streams: SuiteStreams, h1: H1Child):
        self.fixture, self.results, self.streams, self.h1 = fixture, results, streams, h1
        self.admission = fixture.admission
        self.entered = False
        self.armed = False
        self.finish_may_have_begun = False
        self.finish_called = False
        self.intercepted = False
        self.close_attempted = False
        self.close_returned = False
        self.saved_fd = -1
        self.saved_identity: tuple[int, int, int, int] | None = None
        self.saved_thread = -1
        self.saved_pid = -1
        self.finish_code: CodeType | None = None
        self.real_close = os.close
        self.wrapper = self._close
        self.old_exception_hook = sys.excepthook
        self.quiet_hook = self._quiet_exception
        self.actual_error: BaseException | None = None

    def __enter__(self) -> TerminalCloseScope:
        import posix
        require(type(self) is TerminalCloseScope and not TerminalCloseScope._claimed
                and not self.entered and self.results.fixture is self.fixture and self.streams.fixture is self.fixture
                and self.h1.fixture is self.fixture and not self.admission.closed and os.close is posix.close,
                "Terminal scope is repeated, foreign or already patched.")
        TerminalCloseScope._claimed = True
        self.fixture.verify()
        _suite_current(self.admission)
        self.real_close = posix.close
        self.old_exception_hook = sys.excepthook
        sys.excepthook = self.quiet_hook
        self.entered = True
        return self

    def _quiet_exception(self, kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
        if self.finish_may_have_begun:
            os._exit(2)
        self.old_exception_hook(kind, error, trace)

    def _original_lease(self) -> tuple[int, int, int, int]:
        require(type(self.admission.lease) is int and self.admission.lease >= 3
                and (self.saved_fd == -1 or self.admission.lease == self.saved_fd),
                "Terminal scope original descriptor changed.")
        held = os.fstat(self.admission.lease)
        named = (self.admission.authority.folder / "admission.lock").lstat()
        require(stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid() and stat.S_IMODE(held.st_mode) == 0o600
                and held.st_nlink == 1 and _same(held, named), "Terminal original admission description is unsafe.")
        return (held.st_dev, held.st_ino, held.st_uid, held.st_mode)

    def _finish_source(self) -> CodeType:
        from types import CodeType
        source_name = str(self.admission.payload.root / "src/arbol_claude_driver/admission.py")
        raw = _payload_bytes(self.admission, "src/arbol_claude_driver/admission.py", limit=_DOCUMENT_LIMIT)
        module = compile(raw, source_name, "exec", dont_inherit=True, optimize=sys.flags.optimize)
        classes = [code for code in module.co_consts if isinstance(code, CodeType) and code.co_name == "Admission"]
        require(len(classes) == 1, "Packaged Admission source is ambiguous.")
        finishes = [code for code in classes[0].co_consts if isinstance(code, CodeType) and code.co_name == "finish"]
        require(len(finishes) == 1 and type(self.admission) is Admission
                and Admission.finish.__code__ == finishes[0] and Admission.finish.__code__.co_filename == source_name,
                "Actual Admission.finish is not the exact packaged source.")
        return Admission.finish.__code__

    def _graph(self, witnesses: tuple[TerminalHelperWitness, ...], entries: tuple[str, ...]) -> list[Object]:
        require(0 < len(witnesses) <= 4096 and len(witnesses) == len(entries), "Pinned full helper schedule differs.")
        admission, suite = self.admission, self.admission.context
        with admission.authority.mutex():
            require(not admission.transfers and not os.path.lexists(admission.authority.folder / "taint.json"),
                    "Terminal scope has unresolved transfers or taint.")
            helper_rows = [witness.verify(admission, entry) for witness, entry in zip(witnesses, entries, strict=True)]
            writers: dict[str, Object] = {}
            folder = admission.authority.folder / "writers" / suite["invocation"]
            for path in folder.iterdir():
                require(path.name.endswith(".json"), "Unexpected current-invocation writer entry.")
                row = _document(read_bytes(path))
                validate("writer", row)
                require(row["invocation"] == suite["invocation"] and row["writer"] + ".json" == path.name
                        and row["component"] == COMPONENT, "Current-invocation writer binding differs.")
                writers[row["writer"]] = row
            expected_writers = {suite["writer"], suite["parent"]["writer"], *(row["writer"] for row in helper_rows)}
            require(len(expected_writers) == len(helper_rows) + 2 and set(writers) == expected_writers,
                    "A current-invocation helper lacks actual owner-backed completion evidence.")
            require(writers[suite["writer"]] == _expected_writer(suite, admission.context_digest, "active"),
                    "Whole Suite is not genuinely still active.")
            root = writers[suite["parent"]["writer"]]
            require(root == {"version": 1, "invocation": suite["invocation"], "writer": suite["parent"]["writer"],
                             "parent": None, "component": COMPONENT, "epoch": suite["epoch"],
                             "payloadSha256": admission.payload.digest, "boot": suite["boot"],
                             **{key: suite["parent"][key] for key in ("pid", "startedAt")},
                             "phase": "terminal", "contextSha256": None}, "Actual provider root differs.")
            expected_handoffs = {suite["handoff"], *(row["handoff"] for row in helper_rows)}
            seen_handoffs: set[str] = set()
            for path in (admission.authority.folder / "handoffs").iterdir():
                if path.name.endswith(".context.json"):
                    continue
                require(path.name.endswith(".json"), "Unexpected current handoff entry.")
                row = _document(read_bytes(path))
                validate("handoff", row)
                if row["invocation"] == suite["invocation"]:
                    require(row["phase"] == "accepted" and row["handoff"] + ".json" == path.name,
                            "Current-invocation handoff is unfinished or replaced.")
                    seen_handoffs.add(row["handoff"])
            require(seen_handoffs == expected_handoffs and len(expected_handoffs) == len(helper_rows) + 1,
                    "A nested/async or unlisted helper lacks its complete actual-owner receipt.")
        return helper_rows

    def arm(self, completed_rows: tuple[LocalResult, ...], witnesses: tuple[TerminalHelperWitness, ...], *,
            expected_entries: tuple[str, ...], tuple_pins_raw: bytes, tuple_pins_sha256: str) -> None:
        require(self.entered and not self.armed and not self.finish_may_have_begun
                and not self.streams.closed and self.streams.exchanged,
                "Terminal scope is not in its one positive post-H1 phase.")
        require(valid_hash(tuple_pins_sha256) and sha256(tuple_pins_raw) == tuple_pins_sha256,
                "Actual retained five-tuple pin bytes differ.")
        _document(tuple_pins_raw, _DOCUMENT_LIMIT)  # Canonical bytes; no new authority/locator/schema.
        self.results.require_ready()
        self.fixture.verify()
        _suite_current(self.admission)
        require(self.h1.settled and self.h1.reaped and self.h1.endpoints_closed
                and self.h1.child.returncode == 0, "Positive H1 local completion is still owed.")
        require(threading.current_thread() is threading.main_thread() and threading.enumerate() == [threading.current_thread()],
                "State/output/ACK/helper worker threads are not all joined.")
        import asyncio
        from arbol_claude_driver.state import State
        require(not State._open, "A real local State guard remains open before finish.")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SpanFailure("An active local event loop remains before terminal finish.")
        self.finish_code = self._finish_source()
        self.saved_fd = self.admission.lease
        self.saved_identity = self._original_lease()
        self.saved_thread = threading.get_ident()
        self.saved_pid = os.getpid()
        context = self.admission.context
        require(context["process"] == process_identity(self.saved_pid)
                and {key: context["parent"][key] for key in ("pid", "startedAt")} == process_identity(os.getppid()),
                "Original Suite/provider process identities changed.")
        base_raw = self.fixture.root.read("phase-report.json", limit=_DOCUMENT_LIMIT)
        local_raw = self.fixture.root.read("local-results.json", limit=_DOCUMENT_LIMIT)
        span_raw = self.fixture.root.read("helper-span.json", limit=16 * 1024)
        base, local, span = _document(base_raw, _DOCUMENT_LIMIT), _document(local_raw, _DOCUMENT_LIMIT), _document(span_raw, 16 * 1024)
        fields(base, "version kind component build payloadSha256 invocation writer contextSha256 selected pendingExternal inventorySha256")
        fields(local, "version kind component inventorySha256 selectedSha256 h1CaseId rows")
        fields(span, "version kind case component phaseReportSha256 localResultsSha256 inventorySha256 suite helper actorReadySha256 admissionLock pendingExternal")
        require(base == {"version": 1, "kind": "abr38-inner-phase-only", "component": COMPONENT,
                         "build": self.admission.payload.build, "payloadSha256": self.admission.payload.digest,
                         "invocation": context["invocation"], "writer": context["writer"],
                         "contextSha256": self.admission.context_digest, "selected": list(self.results.order),
                         "pendingExternal": list(self.results.pending_external),
                         "inventorySha256": self.results.inventory_digest}, "Original base report changed.")
        require(local == {"version": 1, "kind": "abr38-helper-span-local-results", "component": COMPONENT,
                          "inventorySha256": self.results.inventory_digest,
                          "selectedSha256": sha256(_array_bytes(self.results.order)), "h1CaseId": H1_ID,
                          "rows": self.results.rows}, "Original provisional local-results changed.")
        h1_context, h1_context_raw, ready_raw = self.h1.helper_context, self.h1.helper_context_raw, self.h1.ready_raw
        if h1_context is None or h1_context_raw is None or ready_raw is None or self.saved_identity is None:
            raise SpanFailure("Original positive H1 source/participant evidence is missing.")
        require(span == {"version": 1, "kind": "abr38-preterminal-helper-span", "case": self.fixture.case,
                         "component": COMPONENT, "phaseReportSha256": sha256(base_raw),
                         "localResultsSha256": sha256(local_raw), "inventorySha256": self.results.inventory_digest,
                         "suite": {"invocation": context["invocation"], "writer": context["writer"],
                                   "contextSha256": self.admission.context_digest, **context["process"]},
                         "helper": {"entry": ACTOR, "invocation": h1_context["invocation"],
                                    "writer": h1_context["writer"], "handoff": h1_context["handoff"],
                                    "contextSha256": sha256(h1_context_raw), **h1_context["process"]},
                         "actorReadySha256": sha256(ready_raw),
                         "admissionLock": {"device": self.saved_identity[0], "inode": self.saved_identity[1]},
                         "pendingExternal": list(self.results.pending_external)}
                and self.fixture.root.read("actor-ready.json") == ready_raw,
                "Original provisional H1 report/readiness changed.")
        require(len(completed_rows) == len(self.results.order), "Full completed local result schedule is incomplete.")
        helpers = self._graph(witnesses, expected_entries)
        require(sum(witness.child is self.h1.child for witness in witnesses) == 1,
                "The one actual positive H1 helper is missing or duplicated.")
        rows: list[Object] = []
        evidence_directory = self.fixture.directories[-1]
        for index, (expected_id, result, provisional) in enumerate(zip(self.results.order, completed_rows,
                                                                      self.results.rows, strict=True)):
            require(result.case_id == expected_id and result.status == "passed" and result.evidence is not None,
                    "An actual full ordered local result is unfinished/failed/skipped/unavailable.")
            raw = result.evidence
            if raw is None:
                raise AssertionError
            require(0 < len(raw) <= 65536, "Actual completed callback evidence is missing or unbounded.")
            if expected_id != H1_ID:
                require(sha256(raw) == provisional["resultSha256"]
                        and raw == evidence_directory.read(f"{index:04d}.result", limit=65536),
                        "Original completed local callback evidence changed.")
            else:
                require(raw != evidence_directory.read(f"{index:04d}.result", limit=65536),
                        "Provisional H1 preconditions were substituted for actual positive completion.")
            self.fixture.retain(f"terminal-local-{index:04d}.result", raw, limit=65536)
            rows.append({"id": expected_id, "resultSha256": sha256(raw)})
        for index, witness in enumerate(witnesses):
            for suffix, raw in (("context", witness.context_raw), ("terminal", witness.terminal_raw),
                                ("result", witness.result_raw), ("ack", witness.ack_raw)):
                self.fixture.retain(f"terminal-helper-{index:04d}.{suffix}", raw, limit=65536 if suffix == "result" else 4096)
        settled = {"version": 1, "kind": "abr38-terminal-close-settled", "component": COMPONENT,
                   "inventorySha256": self.results.inventory_digest,
                   "selectedSha256": sha256(_array_bytes(self.results.order)), "rows": rows,
                   "helpers": helpers, "pendingExternal": list(self.results.pending_external)}
        settled_digest = self.fixture.retain("terminal-close-settled.json", canonical(settled), limit=_DOCUMENT_LIMIT)
        own_identity = self.saved_identity
        if own_identity is None:
            raise AssertionError
        intent = {"version": 1, "kind": "abr38-terminal-close-intent", "case": self.fixture.case,
                  "component": COMPONENT, "tuplePinsSha256": tuple_pins_sha256,
                  "phaseReportSha256": sha256(base_raw), "settledSha256": settled_digest,
                  "suite": {"invocation": context["invocation"], "writer": context["writer"],
                            "handoff": context["handoff"], "contextSha256": self.admission.context_digest,
                            **context["process"]},
                  "provider": {key: context["parent"][key] for key in ("pid", "startedAt")},
                  "admissionLock": {"device": own_identity[0], "inode": own_identity[1]},
                  "ownLease": {"fd": self.saved_fd, "device": own_identity[0], "inode": own_identity[1]},
                  "closeSite": "claude-original-admission-close"}
        intent_digest = self.fixture.retain("terminal-close-intent.json", canonical(intent), limit=16 * 1024)
        self.streams.require_no_extra()
        sys.stdout.flush()
        sys.stderr.flush()
        armed = canonical({"version": 1, "kind": "terminal-close-armed", "case": self.fixture.case,
                           "intentSha256": intent_digest})
        view = memoryview(armed)
        notification_end = time.monotonic() + 5
        while view:
            require(bool(select.select([], [self.streams.control[1]], [], _remaining(notification_end))[1]),
                    "Terminal armed notification expired.")
            written = os.write(self.streams.control[1], view)
            require(written > 0, "Terminal armed notification made no progress.")
            view = view[written:]
        # All output/data/directory cleanup precedes the genuine owning finish.
        self.streams.close()
        self.fixture.close()
        require(self._original_lease() == self.saved_identity and os.close is self.real_close,
                "Original admission description/function changed before its one-use interception.")
        os.close = self.wrapper
        self.armed = True

    def _close(self, fd: int) -> None:
        if type(fd) is not int or fd != self.saved_fd:
            return self.real_close(fd)
        earlier_error = self.actual_error
        observation_error: BaseException | None = None
        mismatch_error: BaseException | None = None
        stop_error: BaseException | None = None
        restore_error: BaseException | None = None
        exact = False
        try:
            # The unconditional original-close boundary is established BEFORE
            # any saved-description/source/thread observation can refuse/raise.
            try:
                exact = (self.finish_may_have_begun and self.armed and self.actual_error is None
                         and not self.intercepted and os.getpid() == self.saved_pid
                         and threading.get_ident() == self.saved_thread
                         and self._original_lease() == self.saved_identity
                         and sys._getframe(1).f_code is self.finish_code
                         and sys._getframe(1).f_locals.get("self") is self.admission)
            except BaseException as error:
                observation_error = error
                if self.actual_error is None:
                    self.actual_error = error
                else:
                    self.actual_error.add_note(f"Close observation also failed: {error!r}.")
            if not exact and observation_error is None:
                mismatch_error = SpanFailure("Original-close identity/source/thread observation differs.")
                if self.actual_error is None:
                    self.actual_error = mismatch_error
                else:
                    self.actual_error.add_note(f"Close observation also differs: {mismatch_error!r}.")
            if exact:
                self.intercepted = True
                try:
                    os.kill(self.saved_pid, signal.SIGSTOP)
                except BaseException as error:
                    stop_error = error
                    self.actual_error = error
        finally:
            # Also covers observation mismatch/error and STOP cancellation.
            # No State, metadata, reports, output, foreign unlock or retries.
            try:
                os.close = self.real_close
            except BaseException as error:
                restore_error = error
                if self.actual_error is None:
                    self.actual_error = error
                else:
                    self.actual_error.add_note(f"Hook restoration also failed: {error!r}.")
            finally:
                self.close_attempted = True
                try:
                    self.real_close(fd)
                    self.close_returned = True
                except BaseException as close_error:
                    for label, prior in (("Earlier fixture", earlier_error),
                                         ("Close observation", observation_error or mismatch_error),
                                         ("Self-STOP", stop_error), ("Hook restoration", restore_error)):
                        if prior is not None:
                            close_error.add_note(f"{label} also failed: {prior!r}.")
                    self.actual_error = close_error
                    raise  # The genuine close exception/type remains primary.
        if restore_error is not None:
            for label, prior in (("Close observation", observation_error or mismatch_error), ("Self-STOP", stop_error)):
                if prior is not None:
                    restore_error.add_note(f"{label} also failed: {prior!r}.")
            raise restore_error
        if stop_error is not None:
            raise stop_error
        if observation_error is not None:
            raise observation_error
        # A false observation delegates the actual close result unchanged. Its
        # separately retained fixture error prevents a successful scope result.

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> Literal[False]:
        if not self.armed:
            # Without positive settled evidence, no finish can honestly certify
            # all local work. Preserve its actual failed writer/unknown custody.
            cleanup_errors: list[BaseException] = []
            for close in (self.streams.close, self.fixture.close):
                try:
                    close()
                except BaseException as cleanup:
                    cleanup_errors.append(cleanup)
                    if error is not None:
                        error.add_note(f"Unarmed terminal scope cleanup failed: {cleanup!r}.")
            sys.excepthook = self.old_exception_hook
            if error is not None:
                self.actual_error = (BaseExceptionGroup("Unarmed body and independent cleanup failures.",
                                                       [error, *cleanup_errors]) if cleanup_errors else error)
                return False  # Preserve the original body exception and its existing chain.
            failure = SpanFailure("Terminal scope never armed; preserve incomplete closed recovery obligations.")
            if cleanup_errors:
                group = BaseExceptionGroup("All independent unarmed cleanup failures.", cleanup_errors)
                self.actual_error = group
                raise failure from group
            raise failure
        status = 2
        self.finish_may_have_begun = True
        try:
            require(not self.finish_called, "Terminal scope cannot finish twice.")
            self.finish_called = True
            self.admission.finish()
            if (error is None and self.actual_error is None and self.intercepted
                    and self.close_attempted and self.close_returned and self.admission.closed):
                status = 0
        except BaseException as actual_error:
            if self.actual_error is not None and self.actual_error is not actual_error:
                actual_error.add_note(f"Earlier fixture observation also failed: {self.actual_error!r}.")
            self.actual_error = actual_error
        finally:
            if os.close is self.wrapper:
                os.close = self.real_close
            elif os.close is not self.real_close:
                status = 2
            # The sole owning context ends the Suite. No bootstrap handler,
            # finalizer or outer context gets a post-finish diagnostic chance.
            os._exit(status)

# Authored unit identities only. Missing case bodies explicitly prevent entry;
# their names join this fixed schedule only after the bodies exist.
CLAUDE_UNIT_ORDER = (
    "tests/top/test_top_runtime.py::StateTests.test_recovery_never_deletes_or_overwrites_a_newer_run",
    "tests/top/test_top_runtime.py::TransportTests.test_output_backpressure_uses_one_absolute_stop_deadline",
    "tests/top/test_top_subscription_identity.py::LocalIdentityTests.test_two_implicit_captures_contend_and_a_fresh_invocation_accepts_exact_proof",
    "tests/top/test_top_subscription_identity.py::LocalIdentityTests.test_legacy_list_keeps_supported_record_and_rejects_retired_aliases",
    "tests/top/test_top_subscription_identity.py::LocalIdentityTests.test_interrupted_conversion_refuses_list_and_proof_without_recovering",
    "tests/top/test_top_subscription_identity.py::LocalIdentityTests.test_native_refusals_preserve_proof_and_native_state",
    "tests/top/test_top_subscription_identity.py::LocalIdentityTests.test_lost_or_corrupt_proof_refuses_without_regeneration",
    "tests/top/test_top_subscription_identity.py::LocalIdentityTests.test_create_publication_five_real_fsync_windows",
    "tests/top/test_top_subscription_identity.py::LifetimeRaceTests.test_matching_and_displaced_record_save",
    "tests/top/test_top_subscription_identity.py::LifetimeRaceTests.test_matching_and_displaced_run_save",
    "tests/top/test_top_subscription_identity.py::LifetimeRaceTests.test_matching_and_displaced_chat_session_completion",
    "tests/top/test_top_subscription_identity.py::LifetimeRaceTests.test_matching_and_displaced_inference_metadata_broker",
    H1_ID,
)
# These are pending duties, not a claim that concrete observers are wired.
CLAUDE_PENDING_EXTERNAL = ("H1-span-completion", "terminal-close-closure",
                           "create-publication-recovery", "create-publication-absence")


class ClaudeWholeUnitPhase:
    """Already-admitted whole-unit preparation; the checker retains bootstrap ownership."""

    def __init__(self, admission: Admission, case: str) -> None:
        self.admission = admission
        self.case = case
        self.fixture: Fixture | None = None
        self.streams: SuiteStreams | None = None
        self.results: LocalResults | None = None
        self.binding: object | None = None
        self.create_helpers: list[TerminalHelperWitness] = []
        self.closed = False

    def __enter__(self) -> ClaudeWholeUnitPhase:
        require(not self.closed and self.fixture is None and current_admission() is self.admission,
                "The entered checker Admission is required.")
        context = _suite_current(self.admission)
        require(context["role"] == "helper" and context["entry"] == "scripts/check_build.py"
                and sys.argv[1:] == ["unit"]
                and Path(sys.argv[0]) == self.admission.payload.root / "scripts/check_build.py"
                and verify_acceptance(self.admission.authority, self.admission.payload,
                                      context["acceptanceSha256"])["status"] == "candidate",
                "Only the candidate's actual unit checker can own this phase.")
        try:
            self.fixture = Fixture.create(self.admission, self.case)
            self.results = LocalResults(self.fixture, CLAUDE_UNIT_ORDER, CLAUDE_PENDING_EXTERNAL)
            self.streams = SuiteStreams.separate(self.fixture)
            from top.test_top_subscription_identity import H1CaseBinding, HelperSpanTests
            self.binding = H1CaseBinding(self.fixture, self.results, self.streams)
            HelperSpanTests.bind(self.binding)
            require(current_admission() is self.admission, "Checker Admission changed during setup.")
            return self
        except BaseException as original:
            self._cleanup(original)
            raise

    def _cleanup(self, original: BaseException | None) -> None:
        self.closed = True
        failures: list[BaseException] = []
        if self.binding is not None:
            from top.test_top_subscription_identity import H1CaseBinding, HelperSpanTests
            assert isinstance(self.binding, H1CaseBinding)
            if self.binding.child is not None and not self.binding.child.settled:
                try:
                    for note in self.binding.child.abort():
                        failures.append(SpanFailure(note))
                except BaseException as error:
                    failures.append(error)
            if HelperSpanTests._binding is self.binding:
                HelperSpanTests._binding = None
        for resource in (self.streams, self.fixture):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    failures.append(error)
        if failures:
            if original is not None:
                for failure in failures:
                    original.add_note("Claude phase cleanup: " + repr(failure))
            else:
                raise BaseExceptionGroup("Claude phase cleanup failed.", failures)

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        if not self.closed:
            self._cleanup(error)

    def continue_h1(self) -> H1Child:
        from top.test_top_subscription_identity import H1CaseBinding
        require(not self.closed and current_admission() is self.admission
                and self.results is not None and self.streams is not None
                and isinstance(self.binding, H1CaseBinding), "The actual admitted phase is unavailable.")
        assert self.results is not None and self.streams is not None
        assert isinstance(self.binding, H1CaseBinding)
        child = self.binding.child
        require(child is not None and self.binding.provisional_result is not None
                and self.results.rows[-1]["localStatus"] == "h1-preconditions-done",
                "Actual H1 callback and preconditions are missing.")
        assert child is not None
        child.publish_and_continue(self.results, self.streams)
        require(child.settled and child.reaped and child.endpoints_closed and child.child.returncode == 0,
                "Actual H1 completion and reap are incomplete.")
        return child

    def record_create_helper(self, child: H1Child, window: str, command: str) -> None:
        """Retain the actual owner receipt before the local create fixture closes."""
        require(not self.closed and current_admission() is self.admission and child.fixture.admission is self.admission,
                "Create helper belongs to another checker.")
        windows = ("C1-prepared-durable", "C2-record-durable", "C3-sidecar-durable",
                   "C4-committed-durable", "C5-clear-durable")
        schedule = tuple((name, mode) for name in windows for mode in ("continue", "abort-io"))
        index = len(self.create_helpers)
        require(index < len(schedule) and (window, command) == schedule[index],
                "Create helper is missing, duplicated or out of order.")
        args = child.child.args
        require(isinstance(args, (list, tuple)) and list(args[:3]) == child.ticket.prefix
                and list(args[3:7]) == ["create-stop", SELECTOR, window, child.fixture.case]
                and len(args) == 8, "Create helper argv differs from its fixed actor route.")
        expected_outcome = "completed" if command == "continue" else "injected-failure"
        require(child.child.returncode == (0 if command == "continue" else 1)
                and _document(bytes(child.output["stdout"])) == {"version": 1, "kind": "actor-result",
                    "case": child.fixture.case, "action": "create-stop", "outcome": expected_outcome}
                and not child.output["stderr"], "Create helper result or actual wait differs.")
        witness = TerminalHelperWitness.from_h1(child)
        witness.verify(self.admission, ACTOR)
        self.create_helpers.append(witness)

    def terminal_helpers(self) -> tuple[TerminalHelperWitness, ...]:
        require(not self.closed and current_admission() is self.admission and len(self.create_helpers) == 10,
                "The actual ten create helper completions are incomplete.")
        from top.test_top_subscription_identity import H1CaseBinding
        require(isinstance(self.binding, H1CaseBinding) and self.binding.child is not None,
                "The actual H1 child is unavailable.")
        assert isinstance(self.binding, H1CaseBinding) and self.binding.child is not None
        return (*self.create_helpers, TerminalHelperWitness.from_h1(self.binding.child))

    def terminal_scope(self) -> TerminalCloseScope:
        from top.test_top_subscription_identity import H1CaseBinding
        require(not self.closed and current_admission() is self.admission
                and self.fixture is not None and self.results is not None and self.streams is not None
                and isinstance(self.binding, H1CaseBinding), "The admitted phase is unavailable.")
        assert self.fixture is not None and self.results is not None and self.streams is not None
        assert isinstance(self.binding, H1CaseBinding)
        child = self.binding.child
        require(child is not None and child.settled and child.reaped and child.endpoints_closed,
                "Terminal scope requires the actual settled H1 helper.")
        assert child is not None
        return TerminalCloseScope(self.fixture, self.results, self.streams, child)

class ClaudeWholeUnitResult(unittest.TestResult):
    """Record actual unittest callbacks; never infer success from a child exit."""

    def __init__(self, phase: ClaudeWholeUnitPhase) -> None:
        super().__init__()
        require(phase.results is not None and not phase.closed, "Entered whole-unit phase required.")
        self.phase = phase
        self.index = 0
        self.current: str | None = None
        self.issues: list[tuple[str, str]] = []
        self.completed: list[LocalResult] = []

    @staticmethod
    def case_id(test: unittest.TestCase) -> str:
        parts = test.id().split(".")
        require(len(parts) >= 3 and parts[-3] in ("test_top_runtime", "test_top_subscription_identity"),
                "Unrecognized whole-unit callback.")
        return "tests/top/" + parts[-3] + ".py::" + parts[-2] + "." + parts[-1]

    def startTest(self, test: unittest.TestCase) -> None:
        case = self.case_id(test)
        require(self.current is None and self.index < len(CLAUDE_UNIT_ORDER)
                and case == CLAUDE_UNIT_ORDER[self.index], "Unexpected whole-unit case order.")
        self.current = case
        self.index += 1
        super().startTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:
        from top.test_top_subscription_identity import H1CaseBinding
        case = self.case_id(test)
        require(case == self.current and self.phase.results is not None, "Unmatched success callback.")
        if case == H1_ID:
            binding = self.phase.binding
            require(isinstance(binding, H1CaseBinding) and binding.provisional_result is not None
                    and binding.child is not None, "H1 did not return its actual provisional result.")
            assert isinstance(binding, H1CaseBinding) and binding.provisional_result is not None
            result = binding.provisional_result
        else:
            result = LocalResult(case, "passed", canonical({"version": 1, "kind": "abr38-unittest-success", "id": case}))
        assert self.phase.results is not None
        self.phase.results.record(result)
        self.completed.append(result)
        super().addSuccess(test)

    def addError(self, test: Any, err: Any) -> None:
        super().addError(test, err)
        self.issues.append((self.current or str(test), "error"))

    def addFailure(self, test: Any, err: Any) -> None:
        super().addFailure(test, err)
        self.issues.append((self.current or str(test), "failure"))

    def addSkip(self, test: Any, reason: str) -> None:
        super().addSkip(test, reason)
        self.issues.append((self.current or str(test), "skipped"))

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.issues.append((self.current or str(test), "subtest"))

    def addExpectedFailure(self, test: Any, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self.issues.append((self.current or str(test), "expected-failure"))

    def addUnexpectedSuccess(self, test: Any) -> None:
        super().addUnexpectedSuccess(test)
        self.issues.append((self.current or str(test), "unexpected-success"))

    def stopTest(self, test: unittest.TestCase) -> None:
        case = self.case_id(test)
        require(case == self.current and self.phase.results is not None, "Unmatched case completion.")
        assert self.phase.results is not None
        issues = [kind for identity, kind in self.issues if identity == case]
        if issues:
            status = "skipped" if all(kind == "skipped" for kind in issues) else "failed"
            try:
                self.phase.results.record(LocalResult(case, status, canonical({"version": 1, "id": case,
                                                                              "callbacks": issues})))
            except BaseException as error:
                self.issues.append((case, "result-recording-" + type(error).__name__))
        self.current = None
        super().stopTest(test)

    def complete(self) -> tuple[LocalResult, ...]:
        require(self.current is None and not self.issues and not self.skipped and self.wasSuccessful()
                and tuple(row.case_id for row in self.completed) == CLAUDE_UNIT_ORDER,
                "Whole-unit callbacks are incomplete or unsuccessful.")
        return tuple(self.completed)

    def after_h1(self) -> tuple[LocalResult, ...]:
        from top.test_top_subscription_identity import H1CaseBinding, _images
        before = self.complete()
        child = self.phase.continue_h1()
        binding = self.phase.binding
        assert isinstance(binding, H1CaseBinding) and binding.provisional_result is not None
        assert binding.provisional_result.evidence is not None
        provisional = _document(binding.provisional_result.evidence)
        observed = {name: sha256(raw) for name, raw in _images(binding.fixture).items()}
        require(provisional["before"] == provisional["after"] == observed,
                "H1 local state changed after actual helper completion.")
        evidence = canonical({"version": 1, "kind": "abr38-h1-local-complete", "id": H1_ID,
                              "preconditionsSha256": sha256(binding.provisional_result.evidence),
                              "after": observed, "exit": child.child.returncode})
        return (*before[:-1], LocalResult(H1_ID, "passed", evidence))
