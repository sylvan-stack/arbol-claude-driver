"""Package original standalone CPython bytes and this Driver into its final folder."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_support import COMPONENT, ENTRY, ROOT, build_folder, digest, metadata, source_provenance, target  # noqa: E402


def archive(distribution: dict[str, Any]) -> Path:
    checksum = distribution.get("sha256")
    url = distribution.get("url")
    if not isinstance(checksum, str) or len(checksum) != 64 or not isinstance(url, str):
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
    version, pin = metadata()
    if folder == ROOT or folder in ROOT.parents or ROOT in folder.parents:
        raise ValueError("The Build folder must not overlap its source checkout.")
    if (folder / "package-manifest.json").exists() or (folder / "runtime").exists():
        raise ValueError("Refusing to overwrite an existing packaged program.")
    selected = target()
    distribution = pin["distributions"][selected]
    compressed = archive(distribution)
    runtime = folder / "runtime"
    runtime.mkdir(mode=0o700)
    # The original install_only archive is relocatable; a uv-created venv is not.
    with tarfile.open(compressed, "r:gz") as bundle:
        bundle.extractall(runtime, filter="data")
    interpreter = runtime / "python/bin/python3.14"
    probe = subprocess.run([str(interpreter), "-I", "-c", "import platform; print(platform.python_version())"],
                           check=True, capture_output=True, text=True, cwd=folder)
    if probe.stdout.strip() != pin["version"]:
        raise ValueError("The extracted interpreter does not match the pinned Python version.")
    package_name = COMPONENT.replace("-", "_")
    (folder / "src").mkdir()
    shutil.copytree(ROOT / "src" / package_name, folder / "src" / package_name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "tests", folder / "tests", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("package.json", "pyproject.toml", ".python-version", "uv.lock", "README.md"):
        shutil.copy2(ROOT / name, folder / name)
    scripts = folder / "scripts"
    scripts.mkdir()
    for source in (ROOT / "scripts").iterdir():
        if source.suffix in (".py", ".json"):
            shutil.copy2(source, scripts / source.name)
    fixtures = folder / "test"
    fixtures.mkdir()
    for source in (ROOT / "test").iterdir():
        if source.is_file() and (source.suffix in (".json", ".txt") or source.name.startswith("conformance-")):
            shutil.copy2(source, fixtures / source.name)
    binary = folder / "bin"
    binary.mkdir()
    shutil.copy2(ROOT / "bin/entry.py", binary / "entry.py")
    executable = folder / ENTRY
    executable.write_text(
        '#!/bin/sh\ndirectory=${0%/*}\n[ "$directory" != "$0" ] || directory=.\n'
        'root=$(CDPATH= cd -- "$directory/.." && pwd -P) || exit 1\n'
        'exec "$root/runtime/python/bin/python3.14" -I "$root/bin/entry.py" "$@"\n', encoding="utf-8")
    executable.chmod(0o755)
    manifest = {
        "format": 1, "component": COMPONENT, "packageVersion": version, "entry": ENTRY,
        "build": number or "0", "candidate": number is None,
        "source": source_provenance(),
        "python": {"version": pin["version"], "target": selected, "provider": pin["provider"],
                   "release": pin["release"], "archiveUrl": distribution["url"],
                   "archiveSha256": distribution["sha256"], "executable": "runtime/python/bin/python3.14",
                   "executableSha256": digest(interpreter)},
        "files": {str(path.relative_to(folder)): digest(path) for path in sorted(folder.rglob("*"))
                  if path.is_file() and runtime not in path.parents and path.name not in ("build.json", "build.log")},
    }
    (folder / "package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
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
    print(f"Packaged {COMPONENT} in {folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
