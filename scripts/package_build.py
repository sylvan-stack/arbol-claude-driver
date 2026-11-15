"""Package original standalone CPython bytes and this Driver into its final folder."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from driver_payload import canonical, emit, freeze, publish  # noqa: E402
from build_support import COMPONENT, ENTRY, ROOT, build_folder, digest, metadata, source_provenance, target, committed_source  # noqa: E402


def archive(distribution: dict[str, Any]) -> Path:
    checksum = distribution.get("sha256")
    url = distribution.get("url")
    if (not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None or not isinstance(url, str)
            or not url.startswith("https://github.com/astral-sh/python-build-standalone/releases/download/")):
        raise ValueError("The standalone distribution must have a pinned URL and SHA-256 digest.")
    cache = Path(os.environ.get("ARBOL_PYTHON_ARCHIVE_CACHE", str(Path.home() / ".cache/arbol-driver-build/python")))
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{checksum}.tar.gz"
    if destination.exists() and digest(destination) != checksum:
        raise ValueError("The cached standalone Python archive has the wrong SHA-256 digest.")
    if not destination.exists():
        fd, name = tempfile.mkstemp(prefix="download-", suffix=".tar.gz", dir=cache)
        os.close(fd)
        temporary = Path(name)
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            if digest(temporary) != checksum:
                raise ValueError("The downloaded standalone Python archive has the wrong SHA-256 digest.")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def package(candidate: str | None) -> Path:
    folder, number = build_folder(candidate)
    provenance = source_provenance()
    if provenance['uncommittedChanges'] is not False:
        raise ValueError('Refusing to package dirty source.')
    if folder == ROOT or folder in ROOT.parents or ROOT in folder.parents:
        raise ValueError('The Build folder must not overlap its source checkout.')
    payload = folder / 'driver-payload'
    if payload.exists():
        raise ValueError('Refusing to overwrite a preserved payload or failed candidate.')
    folder.chmod(0o700)
    payload.mkdir(mode=0o700)
    with committed_source(provenance) as source_root:
        version, pin = metadata(source_root)
        selected = target()
        distribution = pin['distributions'][selected]
        compressed = archive(distribution)
        runtime = payload / 'runtime'
        runtime.mkdir(mode=0o700)
        with tarfile.open(compressed, 'r:gz') as bundle:
            bundle.extractall(runtime, filter='data')
        interpreter = runtime / 'python/bin/python3.14'
        # The only executable packaging observation is the pinned interpreter's
        # stdlib version. No Driver/helper, admission, gate or provider runs.
        probe = subprocess.run([str(interpreter), '-ISB', '-c', 'import sys;print(".".join(map(str,sys.version_info[:3])))'],
                               check=True, capture_output=True, text=True, cwd=payload)
        if probe.stdout.strip() != pin['version']:
            raise ValueError('The extracted interpreter does not match the pinned Python version.')
        (payload / 'src').mkdir(mode=0o700)
        shutil.copytree(source_root / 'src' / COMPONENT.replace('-', '_'), payload / 'src' / COMPONENT.replace('-', '_'),
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(source_root / 'tests', payload / 'tests', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('package.json', 'pyproject.toml', '.python-version', 'uv.lock', 'README.md'):
            shutil.copy2(source_root / name, payload / name)
        scripts = payload / 'scripts'
        scripts.mkdir(mode=0o700)
        for item in (source_root / 'scripts').iterdir():
            if item.suffix in ('.py', '.json'):
                shutil.copy2(item, scripts / item.name)
        fixtures = payload / 'test'
        fixtures.mkdir(mode=0o700)
        for item in (source_root / 'test').iterdir():
            if item.is_file() and (item.suffix in ('.json', '.txt') or item.name.startswith('conformance-')):
                shutil.copy2(item, fixtures / item.name)
        binary = payload / 'bin'
        binary.mkdir(mode=0o700)
        for name in ('entry.py', 'metadata-helper.py'):
            shutil.copy2(source_root / 'bin' / name, binary / name)
        executable = payload / ENTRY
        executable.write_text(
            '#!/bin/sh\ndirectory=${0%/*}\n[ "$directory" != "$0" ] || directory=.\n'
            'directory=$(CDPATH= cd -P -- "$directory" && pwd -P) || exit 1\nroot=${directory%/*}\n'
            'exec "$root/runtime/python/bin/python3.14" -ISB "$root/bin/entry.py" "$@"\n', encoding='utf-8')
        executable.chmod(0o755)
        manifest = {
            'format': 1, 'component': COMPONENT, 'packageVersion': version, 'entry': ENTRY,
            'build': number, 'candidate': number is None, 'source': provenance,
            'python': {'version': pin['version'], 'target': selected, 'provider': pin['provider'],
                       'release': pin['release'], 'archiveUrl': distribution['url'], 'archiveSha256': distribution['sha256'],
                       'executable': 'runtime/python/bin/python3.14', 'executableSha256': digest(interpreter)},
            'files': {str(path.relative_to(payload)): digest(path) for path in sorted(payload.rglob('*')) if path.is_file()},
        }
        publish(payload / 'package-manifest.json', canonical(manifest))
    if source_provenance() != provenance:
        raise ValueError('Source changed during packaging; the failed candidate is preserved.')
    # All data/provenance bytes settle before M/D. Read-only descendants also
    # prevent old gate interpreter flags from adding caches into the M tree.
    freeze(payload)
    roots = ['runtime/python/lib/python3.14', 'runtime/python/lib/python3.14/lib-dynload', 'src']
    declaration = emit(payload, component=COMPONENT, build=number, commit=str(provenance['commit']), tree=str(provenance['tree']),
                       interpreter='runtime/python/bin/python3.14', entry='bin/entry.py',
                       helpers=['bin/metadata-helper.py'], import_roots=roots)
    aliases = ('runtime', 'src', 'tests', 'test', 'scripts', 'bin', 'package.json', 'pyproject.toml',
               '.python-version', 'uv.lock', 'README.md', 'package-manifest.json')
    for name in aliases:
        path = folder / name
        if os.path.lexists(path):
            raise ValueError('Refusing to replace an existing compatibility path.')
        path.symlink_to('driver-payload/' + name, target_is_directory=(payload / name).is_dir())
    if number is None:
        publish(folder / 'driver-payload-inputs.json', canonical({
            'version': 1, 'kind': 'driver-static-input', 'registeredBuildRequired': True,
            'source': {'commit': provenance['commit'], 'tree': provenance['tree']},
            'runtime': manifest['python'], 'payloadManifestSha256': declaration['payloadManifestSha256'],
            'declarationInputs': {key: value for key, value in declaration.items() if key not in ('version', 'kind')},
            'compatibilityPaths': {name: 'driver-payload/' + name for name in aliases}}))
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", help="Absolute empty folder for a development candidate, outside the numbered registry")
    arguments = parser.parse_args()
    try:
        folder = package(arguments.candidate)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Cannot package {COMPONENT}: {error}", file=sys.stderr)
        return 1
    print(f"Packaged {COMPONENT} inputs in {folder}; no acceptance issued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
