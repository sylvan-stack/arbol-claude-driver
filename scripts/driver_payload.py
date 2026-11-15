"""Static-only immutable payload declaration. No Driver modules are imported."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST = 'driver-payload-manifest.json'
DESCRIPTOR = 'driver-capability.json'
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 100000
Object = dict[str, Any]


def canonical(value: Object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def relative(value: str) -> str:
    raw = value.encode('utf-8', 'strict')
    if (not raw or len(raw) > 4096 or '\\' in value or '\0' in value
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Payload paths must be canonical relative UTF-8 paths.')
    return value


def publish(path: Path, raw: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        Path(temporary).unlink(missing_ok=True)


def freeze(root: Path) -> None:
    for folder, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in (*files, *directories):
            path = Path(folder) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                path.chmod(0o555)
            elif stat.S_ISREG(info.st_mode):
                path.chmod(0o555 if info.st_mode & 0o111 else 0o444)
            else:
                raise ValueError('An immutable payload cannot contain special files.')
    root.chmod(0o700)


def inventory(root: Path) -> list[Object]:
    if root.resolve(strict=True) != root or root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ValueError('The immutable root must be private, owned and physical.')
    rows: list[Object] = []
    normalized: set[str] = set()
    def visit(folder: Path) -> None:
        for path in sorted(folder.iterdir(), key=lambda item: item.name.encode('utf-8')):
            name = relative(path.relative_to(root).as_posix())
            if folder == root and path.name in (MANIFEST, DESCRIPTOR):
                continue
            norm = unicodedata.normalize('NFC', name)
            if norm in normalized or len(rows) >= MAX_FILES:
                raise ValueError('Payload inventory has a collision or exceeds its bound.')
            normalized.add(norm)
            before = path.lstat()
            if before.st_uid != os.getuid():
                raise ValueError('Payload files must be owned by the packager.')
            row: Object = {'path': name, 'mode': stat.S_IMODE(before.st_mode), 'size': 0, 'sha256': None, 'target': None}
            if stat.S_ISDIR(before.st_mode):
                row['type'] = 'directory'
                rows.append(row)
                visit(path)
            elif stat.S_ISREG(before.st_mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    opened = os.fstat(descriptor)
                    with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    after, named = os.fstat(descriptor), path.lstat()
                    def keys(info: os.stat_result) -> tuple[int, int, int, int, int]:
                        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode
                    if keys(before) != keys(opened) or keys(opened) != keys(after) or keys(after) != keys(named):
                        raise ValueError('Payload bytes changed while hashing.')
                finally:
                    os.close(descriptor)
                row.update(type='file', size=before.st_size, sha256=digest)
                rows.append(row)
            elif stat.S_ISLNK(before.st_mode):
                target = os.readlink(path)
                raw = target.encode('utf-8', 'strict')
                resolved = path.resolve(strict=True)
                if not raw or len(raw) > 4096 or os.path.isabs(target) or not resolved.is_relative_to(root):
                    raise ValueError('Payload links must resolve entirely within the root.')
                row.update(type='symlink', size=len(raw), sha256=sha256(raw), target=target)
                rows.append(row)
            else:
                raise ValueError('Payload inventory rejects special files.')
    visit(root)
    # Also reject directory traversal cycles through otherwise valid links.
    nodes = {'', *(row['path'] for row in rows if row['type'] == 'directory')}
    edges: dict[str, set[str]] = {name: set() for name in nodes}
    for row in rows:
        target = row['path'] if row['type'] == 'directory' else None
        if row['type'] == 'symlink' and (root / row['path']).resolve().is_dir():
            target = (root / row['path']).resolve().relative_to(root).as_posix()
            target = '' if target == '.' else target
        if target is not None:
            parent = PurePosixPath(row['path']).parent.as_posix()
            edges['' if parent == '.' else parent].add(target)
    degree = {name: 0 for name in nodes}
    for targets in edges.values():
        for target in targets:
            degree[target] += 1
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
        raise ValueError('Payload directory links must be acyclic.')
    return sorted(rows, key=lambda row: row['path'].encode('utf-8'))


def emit(root: Path, *, component: str, build: str | None, commit: str, tree: str,
         interpreter: str, entry: str, helpers: list[str], import_roots: list[str]) -> Object:
    if any(re.fullmatch('[0-9a-f]{40}', value) is None for value in (commit, tree)):
        raise ValueError('Payload source must be an exact committed tree.')
    if build is not None and re.fullmatch('[1-9a-f][0-9a-f]*', build) is None:
        raise ValueError('Only an exact registered Build can receive D.')
    rows = inventory(root)
    indexed = {row['path']: row for row in rows}
    for name in (interpreter, entry, *helpers):
        if indexed.get(relative(name), {}).get('type') != 'file':
            raise ValueError('Every entry/interpreter/helper must be an inventoried regular file.')
    if not indexed[interpreter]['mode'] & 0o111:
        raise ValueError('The pinned interpreter must be executable.')
    for values in (helpers, import_roots):
        if values != sorted(set(values), key=lambda name: name.encode()):
            raise ValueError('Helper/import roots must be lexical and unique.')
    if not import_roots:
        raise ValueError('An immutable payload needs explicit import roots.')
    for name in import_roots:
        row = indexed.get(relative(name), {})
        if not (row.get('type') == 'directory' or row.get('type') == 'file' and name.endswith('.zip')):
            raise ValueError('Import roots must be existing inventoried directories or zip files.')
    manifest = {'version': 1, 'kind': 'driver-payload-manifest', 'files': rows}
    raw = canonical(manifest)
    if len(raw) > MAX_BYTES:
        raise ValueError('Payload manifest exceeds its bound.')
    declaration: Object = {'version': 1, 'kind': 'driver-capability', 'component': component,
                           'source': {'commit': commit, 'tree': tree}, 'interpreter': interpreter, 'entry': entry,
                           'helpers': helpers, 'importRoots': import_roots, 'payloadManifestSha256': sha256(raw),
                           'capabilities': {'subscriptionIdentity': 1, 'driverMaintenance': 1}}
    if build is not None:
        declaration['build'] = build
        if len(canonical(declaration)) > 4096:
            raise ValueError('Capability declaration exceeds its bound.')
    publish(root / MANIFEST, raw)
    if build is not None:
        publish(root / DESCRIPTOR, canonical(declaration))
    # Static inputs without a registered Build deliberately have no executable D.
    if inventory(root) != rows:
        raise ValueError('The final immutable inventory changed.')
    return declaration
