"""Standalone Build helpers; deliberately local to this independent Driver."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "arbol-claude-driver"
ENTRY = f"bin/{COMPONENT}"
BUILD_NUMBER = re.compile(r"[1-9a-f][0-9a-f]*\Z")


def read_object(path: Path) -> dict[str, Any]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return value


def metadata(root: Path = ROOT) -> tuple[str, dict[str, Any]]:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    package = read_object(root / "package.json")
    version = project.get("version")
    if not isinstance(version, str) or not version or package.get("version") != version:
        raise ValueError("package.json and pyproject.toml must agree on the package version.")
    if project.get("name") != COMPONENT or project.get("dependencies", []):
        raise ValueError("The package must name this Driver and have no runtime dependencies.")
    pin = read_object(root / "scripts/python-distribution.json")
    if project.get("requires-python") != f"=={pin.get('version')}":
        raise ValueError("pyproject.toml must require the exact pinned standalone Python version.")
    if (root / ".python-version").read_text(encoding="utf-8").strip() != pin.get("version"):
        raise ValueError(".python-version must match the pinned standalone Python version.")
    return version, pin


def source_provenance(root: Path = ROOT) -> dict[str, str | bool]:
    def git(*arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True, timeout=15).stdout.strip()

    return {"repository": str(root.resolve()), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": git("rev-parse", "HEAD"), "uncommittedChanges": bool(git("status", "--porcelain"))}


def target() -> str:
    architecture = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}.get(platform.machine())
    if sys.platform != "darwin" or architecture is None:
        raise ValueError("Standalone Driver delivery currently supports macOS arm64 and x86_64.")
    return f"{architecture}-apple-darwin"


def build_folder(candidate: str | None = None) -> tuple[Path, str | None]:
    directory = candidate if candidate is not None else os.environ.get("ARBOL_BUILD_DIR")
    if not directory or not Path(directory).is_absolute():
        raise ValueError("Set ARBOL_BUILD_DIR or --candidate to an absolute Build folder.")
    folder = Path(directory).resolve(strict=True)
    if candidate is not None:
        if (folder / "build.json").exists():
            raise ValueError("A development candidate cannot replace a numbered Build.")
        return folder, None
    number = os.environ.get("ARBOL_BUILD_NUMBER", "")
    if not BUILD_NUMBER.fullmatch(number):
        raise ValueError("ARBOL_BUILD_NUMBER must be lower-case hexadecimal without a leading zero.")
    record = read_object(folder / "build.json")
    version, _ = metadata()
    if record.get("component") != COMPONENT or record.get("build") != number or record.get("packageVersion") != version:
        raise ValueError("The Build record must match this component, number and package version.")
    if record.get("entry") != ENTRY:
        raise ValueError("The Build record must name this Driver's executable entry.")
    return folder, number


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> int:
    result = subprocess.run(arguments, cwd=cwd, env=env, check=False)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode
