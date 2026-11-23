"""Check the program in its final Build folder, without starting a registry launcher."""

from __future__ import annotations
import pwd
import unittest


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


def unit(folder: Path) -> int:
    sys.path.insert(0, str(folder))
    sys.path.insert(0, str(folder / "src"))
    os.chdir(folder)
    # A Build runs the top tier only (SYL-329); the mid tier runs only when the owner asks.
    suite = unittest.TestLoader().discover(str(folder / "tests/top"), top_level_dir=str(folder / "tests"))
    if suite.countTestCases() == 0:
        raise ValueError("The packaged unit check found no top-tier tests.")
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def contract(folder: Path) -> int:
    # A Build runs the top tier only (SYL-329). The contract scenarios are mid tier; they run only when the owner asks.
    print("contract: no top-tier scenario; the mid-tier scenarios run only when the owner asks (SYL-329).", flush=True)
    return 0


async def observe_native_retention(
    folder: Path, provider_build: str, acceptance_digest: str, native_sha256: str,
) -> tuple[Path, dict[str, bool]]:
    """Prepare a fresh disposable Driver record for the existing native gate. Step 53 owns wiring."""
    from contextlib import ExitStack
    from datetime import UTC, datetime, timedelta
    import stat
    import uuid

    from arbol_claude_driver.admission import verify_acceptance
    from arbol_claude_driver.identity_protocol import (
        BUILD, Authority, canonical, decode, load_payload, private_directory, read_bytes, valid_hash, valid_token,
    )
    from arbol_claude_driver.processes import child_command

    def ensure(ok: bool, message: str) -> None:
        if not ok:
            raise ValueError(message)

    ensure(folder.is_absolute() and folder.resolve(strict=True) == folder and folder.parent.name == COMPONENT
           and BUILD.fullmatch(folder.name) is not None and BUILD.fullmatch(provider_build) is not None
           and valid_hash(acceptance_digest) and valid_hash(native_sha256), "Invalid native qualification inputs.")
    registry = folder.parent.parent
    producer = load_payload(folder / "driver-payload", COMPONENT, folder.name)
    provider = load_payload(registry / "arbol" / provider_build / "driver-payload", "arbol", provider_build)
    authority = Authority(registry, COMPONENT)
    owner = pwd.getpwuid(os.getuid())
    home = Path(owner.pw_dir)
    native_link = home / ".local/bin/claude"
    native = native_link.resolve(strict=True)

    def verify_inputs() -> None:
        physical_producer = load_payload(producer.root, COMPONENT, producer.build)
        physical_provider = load_payload(provider.root, "arbol", provider.build)
        record = verify_acceptance(authority, physical_producer, acceptance_digest)
        ensure(physical_producer == producer and physical_provider == provider and record["status"] == "candidate"
               and physical_provider.digest == record["providerPayloadSha256"],
               "This observation requires the original accepted physical Registry and payloads.")
        for module, relative in (("arbol_claude_driver.admission", "src/arbol_claude_driver/admission.py"),
                                 ("arbol_claude_driver.identity_protocol", "src/arbol_claude_driver/identity_protocol.py"),
                                 ("arbol_claude_driver.processes", "src/arbol_claude_driver/processes.py"),
                                 ("build_support", "scripts/build_support.py")):
            ensure(Path(sys.modules[module].__file__ or "").resolve(strict=True) == producer.root / relative,
                   "An imported observer dependency differs from the accepted package.")
        ensure(native_link.resolve(strict=True) == native and native.is_file() and os.access(native, os.X_OK)
               and not native.stat().st_mode & 0o022 and digest(native) == native_sha256,
               "The bound Claude Code executable changed.")

    verify_inputs()
    packaged_tests = producer.root / "tests"
    sys.path.insert(0, str(packaged_tests))
    try:
        from identity_support import Directory
    finally:
        sys.path.remove(str(packaged_tests))
    ensure(Path(sys.modules[Directory.__module__].__file__ or "").resolve(strict=True)
           == packaged_tests / "identity_support.py", "The fixture helper differs from the accepted package.")
    profile = home / ".claude"
    ensure(profile.resolve(strict=True) == profile and profile.is_dir(), "The existing native Claude profile is required.")
    native_env = {"HOME": str(home), "USER": owner.pw_name, "PATH": f"{home / '.local/bin'}:/usr/bin:/bin", "LANG": "C"}
    prefix = [*provider.prefix, "--qualification-launch", str(registry), COMPONENT, folder.name, "driver", "--"]
    # Physical sibling fixture root is created by the original qualification workspace.
    ensure(registry.name == "builds", "Expected the original candidate workspace registry.")
    with ExitStack() as stack:
        profile_fd = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, profile_fd)
        profile_stat = os.fstat(profile_fd)
        native_fd = os.open(native, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        stack.callback(os.close, native_fd)
        native_stat = os.fstat(native_fd)

        def verify_profile() -> None:
            current = profile.lstat()
            held = os.fstat(profile_fd)
            binary = native.lstat()
            ensure(stat.S_ISREG(native_stat.st_mode) and stat.S_ISREG(binary.st_mode)
                   and (native_stat.st_dev, native_stat.st_ino, native_stat.st_size, native_stat.st_mtime_ns) ==
                   (binary.st_dev, binary.st_ino, binary.st_size, binary.st_mtime_ns),
                   "The original native executable was replaced or changed.")
            ensure(profile.resolve(strict=True) == profile and stat.S_ISDIR(current.st_mode)
                   and stat.S_ISDIR(held.st_mode) and held.st_uid == current.st_uid == os.getuid()
                   and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino) ==
                   (profile_stat.st_dev, profile_stat.st_ino), "The original native profile changed.")

        verify_profile()

        def owned(path: Path) -> Directory:
            value = Directory.root(path)
            stack.callback(value.close)
            return value

        def create(parent: Directory, name: str) -> Directory:
            value = parent.child(name, fresh=True)
            stack.callback(value.close)
            return value

        fixtures = owned(registry.parent / "fixtures")
        case = create(fixtures, "claude-native-" + uuid.uuid4().hex)
        state = create(case, "state")
        local_home = create(case, "home")
        settings = create(local_home, ".arbol")
        # Only the disposable Driver home sees this pin. The real harness owns authentication in its normal HOME.
        pinned_settings = canonical({"toolPath": str(native)})
        settings.exclusive("arbol-claude-driver.json", pinned_settings)
        driver_env = {**native_env, "HOME": str(local_home.path)}

        async def command(argv: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
            for directory in (fixtures, case, state, local_home, settings):
                directory.verify()
            ensure(settings.read("arbol-claude-driver.json") == pinned_settings, "The disposable tool pin changed.")
            verify_inputs()
            verify_profile()
            result = await child_command(argv[0], argv[1:], env, cwd, timeout=40, capture_stderr=True)
            verify_inputs()
            verify_profile()
            ensure(result["code"] == 0 and not result["stderr"], "Native retention operation refused or reported an error.")
            return decode(result["stdout"].encode(), limit=65536, exact=False)

        async def public(*args: str) -> dict[str, Any]:
            return await command([*prefix, "--state-dir", str(state.path), *args], driver_env, case.path)

        async def confirm_native() -> None:
            status = await command([str(native), "auth", "status", "--json"], native_env, home)
            ensure(status.get("loggedIn") is True and status.get("authMethod") == "claude.ai"
                   and status.get("configDirectory") == str(profile), "Native Subscription authentication is unavailable.")

        async def proof(original: dict[str, Any] | None = None) -> dict[str, Any]:
            expected = [] if original is None else ["--expect", original["generation"] + "." + original["witness"]]
            received = await public("subscriptions", "identity", "anthropic", *expected)
            ensure(set(received) == {"version", "driver", "subscription", "generation", "witness"}
                   and type(received["version"]) is int and received["version"] == 1
                   and received["driver"] == "claude" and received["subscription"] == "anthropic"
                   and valid_token(received["generation"]) and valid_token(received["witness"])
                   and (original is None or received == original), "The ordinary proof changed.")
            return received

        def stored() -> tuple[bytes, dict[str, Any]]:
            private_directory(state.path / "subscriptions")
            raw = read_bytes(state.path / "subscriptions/anthropic/record.json", limit=65536)
            record = decode(raw, limit=65536, exact=False)
            ensure(record.get("id") == "anthropic" and record.get("family") == "anthropic"
                   and record.get("state") == "signed_in" and record.get("auth") == "vendor_login",
                   "A signed-in native Subscription was not persisted.")
            return raw, record

        async def refresh() -> tuple[bytes, dict[str, Any], datetime]:
            info = await public("info")
            tool = info.get("tool")
            ensure(info.get("driver") == "claude" and info.get("stateDirectory") == str(state.path)
                   and info.get("settingsFile") == str(settings.path / "arbol-claude-driver.json")
                   and isinstance(tool, dict) and tool.get("supported") is True and tool.get("path") == str(native),
                   "The actual Driver did not select the bound installed Claude Code executable.")
            await confirm_native()
            start = datetime.now(UTC)
            returned = await public("subscriptions", "check", "anthropic")
            end = datetime.now(UTC)
            image, persisted = stored()
            ensure(returned.get("id") == "anthropic" and returned.get("state") == "signed_in"
                   and returned.get("checkedAt") == persisted.get("checkedAt")
                   and isinstance(persisted.get("checkedAt"), str), "The public refresh and persisted observation differ.")
            checked = datetime.fromisoformat(persisted["checkedAt"])
            ensure(checked.tzinfo is not None and start - timedelta(milliseconds=1) < checked <= end,
                   "The persisted native check time is outside this actual refresh.")
            return image, persisted, checked

        ensure(not os.path.lexists(state.path / "subscriptions"), "The native fixture was already populated.")
        _, first, first_time = await refresh()
        original = await proof()
        label = "ABR-38 native retention"
        updated = await public("subscriptions", "update", "anthropic", "--label", label)
        before, relabelled = stored()
        ensure(first.get("label") != label and updated.get("label") == label and relabelled.get("label") == label
               and relabelled.get("checkedAt") == first["checkedAt"], "The ordinary relabel was not persisted.")
        await proof(original)
        after, refreshed, last_time = await refresh()
        ensure(after != before and last_time > first_time and refreshed.get("label") == label,
               "The native refresh omitted its update or changed the ordinary relabel.")
        await proof(original)
        verify_inputs()
        verify_profile()
        return state.path, {"nativeSubscription": True, "relabelPersisted": True, "freshCheckPersisted": True,
                            "originalProofRetained": True}


def live(folder: Path) -> int:
    owner = pwd.getpwuid(os.getuid())
    home = Path(owner.pw_dir)
    # The owner's live qualification scope selects an existing real profile directly.
    state = os.environ.get("ARBOL_BUILD_TEST_STATE_DIR", str(home / ".arbol/drivers/claude"))
    subscription = os.environ.get("ARBOL_BUILD_LIVE_SUBSCRIPTION", "anthropic")
    if not state or not Path(state).is_absolute():
        raise ValueError("ARBOL_BUILD_TEST_STATE_DIR must name an absolute existing Driver state directory.")
    if subscription != "anthropic":
        raise ValueError("ARBOL_BUILD_LIVE_SUBSCRIPTION must name the existing native anthropic Subscription.")
    if not (Path(state) / "format").is_file() or not (Path(state) / "subscriptions" / subscription / "record.json").is_file():
        raise ValueError("Live qualification requires an existing registered Subscription.")
    # claude finds its Keychain login under the account named by USER; without it the login is invisible.
    env = {"HOME": str(home), "USER": owner.pw_name, "PATH": f"{home / '.local/bin'}:/usr/bin:/bin", "LANG": os.environ.get("LANG", "C"),
           "ARBOL_BUILD_LIVE_SUBSCRIPTION": subscription}
    return run([str(folder / ENTRY), "dev", "build-check", "--state-dir", state], cwd=folder, env=env)


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
