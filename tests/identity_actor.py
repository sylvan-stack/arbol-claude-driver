"""Fixed H1 test helper; no gate wiring, runtime use or other actor action yet."""
from __future__ import annotations

# The standalone path validates isolated runtime confinement before importing
# anything beyond these built-in/frozen interpreter modules.
import sys
import os


class ActorFailure(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ActorFailure(message)


def _inside(path: str, root: str) -> bool:
    return bool(path and os.path.isabs(path) and os.path.commonpath((os.path.realpath(path), root)) == root)


class _Directory:
    """Existing private physical directories; this actor never creates a namespace."""

    def __init__(self, path: Path, descriptor: int):
        import stat

        self.path, self.descriptor = path, descriptor
        info = os.fstat(descriptor)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o700, "Unsafe actor fixture directory.")
        self.identity = (info.st_dev, info.st_ino)
        self.verify()

    @classmethod
    def root(cls, path: Path) -> _Directory:
        _require(path.is_absolute() and path.resolve(strict=True) == path, "Actor registry is not physical.")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            return cls(path, descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def verify(self) -> None:
        import stat

        info, named = os.fstat(self.descriptor), self.path.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o700
                 and (info.st_dev, info.st_ino) == self.identity == (named.st_dev, named.st_ino),
                 "Actor fixture directory changed.")

    def existing(self, name: str) -> _Directory:
        _require(bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name,
                 "Invalid actor namespace component.")
        self.verify()
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=self.descriptor)
        try:
            result = _Directory(self.path / name, descriptor)
            named = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            _require(result.identity == (named.st_dev, named.st_ino), "Actor child directory changed.")
            self.verify()
            return result
        except BaseException:
            os.close(descriptor)
            raise

    def read(self, name: str, *, limit: int = 4096) -> bytes:
        import stat

        _require(bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name,
                 "Invalid actor evidence name.")
        self.verify()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=self.descriptor)
        try:
            before = os.fstat(descriptor)
            _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                     and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1 and before.st_size <= limit,
                     "Unsafe actor fixture evidence.")
            raw = bytearray()
            while len(raw) <= limit:
                part = os.read(descriptor, min(65536, limit + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            after, named = os.fstat(descriptor), os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            _require(len(raw) <= limit and after.st_nlink == 1
                     and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                          before.st_uid, before.st_mode) ==
                         (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_uid, after.st_mode)
                     and (after.st_dev, after.st_ino) == (named.st_dev, named.st_ino),
                     "Actor fixture evidence changed.")
            self.verify()
            return bytes(raw)
        finally:
            os.close(descriptor)

    def exclusive(self, name: str, raw: bytes) -> None:
        import stat

        _require(name == "h1-capture.json" and 0 < len(raw) <= 4096, "Invalid fixed capture observation.")
        self.verify()
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.descriptor)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                _require(written > 0, "Actor capture write made no progress.")
                view = view[written:]
            os.fsync(descriptor)
            info, named = os.fstat(descriptor), os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                     and (info.st_dev, info.st_ino) == (named.st_dev, named.st_ino), "Actor capture name changed.")
            os.fsync(self.descriptor)
            self.verify()
        finally:
            os.close(descriptor)

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            os.close(descriptor)


def _lease_hold(admission: Admission, channel: Socket, case: str) -> None:
    """No State/Subscription operation occurs between capture-return and control."""
    import asyncio
    import socket
    import time
    from pathlib import Path

    from arbol_claude_driver.identity_protocol import canonical, decode, fields, valid_token
    from arbol_claude_driver.state import State
    from arbol_claude_driver.subscriptions import Subscriptions

    context = admission.context
    _require(context["component"] == "arbol-claude-driver" and context["entry"] == "tests/identity_actor.py"
             and context["role"] == "helper" and valid_token(context["invocation"])
             and valid_token(context["parent"]["writer"]) and not admission.closed,
             "Actor does not own the actual fixed helper admission.")
    directories: list[_Directory] = []
    state: State | None = None
    original: BaseException | None = None
    try:
        current = _Directory.root(Path(context["registry"]))
        directories.append(current)
        for name in (".driver-qualification", "fixtures", context["invocation"], case):
            current = current.existing(name)
            directories.append(current)
        fixture = current
        marker = decode(fixture.read(".fixture-owner.json"))
        fields(marker, "version case component invocation parentWriter payloadSha256")
        _require(marker == {"version": 1, "case": case, "component": "arbol-claude-driver",
                            "invocation": context["invocation"], "parentWriter": context["parent"]["writer"],
                            "payloadSha256": admission.payload.digest}, "Actor fixture ownership differs.")
        state_directory = fixture.existing("state")
        directories.append(state_directory)
        vendor_directory = fixture.existing("vendor")
        directories.append(vendor_directory)
        _require(state_directory.read("format") == b"2\n", "H1 requires its genuinely prepared local format-two state.")
        state = State(state_directory.path)
        state.initialize(admission)
        identity = state.identity
        _require(identity is not None, "H1 state guard is unavailable.")
        if identity is None:
            raise AssertionError
        _require(channel.fileno() not in (admission.lease, identity.descriptor), "Actor control aliases a held guard.")
        subscriptions = Subscriptions(state, {}, [], vendor_directory.path / "settings.json")
        # This is the real wrapper. asyncio.run also finishes its loop/task
        # cleanup before return; the short-lock finally has already run.
        proof = asyncio.run(subscriptions.identity("anthropic-2"))
        fields(proof, "version driver subscription generation witness")
        _require(proof["driver"] == "claude" and proof["subscription"] == "anthropic-2"
                 and valid_token(proof["generation"]) and valid_token(proof["witness"]),
                 "The real local capture did not return a supported proof.")
        fixture.exclusive("h1-capture.json", canonical(proof))
        ready = canonical({"version": 1, "kind": "actor-ready", "case": case,
                           "action": "lease-hold", "window": "H1-independent-lease"})
        deadline = time.monotonic() + 10
        channel.settimeout(max(0.001, deadline - time.monotonic()))
        channel.sendall(ready)
        raw = bytearray()
        while b"\n" not in raw:
            remaining = deadline - time.monotonic()
            _require(remaining > 0, "H1 parent control expired.")
            channel.settimeout(remaining)
            part = channel.recv(4097 - len(raw))
            _require(bool(part), "H1 parent EOF is failure.")
            raw.extend(part)
            _require(len(raw) <= 4096, "H1 parent control exceeded its bound.")
        response = decode(bytes(raw))
        fields(response, "version kind case command")
        _require(response == {"version": 1, "kind": "actor-control", "case": case, "command": "continue"},
                 "H1 accepts only its one fixed parent continue.")
        channel.setblocking(False)
        try:
            _require(not channel.recv(1, socket.MSG_PEEK), "Extra H1 parent control data.")
        except BlockingIOError:
            pass
        # Positive or failed cleanup below performs no further Subscription
        # operation. It closes only the actual local guard and owned handles.
    except BaseException as error:
        original = error
        raise
    finally:
        errors: list[BaseException] = []
        if state is not None:
            try:
                state.close()
            except BaseException as error:
                errors.append(error)
        for directory in reversed(directories):
            try:
                directory.close()
            except BaseException as error:
                errors.append(error)
        try:
            channel.close()
        except BaseException as error:
            errors.append(error)
        if errors:
            if original is not None:
                for cleanup_error in errors:
                    original.add_note(f"H1 cleanup failed: {cleanup_error!r}.")
            else:
                raise BaseExceptionGroup("H1 local cleanup failed; no terminal claim.", errors)


def _bootstrap() -> int:
    script = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(script))
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode
            or not sys.flags.ignore_environment or not sys.flags.safe_path
            or os.path.realpath(root) != root or os.path.basename(root) != "driver-payload"
            or os.path.realpath(script) != script
            or any(not _inside(path, root) for path in (sys.executable, sys.prefix, sys.base_prefix,
                                                       sys.exec_prefix, sys.base_exec_prefix, *sys.path))
            or os.__spec__ is None or os.__spec__.origin != "frozen"):
        os.write(2, b"H1 helper requires the fixed isolated admitted payload.\n")
        return 2
    # Standard-library imports now resolve only within the bundled runtime.
    import json
    import re
    import signal
    import socket
    from pathlib import Path

    def expired(number: int, frame: FrameType | None) -> None:
        raise TimeoutError("The fixed actor lifetime expired.")

    previous_alarm = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 120)
    admission: Admission | None = None
    channel: socket.socket | None = None
    case: str | None = None
    try:
        _require(len(sys.argv) == 6 and all(word.isascii() for word in sys.argv[1:]), "Invalid fixed H1 arguments.")
        action, selector, window, case, control_text = sys.argv[1:]
        _require((action, selector, window) == ("lease-hold", "anthropic-2", "H1-independent-lease")
                 and re.fullmatch(r"[0-9a-f]{32}", case, re.ASCII) is not None
                 and re.fullmatch(r"[1-9][0-9]{0,4}", control_text, re.ASCII) is not None
                 and 3 <= int(control_text) <= 65535, "Invalid fixed H1 action or namespace.")
        descriptor = Path(root) / "driver-capability.json"
        with descriptor.open("rb") as stream:
            raw = stream.read(4097)
        _require(0 < len(raw) <= 4096, "Invalid bounded payload declaration.")
        declaration = json.loads(raw.decode("utf-8", "strict"))
        _require(isinstance(declaration, dict) and isinstance(declaration.get("importRoots"), list),
                 "Missing fixed import roots.")
        roots: list[str] = []
        for name in declaration["importRoots"]:
            _require(isinstance(name, str) and bool(name) and "\\" not in name and not os.path.isabs(name)
                     and all(part not in ("", ".", "..") for part in name.split("/")), "Unsafe import root.")
            candidate = os.path.join(root, name)
            _require(_inside(candidate, root) and (os.path.isdir(candidate) or name.endswith(".zip")
                                                  and os.path.isfile(candidate)), "Import root escaped payload.")
            roots.append(candidate)
        _require(bool(roots), "Missing bundled import roots.")
        sys.path[:] = roots
        from arbol_claude_driver.admission import AUTHORITY_ENV, Admission, NORMAL_FD, descriptor_number
        from arbol_claude_driver.identity_protocol import canonical, load_payload

        payload = load_payload(Path(root), "arbol-claude-driver", str(declaration.get("build", "")))
        entry = os.path.relpath(script, root).replace(os.sep, "/")
        _require(entry == "tests/identity_actor.py" and entry in payload.descriptor["helpers"]
                 and sys.executable == str(payload.root / payload.descriptor["interpreter"])
                 and Path(sys.executable).resolve(strict=True) == payload.root / payload.descriptor["interpreter"]
                 and sys.path == [str(payload.root / name) for name in payload.descriptor["importRoots"]],
                 "H1 source/runtime/helper declaration differs.")
        control_fd = int(control_text)
        _require(NORMAL_FD in os.environ and control_fd != descriptor_number(os.environ[NORMAL_FD]),
                 "H1 control aliases admission authority.")
        channel = socket.socket(fileno=control_fd)
        _require(channel.family == socket.AF_UNIX and channel.type == socket.SOCK_STREAM,
                 "H1 control is not an AF_UNIX stream.")
        channel.getpeername()  # It must actually be connected, not a listening socket.
        channel.set_inheritable(False)
        admission = Admission.receive(payload, entry, dict(os.environ))
        _require(control_fd != admission.lease, "H1 control aliases independent admission.")
        for key in AUTHORITY_ENV:
            os.environ.pop(key, None)
        try:
            _lease_hold(admission, channel, case)
            channel = None
        except BaseException:
            # No success/terminal reconstruction follows a failed body or EOF.
            # The kernel closes leases on exit; durable unknown custody remains.
            raw_result = canonical({"version": 1, "kind": "actor-result", "case": case,
                                    "action": "lease-hold", "outcome": "refused"})
            os.write(1, raw_result)
            os.write(2, b"H1 local action failed; preserve custody and private evidence.\n")
            return 1
        raw_result = canonical({"version": 1, "kind": "actor-result", "case": case,
                                "action": "lease-hold", "outcome": "completed"})
        written = os.write(1, raw_result)
        _require(written == len(raw_result), "H1 final result output was incomplete.")
        # All state/loop/control/directory resources and final output are done.
        # Only the genuine API can publish this actor's terminal record.
        admission.finish()
        return 0
    except BaseException:
        os.write(2, b"H1 helper unavailable; preserve the actual failed admission.\n")
        return 2
    finally:
        if channel is not None:
            channel.close()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)


# No runtime application imports, test-root insertion or source fallback occurs
# on library import. The standalone bootstrap above is the only actor entry.
if __name__ != "__main__":
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from pathlib import Path
        from socket import socket as Socket
        from types import FrameType
        from arbol_claude_driver.admission import Admission

if __name__ == "__main__":
    raise SystemExit(_bootstrap())
