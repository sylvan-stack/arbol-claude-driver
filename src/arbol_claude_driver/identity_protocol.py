"""Strict local identity protocol and immutable payload verification.

This module imports only the standard library and performs no state access on import.
Manifest declarations supply identity, never qualification or mutation authority.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath
from pwd import getpwuid
from typing import Any, NoReturn

Object = dict[str, Any]
COMPONENTS = tuple(f"arbol-{name}-driver" for name in ("claude", "codex", "mimo", "pi"))
LIMIT = 4096
MANIFEST_LIMIT = 32 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)


class Refusal(ValueError):
    """A bounded nonsecret refusal; exception text never includes document values."""


def refuse() -> NoReturn:
    raise Refusal("Driver admission authority is missing, changed or unsafe; preserve it for closed recovery.")


def token() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def valid_token(value: object) -> bool:
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        return False
    try:
        raw = base64.urlsafe_b64decode(value + "=")
        return len(raw) == 32 and base64.urlsafe_b64encode(raw).decode().rstrip("=") == value
    except ValueError:
        return False


def valid_hash(value: object) -> bool:
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def natural(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and value >= int(positive)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_SECONDS = 253402300799
_STARTED = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{6})Z\Z", re.ASCII)


class BsdInfo(ctypes.Structure):
    """Public sys/proc_info.h proc_bsdinfo, qualified 64-bit Darwin layout."""

    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32), ("jobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32), ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def library() -> ctypes.CDLL:
    if (sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(BsdInfo) != 136 or ctypes.alignment(BsdInfo) != 8
            or BsdInfo.pid.offset != 12 or BsdInfo.status.offset != 4
            or BsdInfo.start_sec.offset != 120 or BsdInfo.start_usec.offset != 128):
        refuse()
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError):
        refuse()
    return lib


def start_string(seconds: int, microseconds: int) -> str:
    if (not natural(seconds, positive=True) or seconds > _MAX_SECONDS
            or not natural(microseconds) or microseconds >= 1000000):
        refuse()
    try:
        # Integer timedelta arithmetic preserves tvusec; no floating epoch roundtrip.
        value = _EPOCH + timedelta(seconds=seconds, microseconds=microseconds)
    except (OverflowError, ValueError):
        refuse()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def valid_start(value: object) -> bool:
    if not isinstance(value, str) or (match := _STARTED.fullmatch(value)) is None:
        return False
    try:
        year, month, day, hour, minute, second, microsecond = map(int, match.groups())
        parsed = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)
        return parsed >= _EPOCH and parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") == value
    except ValueError:
        return False


def identity_record(value: object) -> Object:
    if (not isinstance(value, dict) or set(value) != {"pid", "startedAt"}
            or not natural(value["pid"], positive=True) or value["pid"] > 2**31 - 1
            or not valid_start(value["startedAt"])):
        refuse()
    return value


@dataclass(frozen=True)
class Process:
    owner: Object
    parent: int
    group: int


def inspect_process(pid: int) -> Process | None:
    if not natural(pid, positive=True) or pid > 2**31 - 1:
        refuse()
    value = BsdInfo()
    ctypes.set_errno(0)
    size = library().proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
    error = ctypes.get_errno()
    if not size and error == errno.ESRCH:
        return None
    if size != 136 or value.pid != pid:
        refuse()
    if value.status == 5:  # Public sys/proc.h SZOMB is not a live identity.
        return None
    if value.status not in (2, 3, 4) or value.uid != os.getuid() or not value.ppid:
        refuse()  # SIDL/unknown/inaccessible observations supply no authority.
    owner = {"pid": pid, "startedAt": start_string(value.start_sec, value.start_usec)}
    return Process(identity_record(owner), int(value.ppid), int(value.pgid))


def process_identity(pid: int) -> Object:
    value = inspect_process(pid)
    if value is None:
        refuse()
    return value.owner



def canonical(value: Object) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, UnicodeError, TypeError):
        refuse()
    raise AssertionError


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> Object:
    result: Object = {}
    for name, value in items:
        if name in result:
            refuse()
        result[name] = value
    return result


def _constant(value: str) -> Any:
    refuse()


def decode(raw: bytes, *, limit: int = LIMIT, exact: bool = True) -> Object:
    if not raw or len(raw) > limit:
        refuse()
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_constant)
    except (ValueError, UnicodeError):
        refuse()
    if not isinstance(value, dict) or (exact and canonical(value) != raw):
        refuse()
    return value


def fields(value: Object, names: str) -> None:
    if set(value) != set(names.split()) or type(value.get("version")) is not int or value.get("version") != 1:
        refuse()


def physical(path: str | Path, *, directory: bool = True) -> Path:
    """A preexisting absolute physical path, with no symlink component."""
    candidate = Path(path)
    if not candidate.is_absolute() or str(candidate) != os.path.abspath(candidate):
        refuse()
    try:
        if candidate.resolve(strict=True) != candidate:
            refuse()
        info = candidate.lstat()
        if directory and not stat.S_ISDIR(info.st_mode):
            refuse()
    except OSError:
        refuse()
    return candidate


def private_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        # The parent is already verified. Never follow an existing symlink to create children.
        physical(path.parent)
        try:
            path.mkdir(mode=0o700)
            sync_directory(path.parent)
        except FileExistsError:
            pass
    physical(path)
    info = path.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        refuse()
    return path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular(descriptor: int, *, mode: int = 0o600) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        refuse()
    return info


def read_bytes(path: Path, *, limit: int = LIMIT) -> bytes:
    private_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = _regular(descriptor)
        if info.st_size > limit:
            refuse()
        raw = bytearray()
        while len(raw) <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        current = _regular(descriptor)
        named = path.lstat()
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
                (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino) or len(raw) > limit):
            refuse()
        return bytes(raw)
    finally:
        os.close(descriptor)


def read(path: Path, *, limit: int = LIMIT) -> Object:
    return decode(read_bytes(path, limit=limit), limit=limit)


def publish_bytes(path: Path, raw: bytes, *, limit: int = LIMIT) -> None:
    private_directory(path.parent)
    if len(raw) > limit:
        refuse()
    try:
        prior = path.lstat()
        if not stat.S_ISREG(prior.st_mode) or prior.st_uid != os.getuid() or stat.S_IMODE(prior.st_mode) != 0o600:
            refuse()
    except FileNotFoundError:
        pass
    temporary = path.parent / ("." + path.name + "." + token())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish(path: Path, value: Object, *, limit: int = LIMIT) -> None:
    publish_bytes(path, canonical(value), limit=limit)


def remove(path: Path) -> None:
    private_directory(path.parent)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            refuse()
        path.unlink()
        sync_directory(path.parent)
    except FileNotFoundError:
        pass


def validate(kind: str, value: Object) -> None:
    """Strict record field/type boundary. Operation-specific invariants are checked by its consumer."""
    schemas = {
        "control": "version phase session epoch",
        "writer": "version invocation writer parent component epoch payloadSha256 boot pid startedAt phase contextSha256",
        "handoff": "version handoff component invocation parent child epoch payloadSha256 phase nonce contextSha256 role entry acceptanceSha256",
        "taint": "version component epoch boot reason invocation payloadSha256",
        "maintenance": "version kind component session epoch phase gateSha256",
        "qualification": "version component acceptanceSha256",
        "deployment": "version kind session registrySha256 gateSha256 callersSha256 oldBoot newBoot phase components checkpoint",
        "context": "version kind component session epoch gateSha256 payloadSha256 stateDirectory operation attempt receiptSha256 registry acceptanceSha256 generation",
        "operation": "version kind component session epoch gateSha256 payloadSha256 stateDirectory operation attempt receiptSha256 registry acceptanceSha256 generation phase sourceSha256 backupSha256",
        "result": "version kind component session epoch operation attempt payloadSha256 stateDirectorySha256 format generation status backupSha256",
    }
    names = schemas.get(kind)
    if names is None:
        refuse()
    if kind == "handoff" and value.get("phase") == "accepted":
        names += " challenge"
    if kind in ("context", "operation", "result") and value.get("operation") == "identity-recover":
        names += " subscription"
    fields(value, names)
    if "component" in value and value["component"] not in COMPONENTS:
        refuse()
    for key in ("epoch", "invocation", "writer", "handoff", "child", "nonce", "challenge", "attempt", "generation", "checkpoint"):
        if key in value and not valid_token(value[key]):
            refuse()
    for key in ("payloadSha256", "contextSha256", "sourceSha256", "backupSha256"):
        if key in value and value[key] is not None and not valid_hash(value[key]):
            refuse()
    for key in ("gateSha256", "registrySha256", "callersSha256", "receiptSha256", "acceptanceSha256", "stateDirectorySha256"):
        if key in value and not valid_hash(value[key]):
            refuse()
    for key in ("boot", "oldBoot"):
        if key in value and not canonical_uuid(value[key]):
            refuse()
    if "newBoot" in value and value["newBoot"] is not None and not canonical_uuid(value["newBoot"]):
        refuse()
    if "session" in value and value["session"] is not None and not canonical_uuid(value["session"]):
        refuse()
    if kind == "control":
        if value["phase"] not in ("normal", "draining", "exclusive") or (value["phase"] == "normal") != (value["session"] is None):
            refuse()
    if kind == "writer":
        if (not natural(value["pid"], positive=True) or not valid_start(value["startedAt"])
                or value["pid"] > 2**31 - 1 or value["phase"] not in ("active", "terminal")
                or (value["parent"] is not None and not valid_token(value["parent"]))):
            refuse()
    if kind == "handoff" and (value["phase"] not in ("pending", "accepted")
                              or value["role"] not in ("main", "helper") or not valid_token(value["parent"])):
        refuse()
    if kind == "taint" and value["reason"] not in ("legacy", "custody-loss", "initial-deployment"):
        refuse()
    if kind == "maintenance" and (value["kind"] != "driver-state-maintenance" or value["phase"] != "exclusive"):
        refuse()
    if kind in ("context", "operation", "result"):
        if value["operation"] not in ("upgrade", "identity-reset", "restore", "identity-recover"):
            refuse()
        expected = "driver-state-result" if kind == "result" else "driver-state-context"
        if value["kind"] != expected:
            refuse()
    if kind == "operation" and value["phase"] != "pending":
        refuse()
    if kind == "result" and (type(value["format"]) is not int or value["format"] != 2 or value["status"] != "complete"):
        refuse()
    if kind == "deployment" and (value["kind"] != "driver-gate-deployment" or value["phase"] not in (
            "staged", "closed-installed", "awaiting-boot", "boot-verified", "maintaining", "complete", "failed-closed")):
        refuse()


@dataclass(frozen=True)
class Authority:
    registry: Path
    component: str

    def __post_init__(self) -> None:
        private_directory(self.registry)
        if self.component not in COMPONENTS:
            refuse()

    @property
    def folder(self) -> Path:
        return self.registry / self.component / ".driver-admission"

    @property
    def shared(self) -> Path:
        return self.registry / ".driver-qualification"

    @property
    def identity(self) -> Object:
        info = self.registry.stat()
        return {"path": str(self.registry), "device": info.st_dev, "inode": info.st_ino}

    def open_lock(self, name: str, *, create: bool = False) -> int:
        if name not in ("control.lock", "admission.lock"):
            refuse()
        private_directory(self.folder)
        path = self.folder / name
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            info = _regular(descriptor)
            named = path.lstat()
            if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                refuse()
            if create:
                os.fsync(descriptor)
                sync_directory(path.parent)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def mutex(self, *, seconds: float = 20) -> Iterator[None]:
        fd = self.open_lock("control.lock")
        deadline = time.monotonic() + seconds
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        refuse()
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            current = self.folder / "control.lock"
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (current.stat().st_dev, current.stat().st_ino):
                refuse()
            yield
        finally:
            os.close(fd)

    def lease(self, *, exclusive: bool = False) -> int:
        descriptor = self.open_lock("admission.lock")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def read(self, kind: str) -> Object:
        value = read(self.folder / (kind + ".json"))
        validate(kind, value)
        return value

    def write(self, kind: str, value: Object) -> None:
        validate(kind, value)
        publish(self.folder / (kind + ".json"), value)


MANIFEST = "driver-payload-manifest.json"
DESCRIPTOR = "driver-capability.json"
SUBTREE = "driver-payload"
MAX_ENTRIES = 100000
BUILD = re.compile(r"[1-9a-f][0-9a-f]*\Z", re.ASCII)
SOURCE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


def relative(value: object) -> str:
    if not isinstance(value, str):
        refuse()
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeError:
        refuse()
    if (not raw or len(raw) > LIMIT or "\0" in value or "\\" in value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        refuse()
    return value


def contained(root: Path, name: object, *, regular: bool = False) -> Path:
    value = relative(name)
    path = root.joinpath(*PurePosixPath(value).parts)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or resolved == root:
            refuse()
        if regular and not stat.S_ISREG(path.lstat().st_mode):
            refuse()
    except (OSError, RuntimeError):
        refuse()
    return path


def _file_hash(path: Path) -> tuple[int, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            refuse()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(descriptor)
        named = path.lstat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)
                or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)):
            refuse()
        return before.st_size, digest
    finally:
        os.close(descriptor)


def _acyclic_links(root: Path, rows: list[Object]) -> None:
    """Reject directory-link traversal cycles as well as symlink-chain cycles."""
    nodes = {"", *(row["path"] for row in rows if row["type"] == "directory")}
    edges: dict[str, set[str]] = {name: set() for name in nodes}
    for row in rows:
        target: str | None = None
        if row["type"] == "directory":
            target = row["path"]
        elif row["type"] == "symlink":
            resolved = (root / row["path"]).resolve(strict=True)
            if resolved.is_dir():
                target = resolved.relative_to(root).as_posix()
                if target == ".":
                    target = ""
        if target is not None:
            parent = PurePosixPath(row["path"]).parent.as_posix()
            parent = "" if parent == "." else parent
            if parent not in edges or target not in edges:
                refuse()
            edges[parent].add(target)
    degree = {name: 0 for name in nodes}
    for targets in edges.values():
        for name in targets:
            degree[name] += 1
    ready = [name for name, count in degree.items() if not count]
    visited = 0
    while ready:
        name = ready.pop()
        visited += 1
        for target in edges[name]:
            degree[target] -= 1
            if not degree[target]:
                ready.append(target)
    if visited != len(nodes):
        refuse()


def inventory(root: Path) -> list[Object]:
    private_directory(root)
    found: list[Object] = []
    collisions: set[str] = set()

    def visit(folder: Path) -> None:
        with os.scandir(folder) as entries:
            names = sorted((entry.name for entry in entries), key=lambda value: value.encode("utf-8", "strict"))
        for name in names:
            path = folder / name
            spelling = relative(path.relative_to(root).as_posix())
            if folder == root and name in (MANIFEST, DESCRIPTOR):
                continue
            normalized = unicodedata.normalize("NFC", spelling)
            if normalized in collisions or len(found) >= MAX_ENTRIES:
                refuse()
            collisions.add(normalized)
            info = path.lstat()
            if info.st_uid != os.getuid():
                refuse()
            row: Object = {"path": spelling, "mode": stat.S_IMODE(info.st_mode), "size": 0,
                           "sha256": None, "target": None}
            if stat.S_ISREG(info.st_mode):
                row["type"] = "file"
                row["size"], row["sha256"] = _file_hash(path)
            elif stat.S_ISDIR(info.st_mode):
                row["type"] = "directory"
            elif stat.S_ISLNK(info.st_mode):
                row["type"] = "symlink"
                target = os.readlink(path)
                try:
                    raw = target.encode("utf-8", "strict")
                    resolved = path.resolve(strict=True)
                except (UnicodeError, OSError, RuntimeError):
                    refuse()
                if not raw or len(raw) > LIMIT or os.path.isabs(target) or not resolved.is_relative_to(root):
                    refuse()
                row.update(size=len(raw), sha256=sha256(raw), target=target)
            else:
                refuse()
            found.append(row)
            if row["type"] == "directory":
                visit(path)

    visit(root)
    _acyclic_links(root, found)
    return sorted(found, key=lambda row: row["path"].encode("utf-8"))


def _manifest(raw: bytes, root: Path) -> Object:
    value = decode(raw, limit=MANIFEST_LIMIT)
    fields(value, "version kind files")
    if value["kind"] != "driver-payload-manifest" or not isinstance(value["files"], list):
        refuse()
    if len(value["files"]) > MAX_ENTRIES:
        refuse()
    prior = b""
    normalized: set[str] = set()
    for row in value["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "type", "mode", "size", "sha256", "target"}:
            refuse()
        name = relative(row["path"])
        encoded = name.encode("utf-8")
        norm = unicodedata.normalize("NFC", name)
        if (encoded <= prior or norm in normalized or name in (MANIFEST, DESCRIPTOR)
                or not natural(row["mode"]) or row["mode"] > 4095 or not natural(row["size"])):
            refuse()
        prior = encoded
        normalized.add(norm)
        kind = row["type"]
        if kind == "directory":
            if row["size"] != 0 or row["sha256"] is not None or row["target"] is not None:
                refuse()
        elif kind == "file":
            if not valid_hash(row["sha256"]) or row["target"] is not None:
                refuse()
        elif kind == "symlink":
            target = row["target"]
            if (not isinstance(target, str) or not target or os.path.isabs(target)
                    or not valid_hash(row["sha256"])):
                refuse()
            try:
                data = target.encode("utf-8", "strict")
                destination = (root / name).resolve(strict=True)
            except (OSError, RuntimeError, UnicodeError):
                refuse()
            if (not destination.is_relative_to(root) or row["size"] != len(data)
                    or row["sha256"] != sha256(data)):
                refuse()
        else:
            refuse()
    return value


def _descriptor(raw: bytes, manifest: Object, manifest_raw: bytes, component: str, build: str) -> Object:
    value = decode(raw)
    fields(value, "version kind component build source interpreter entry helpers importRoots payloadManifestSha256 capabilities")
    if (value["kind"] != "driver-capability" or component not in (*COMPONENTS, "arbol")
            or value["component"] != component or value["build"] != build or not BUILD.fullmatch(build)
            or value["payloadManifestSha256"] != sha256(manifest_raw)):
        refuse()
    source = value["source"]
    if (not isinstance(source, dict) or set(source) != {"commit", "tree"}
            or any(not isinstance(source[key], str) or SOURCE.fullmatch(source[key]) is None for key in source)):
        refuse()
    if value["capabilities"] != {"subscriptionIdentity": 0 if component == "arbol" else 1, "driverMaintenance": 1}:
        refuse()
    # bool compares equal to int in Python; exact wire integer types remain required.
    if any(type(item) is not int for item in value["capabilities"].values()):
        refuse()
    indexed = {row["path"]: row for row in manifest["files"]}
    for name in (value["interpreter"], value["entry"]):
        if indexed.get(relative(name), {}).get("type") != "file":
            refuse()
    if not indexed[value["interpreter"]]["mode"] & 0o111:
        refuse()
    for key in ("helpers", "importRoots"):
        names = value[key]
        if (not isinstance(names, list) or not all(isinstance(name, str) for name in names)
                or names != sorted(set(names), key=lambda name: name.encode("utf-8", "strict"))):
            refuse()
        if key == "importRoots" and not names:
            refuse()
        for name in names:
            row = indexed.get(relative(name), {})
            if key == "helpers" and row.get("type") != "file":
                refuse()
            if key == "importRoots" and not (row.get("type") == "directory" or
                                              (row.get("type") == "file" and name.endswith(".zip"))):
                refuse()
    return value


@dataclass(frozen=True)
class Payload:
    root: Path
    descriptor: Object
    digest: str

    @property
    def component(self) -> str:
        return str(self.descriptor["component"])

    @property
    def build(self) -> str:
        return str(self.descriptor["build"])

    @property
    def prefix(self) -> list[str]:
        return [str(self.root / self.descriptor["interpreter"]), "-ISB", str(self.root / self.descriptor["entry"])]

    def helper_prefix(self, entry: str) -> list[str]:
        if entry not in self.descriptor["helpers"]:
            refuse()
        return [str(self.root / self.descriptor["interpreter"]), "-ISB", str(self.root / entry)]


def load_payload(root: Path, component: str, build: str) -> Payload:
    private_directory(root)
    before = root.lstat()
    manifest_raw = read_bytes(root / MANIFEST, limit=MANIFEST_LIMIT)
    descriptor_raw = read_bytes(root / DESCRIPTOR)
    manifest = _manifest(manifest_raw, root)
    descriptor = _descriptor(descriptor_raw, manifest, manifest_raw, component, build)
    if manifest["files"] != inventory(root):
        refuse()
    for name in (descriptor["interpreter"], descriptor["entry"], *descriptor["helpers"]):
        contained(root, name, regular=True)
    for name in descriptor["importRoots"]:
        contained(root, name)
    after = root.lstat()
    if ((before.st_dev, before.st_ino, before.st_mode, before.st_uid) !=
            (after.st_dev, after.st_ino, after.st_mode, after.st_uid)
            or read_bytes(root / MANIFEST, limit=MANIFEST_LIMIT) != manifest_raw
            or read_bytes(root / DESCRIPTOR) != descriptor_raw):
        refuse()
    return Payload(root, descriptor, sha256(descriptor_raw))




def installed_registry() -> Path:
    return Path(getpwuid(os.getuid()).pw_dir) / "Builds/registries/arbol"


def registered_payload_root(registry: Path, component: str, build: str) -> Path:
    if component not in (*COMPONENTS, "arbol") or BUILD.fullmatch(build) is None:
        refuse()
    if registry == installed_registry():
        registry = Path(getpwuid(os.getuid()).pw_dir) / ".local/lib/sylvan-stack/arbol/stable"
    return registry / component / build / SUBTREE


def verify_installed_registration(registry: Path, payload: Payload) -> str | None:
    """Bind an installed payload to the committed five-package registration."""
    if payload.root != registered_payload_root(registry, payload.component, payload.build):
        refuse()
    if registry != installed_registry():
        return None

    folder = private_directory(registry / payload.component / payload.build)
    if os.path.lexists(folder / "build.json"):
        refuse()
    wrapper = read(folder / "driver-registration.json", limit=MANIFEST_LIMIT)
    fields(wrapper, "version kind registrationSha256 record")
    digest, record = wrapper["registrationSha256"], wrapper["record"]
    if (wrapper["kind"] != "driver-registered-build" or not isinstance(digest, str)
            or not valid_hash(digest) or not isinstance(record, dict)):
        refuse()

    history = private_directory(registry / ".driver-qualification" / "registrations")
    raw = read_bytes(history / (digest + ".json"), limit=MANIFEST_LIMIT)
    group = decode(raw, limit=MANIFEST_LIMIT)
    fields(group, "version kind scope builds")
    expected = ("arbol", *COMPONENTS)
    builds = group["builds"]
    if (sha256(raw) != digest or group["kind"] != "driver-build-registration"
            or group["scope"] != str(registry) or not isinstance(builds, list) or len(builds) != len(expected)):
        refuse()

    selected = None
    row_fields = set("component build source manifestSha256 descriptorSha256 recordSha256 entry entrySha256 entryMode candidate".split())
    for name, row in zip(expected, builds, strict=True):
        if (not isinstance(row, dict) or set(row) != row_fields or row["component"] != name
                or not isinstance(row["build"], str) or BUILD.fullmatch(row["build"]) is None
                or any(not valid_hash(row[key]) for key in (
                    "manifestSha256", "descriptorSha256", "recordSha256", "entrySha256"))):
            refuse()
        if name == payload.component and row["build"] == payload.build:
            selected = row
    source = payload.descriptor["source"]
    recorded_source = record.get("source")
    if (selected is None or sha256(canonical(record)) != selected["recordSha256"]
            or record.get("component") != payload.component or record.get("build") != payload.build
            or record.get("entry") != selected["entry"] or record.get("status") != "working"
            or record.get("packageOnly") is not True or record.get("stableCandidate") is not True
            or record.get("prunedAt") or not isinstance(recorded_source, dict)
            or recorded_source.get("commit") != source["commit"]
            or recorded_source.get("uncommittedChanges") is not False
            or (payload.component in COMPONENTS and record.get("dataFormat") != 2)
            or selected["descriptorSha256"] != payload.digest
            or selected["manifestSha256"] != payload.descriptor["payloadManifestSha256"]
            or selected["source"] != source or not natural(selected["entryMode"])):
        refuse()
    entry = contained(payload.root.parent, selected["entry"], regular=True)
    mode = stat.S_IMODE(entry.lstat().st_mode)
    if mode != selected["entryMode"] or not mode & 0o111 or _file_hash(entry)[1] != selected["entrySha256"]:
        refuse()
    return digest
