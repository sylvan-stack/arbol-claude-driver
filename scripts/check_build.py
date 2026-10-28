"""Check the program in its final Build folder, without starting a registry launcher."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_support import COMPONENT, ENTRY, build_folder, digest, metadata, read_object, run, target  # noqa: E402


def verify(folder: Path, candidate: bool) -> dict[str, Any]:
    version, pin = metadata(folder)
    manifest = read_object(folder / "package-manifest.json")
    if (manifest.get("format") != 1 or manifest.get("component") != COMPONENT
            or manifest.get("entry") != ENTRY or manifest.get("packageVersion") != version
            or manifest.get("candidate") is not candidate):
        raise ValueError("The package manifest does not match this Driver and delivery mode.")
    if candidate:
        if manifest.get("build") != "0" or (folder / "build.json").exists():
            raise ValueError("A development candidate must use Build 0 outside the numbered registry.")
    elif read_object(folder / "build.json").get("build") != manifest.get("build"):
        raise ValueError("The package manifest must agree with the Build record's number.")
    python = manifest.get("python")
    if not isinstance(python, dict) or python.get("version") != pin["version"] or python.get("target") != target():
        raise ValueError("The package interpreter must match the pinned version and this machine's architecture.")
    distribution = pin["distributions"][target()]
    if (python.get("archiveSha256") != distribution["sha256"] or python.get("archiveUrl") != distribution["url"]
            or python.get("provider") != pin["provider"] or python.get("release") != pin["release"]):
        raise ValueError("The package interpreter must match the pinned distribution digest and URL.")
    interpreter = folder / "runtime/python/bin/python3.14"
    if digest(interpreter) != python.get("executableSha256"):
        raise ValueError("The packaged Python executable has changed.")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("The package manifest must record packaged file digests.")
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("Invalid package manifest file digest.")
        path = folder / relative
        if folder not in path.resolve().parents or digest(path) != expected:
            raise ValueError(f"The packaged file {relative} is outside its folder or has changed.")
    entry = folder / ENTRY
    if entry.resolve() != entry or not entry.is_file() or not os.access(entry, os.X_OK):
        raise ValueError("The Build entry must be an executable file inside its folder.")
    probe = subprocess.run([str(interpreter), "-I", "-c", "import platform; print(platform.python_version())"],
                           check=True, cwd=folder, capture_output=True, text=True)
    if probe.stdout.strip() != pin["version"]:
        raise ValueError("The package interpreter reports the wrong Python version.")
    return manifest


# TEST-TIER-DISABLED-BEGIN scripts/check_build.py::unit
# def unit(folder: Path) -> int:
#     sys.path.insert(0, str(folder))
#     sys.path.insert(0, str(folder / "src"))
#     os.chdir(folder)
#     suite = unittest.TestLoader().discover(str(folder / "tests"))
#     if suite.countTestCases() == 0:
#         raise ValueError("The packaged unit check discovered no tests.")
#     return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
# TEST-TIER-DISABLED-END scripts/check_build.py::unit
def unit(folder: Path) -> int:
    raise RuntimeError('Tests are commented under the tier policy; see TESTS.md. Restore only in an explicitly requested testing task.')  # TEST-TIER-REFUSAL


# TEST-TIER-DISABLED-BEGIN scripts/check_build.py::contract
# def contract(folder: Path) -> int:
#     # By default the clone of the last merged main, never the repository's own folder, which can hold unmerged work (BLD-D18).
#     repo = Path(os.environ.get("ARBOL_CONFORMANCE_REPO", str(Path.home() / "repos/sylvan-stack/readonly/arbol"))).resolve()
#     if read_object(repo / "package.json").get("name") != "@sylvan-stack/arbol":
#         raise ValueError("ARBOL_CONFORMANCE_REPO must name the Arbol repository.")
#     # The shared suite is standard-library Python; the Build's own interpreter runs it, with nothing to install.
#     return run([str(folder / "runtime/python/bin/python3.14"), "-I", "-B", str(repo / "conformance/bin/arbol-conformance.py"),
#                 "--driver", shlex.quote(str(folder / ENTRY))], cwd=folder)
# TEST-TIER-DISABLED-END scripts/check_build.py::contract
def contract(folder: Path) -> int:
    # By default the clone of the last merged main, never the repository's own folder, which can hold unmerged work (BLD-D18).
    raise RuntimeError('Tests are commented under the tier policy; see TESTS.md. Restore only in an explicitly requested testing task.')  # TEST-TIER-REFUSAL


# TEST-TIER-DISABLED-BEGIN scripts/check_build.py::live
# def live(folder: Path) -> int:
#     home = Path(pwd.getpwuid(os.getuid()).pw_dir)
#     # The test Subscriptions live in a state directory of their own, not in the default one (DRV-D41).
#     state = os.environ.get("ARBOL_BUILD_TEST_STATE_DIR", str(home / "Library/Application Support/Arbol/drivers/arbol-claude-driver"))
#     if not state or not Path(state).is_absolute():
#         raise ValueError("ARBOL_BUILD_TEST_STATE_DIR must be an absolute test state directory.")
#     env = {"HOME": str(home), "PATH": f"{home / '.local/bin'}:/usr/bin:/bin", "LANG": os.environ.get("LANG", "C")}
#     return run([str(folder / ENTRY), "dev", "build-check", "--state-dir", state], cwd=folder, env=env)
# TEST-TIER-DISABLED-END scripts/check_build.py::live
def live(folder: Path) -> int:
    raise RuntimeError('Tests are commented under the tier policy; see TESTS.md. Restore only in an explicitly requested testing task.')  # TEST-TIER-REFUSAL


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("metadata", "unit", "contract", "live"))
    parser.add_argument("--candidate", help="Absolute development candidate folder")
    arguments = parser.parse_args()
    try:
        folder, _ = build_folder(arguments.candidate)
        manifest = verify(folder, arguments.candidate is not None)
        if arguments.mode == "unit" and Path(sys.executable).resolve() != (folder / "runtime/python/bin/python3.14").resolve():
            return run([str(folder / "runtime/python/bin/python3.14"), "-I", str(folder / "scripts/check_build.py"), "unit",
                        *(["--candidate", str(folder)] if arguments.candidate else [])], cwd=folder)
        if arguments.mode == "metadata":
            print(json.dumps({"component": COMPONENT, "entry": ENTRY, "python": manifest["python"], "passed": True}))
            return 0
        return {"unit": unit, "contract": contract, "live": live}[arguments.mode](folder)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Cannot check {COMPONENT}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

# Archived complete imports; restore needed names in their original scope, not at this file tail.
# TEST-TIER-IMPORTS-BEGIN scripts/check_build.py:8
# import pwd
# TEST-TIER-IMPORTS-END scripts/check_build.py:8
# TEST-TIER-IMPORTS-BEGIN scripts/check_build.py:9
# import shlex
# TEST-TIER-IMPORTS-END scripts/check_build.py:9
# TEST-TIER-IMPORTS-BEGIN scripts/check_build.py:12
# import unittest
# TEST-TIER-IMPORTS-END scripts/check_build.py:12
