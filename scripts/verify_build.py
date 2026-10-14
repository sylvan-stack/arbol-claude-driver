"""Rehearse standalone delivery, failed checks and withdrawal in an isolated registry.

The three checks used here are explicitly synthetic registry checks. They send
no vendor request and do not establish that a Driver is ready for production.
The repository's real arbol-build.json continues to require its actual tests,
contract suite and one-message live check.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_support import COMPONENT, ENTRY, ROOT, metadata  # noqa: E402
from check_build import verify as verify_package  # noqa: E402

COPY_PROGRAM = '''import json, os, shutil, sys
from pathlib import Path
source = Path(sys.argv[1])
destination = Path(os.environ["ARBOL_BUILD_DIR"])
for path in source.iterdir():
    if path.name in ("build.json", "build.log"):
        continue
    if path.is_dir():
        shutil.copytree(path, destination / path.name, symlinks=True)
    else:
        shutil.copy2(path, destination / path.name)
manifest = json.loads((destination / "package-manifest.json").read_text())
manifest["candidate"] = False
manifest["build"] = os.environ["ARBOL_BUILD_NUMBER"]
(destination / "package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\\n")
'''


def command(arguments: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=120)


def response(arguments: list[str], cwd: Path, env: dict[str, str], expected: int = 0) -> dict[str, Any]:
    result = command(arguments, cwd, env)
    if result.returncode != expected:
        raise ValueError(f"Expected exit {expected}, got {result.returncode}: {result.stderr or result.stdout}")
    value: object = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object from the executable.")
    return value


def verify(candidate: Path, arbol: Path) -> dict[str, Any]:
    manifest = verify_package(candidate, True)
    version, _ = metadata(candidate)
    report: dict[str, Any] = {"check": "migration.offline-build-rehearsal", "at": datetime.now(UTC).isoformat(),
                              "component": COMPONENT, "candidate": manifest, "liveRequests": 0, "checks": []}
    with tempfile.TemporaryDirectory(prefix="syl95-build-rehearsal-") as name:
        temporary = Path(name)
        repo, registry, binary, home = (temporary / part for part in ("repo", "registry", "bin", "home"))
        repo.mkdir()
        home.mkdir()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C"}
        relocated = temporary / "relocated candidate"
        shutil.copytree(candidate, relocated, symlinks=True)
        direct = response([str(relocated / ENTRY), "info"], temporary, env)
        if direct.get("version") != version or direct.get("build") != "0":
            raise ValueError("Relocated candidate must report its package version and development build 0.")
        report["relocatedInfo"] = direct
        report["checks"].append({"check": "build.folder-alone", "passed": True})
        relocated_unit = command([str(relocated / "runtime/python/bin/python3.14"), "-I",
                                  str(relocated / "scripts/check_build.py"), "unit", "--candidate", str(relocated)],
                                 temporary, env)
        if relocated_unit.returncode:
            raise ValueError(f"Relocated packaged unit tests failed: {relocated_unit.stderr or relocated_unit.stdout}")
        report["relocatedUnit"] = {"exitCode": relocated_unit.returncode, "stdout": relocated_unit.stdout,
                                   "stderr": relocated_unit.stderr}
        report["checks"].append({"check": "build.relocated-unit", "passed": True})
        (repo / "package.json").write_text(json.dumps({"version": version}) + "\n")
        (repo / "copy_program.py").write_text(COPY_PROGRAM)
        # Preserve the production descriptor by creating a separate fixture repo.
        import shlex

        config: dict[str, Any] = {
            "component": COMPONENT, "entry": ENTRY, "contract": 1, "dataFormat": 1,
            "build": [shlex.join([str(candidate / "runtime/python/bin/python3.14"), str(repo / "copy_program.py"), str(candidate)])],
            "checks": [{"name": f"rehearsal.{kind}", "kind": kind, "command": f'"$ARBOL_BUILD_DIR/{ENTRY}" info'}
                       for kind in ("unit", "contract", "live")],
        }
        descriptor = repo / "arbol-build.json"
        descriptor.write_text(json.dumps(config, indent=2) + "\n")
        for arguments in (["git", "init", "--quiet"], ["git", "add", "."],
                          ["git", "-c", "user.name=Build rehearsal", "-c", "user.email=build-rehearsal@localhost", "commit", "--quiet", "-m", "Isolated migration Build rehearsal"]):
            result = command(arguments, repo, env)
            if result.returncode:
                raise ValueError(result.stderr)
        # The Python arbol-builds of the arbol checkout (SYL-165); its launchers need no Node.js.
        tool = [str(arbol / "bin/arbol-builds"), "--registry-dir", str(registry), "--bin-dir", str(binary)]
        builds = [response([*tool, "build", COMPONENT, "--repo", str(repo)], temporary, env) for _ in range(2)]
        if [record.get("status") for record in builds] != ["working", "working"]:
            raise ValueError("Isolated synthetic checks must create two runnable rehearsal Builds.")
        launcher = str(binary / COMPONENT)
        for expected, arguments in (("2", ["info"]), ("1", ["-v", "1", "info"])):
            info = response([launcher, *arguments], temporary, env)
            if info.get("build") != expected or info.get("version") != version:
                raise ValueError("The launcher selected the wrong Build identity.")
        report["checks"].append({"check": "build.launcher-default-and-named", "passed": True})
        config["checks"].insert(0, {"name": "rehearsal.intentional-failure", "kind": "unit",
                                     "command": f'"$ARBOL_BUILD_DIR/{ENTRY}" definitely-invalid-command'})
        descriptor.write_text(json.dumps(config, indent=2) + "\n")
        failed = response([*tool, "build", COMPONENT, "--repo", str(repo)], temporary, env, 1)
        if failed.get("status") != "failed" or failed.get("failure", {}).get("name") != "rehearsal.intentional-failure":
            raise ValueError("The deliberate failing check must be kept as a failed Build.")
        if response([launcher, "info"], temporary, env).get("build") != "2":
            raise ValueError("The deliberately failed Build changed the prior default.")
        if not (registry / COMPONENT / "3/build.log").is_file():
            raise ValueError("The failed Build must retain its log.")
        # BLD-D13: a failed Build named with -v starts, and says on standard error which step failed.
        named = command([launcher, "-v", "3", "info"], temporary, env)
        failed_line = f"{COMPONENT}@3 is failed: check rehearsal.intentional-failure: Check failed; see build.log.\n"
        if named.returncode != 0 or json.loads(named.stdout).get("build") != "3" or named.stderr != failed_line:
            raise ValueError(f"A failed Build named with -v must start and name its failed step: {named.stderr!r}")
        report["checks"].append({"check": "build.failed-check-preserves-default", "passed": True})
        withdrawn = response([*tool, "withdraw", f"{COMPONENT}@2", "--reason", "Isolated migration rollback rehearsal"], temporary, env)
        if withdrawn.get("status") != "withdrawn":
            raise ValueError("The isolated newest working Build was not withdrawn.")
        if response([launcher, "info"], temporary, env).get("build") != "1":
            raise ValueError("Withdrawing the newest Build must restore the previous default.")
        if response([launcher, "-v", "2", "info"], temporary, env).get("build") != "2":
            raise ValueError("A withdrawn Build must remain explicitly runnable.")
        report["checks"].append({"check": "build.withdrawal-rollback", "passed": True})
        report.update({"workingRehearsals": builds, "failedRehearsal": failed, "withdrawnRehearsal": withdrawn, "outcome": "passed"})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--arbol-repo", default=str(ROOT.parent / "arbol"))
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    try:
        report = verify(Path(arguments.candidate).resolve(strict=True), Path(arguments.arbol_repo).resolve(strict=True))
        Path(arguments.output).write_text(json.dumps(report, indent=2) + "\n")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Build rehearsal failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"outcome": "passed", "evidence": str(Path(arguments.output).resolve()), "checks": report["checks"], "liveRequests": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
