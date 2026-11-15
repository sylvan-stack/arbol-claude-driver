"""Prospective full-inventory H1 support; not wired into a gate or executed.

The fixed admitted Suite owns its admission until its caller has settled every
local resource and final output. Reports here are immutable local observations,
never a complete check result or permission to maintain a subject.
"""
from __future__ import annotations

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
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal

from arbol_claude_driver.admission import Admission, HelperHandoff, boot_uuid, normal_context, verify_acceptance
from arbol_claude_driver.identity_protocol import (
    Object, canonical, decode, fields, inspect_process, load_payload, process_identity,
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
                        child.wait(timeout=max(0, cleanup_end - time.monotonic()))
                    except BaseException as cleanup:
                        error.add_note(f"Actual owned child remains unresolved: {cleanup!r}.")
                    for failed_stream in (child.stdout, child.stderr):
                        if failed_stream is not None:
                            try:
                                failed_stream.close()
                            except BaseException as cleanup:
                                error.add_note(f"Actor output closure failed: {cleanup!r}.")
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
