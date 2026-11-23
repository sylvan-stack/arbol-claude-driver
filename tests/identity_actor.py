"""Fixed H1 and prospective create-stop helper; no gate wiring or runtime use."""
from __future__ import annotations

# The standalone path validates isolated runtime confinement before importing
# anything beyond these built-in/frozen interpreter modules.
import sys
import os


# This belongs to this fixed actor process, never control data or metadata.
# It is set before the real owning finish call and is never cleared.
_FINISH_MAY_HAVE_BEGUN = False


def _preterminal_diagnostic(raw: bytes) -> None:
    if not _FINISH_MAY_HAVE_BEGUN:
        os.write(2, raw)


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


def _create_stop(admission: Admission, channel: Socket, case: str, window: str) -> str:
    """Pause only after the real create publication has fsynced its directory."""
    import asyncio
    import socket
    import time
    from pathlib import Path
    from typing import Any

    from arbol_claude_driver.identity_protocol import canonical, decode, fields, sha256, valid_token
    from arbol_claude_driver.state import State
    from arbol_claude_driver.subscriptions import Subscriptions

    context = admission.context
    _require(context["component"] == "arbol-claude-driver" and context["entry"] == "tests/identity_actor.py"
             and context["role"] == "helper" and valid_token(context["invocation"])
             and valid_token(context["parent"]["writer"]) and not admission.closed,
             "Create actor does not own the actual helper admission.")
    directories: list[_Directory] = []
    state: State | None = None
    original: BaseException | None = None
    real_fsync = os.fsync
    try:
        registry_path = Path(context["registry"])
        _require(registry_path.name == "builds", "Create actor needs the fixed workspace registry.")
        workspace = _Directory.root(registry_path.parent)
        directories.append(workspace)
        registry = workspace.existing("builds")
        directories.append(registry)
        _require(registry.path == registry_path and admission.authority.identity == {
            "path": str(registry.path), "device": registry.identity[0], "inode": registry.identity[1]}
            and sha256(canonical(admission.authority.identity)) == context["registrySha256"],
            "Create actor registry differs from admission.")
        current = workspace.existing("fixtures")
        directories.append(current)
        for name in ("arbol-claude-driver", "create-publication", context["invocation"], case):
            current = current.existing(name)
            directories.append(current)
        fixture = current
        marker = decode(fixture.read(".fixture-owner.json"))
        fields(marker, "version case component invocation parentWriter payloadSha256")
        _require(marker == {"version": 1, "case": case, "component": "arbol-claude-driver",
                            "invocation": context["invocation"], "parentWriter": context["parent"]["writer"],
                            "payloadSha256": admission.payload.digest}, "Create fixture ownership differs.")
        state_directory = fixture.existing("state")
        directories.append(state_directory)
        vendor_directory = fixture.existing("vendor")
        directories.append(vendor_directory)
        _require(state_directory.read("format") == b"2\n", "Create requires prepared format-two State.")
        state = State(state_directory.path)
        state.initialize(admission)
        identity = state.identity
        _require(identity is not None, "Create State guard unavailable.")
        if identity is None:
            raise AssertionError
        _require(channel.fileno() not in (admission.lease, identity.descriptor), "Create control aliases authority.")
        folder = state_directory.path / "subscriptions" / "anthropic-2"
        _require(not os.path.lexists(folder), "Create subject must be genuinely fresh.")
        subscriptions = Subscriptions(state, {}, [], vendor_directory.path / "settings.json")
        observed: list[str] = []
        witness: str | None = None
        fired = False
        injected = False

        def checkpoint(fd: Any) -> None:
            nonlocal witness, fired, injected
            real_fsync(fd)
            if fired or not folder.is_dir():
                return
            try:
                held = os.fstat(fd)
                named = folder.lstat()
                if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                    return
                with_dir = _Directory.root(folder)
            except FileNotFoundError:
                return
            try:
                def maybe(name: str) -> dict[str, object] | None:
                    try:
                        return decode(with_dir.read(name), exact=False)
                    except FileNotFoundError:
                        return None
                journal, record, sidecar = maybe("identity-pending.json"), maybe("record.json"), maybe("identity.json")
            finally:
                with_dir.close()
            phase: str | None = None
            if journal is not None:
                _require(journal.get("operation") == "create" and valid_token(journal.get("witness")),
                         "Unexpected create journal at actual fsync.")
                actual = str(journal["witness"])
                _require(witness is None or witness == actual, "Create witness changed across publication.")
                witness = actual
                if journal.get("phase") == "prepared":
                    if record is None and sidecar is None:
                        phase = "C1-prepared-durable"
                    elif record is not None and sidecar is None:
                        phase = "C2-record-durable"
                    elif record is not None and sidecar == {"version": 1, "state": "active", "witness": witness}:
                        phase = "C3-sidecar-durable"
                elif journal.get("phase") == "committed" and record is not None and sidecar == {
                    "version": 1, "state": "active", "witness": witness,
                }:
                    phase = "C4-committed-durable"
            elif witness is not None and record is not None and sidecar == {
                "version": 1, "state": "active", "witness": witness,
            } and "C4-committed-durable" in observed:
                phase = "C5-clear-durable"
            if phase is None or phase in observed:
                return
            order = ("C1-prepared-durable", "C2-record-durable", "C3-sidecar-durable",
                     "C4-committed-durable", "C5-clear-durable")
            _require(len(observed) < len(order) and phase == order[len(observed)],
                     "Create did not reach the prescribed durable order.")
            observed.append(phase)
            if phase != window:
                return
            fired = True
            ready = canonical({"version": 1, "kind": "actor-ready", "case": case,
                               "action": "create-stop", "window": window})
            deadline = time.monotonic() + 10
            channel.settimeout(max(.001, deadline - time.monotonic()))
            channel.sendall(ready)
            raw = bytearray()
            while b"\n" not in raw:
                remaining = deadline - time.monotonic()
                _require(remaining > 0, "Create parent control expired.")
                channel.settimeout(remaining)
                part = channel.recv(4097 - len(raw))
                _require(bool(part), "Create parent EOF is failure.")
                raw.extend(part)
                _require(len(raw) <= 4096, "Create parent control exceeded its bound.")
            response = decode(bytes(raw))
            fields(response, "version kind case command")
            _require(type(response.get("version")) is int and response.get("version") == 1
                     and response.get("kind") == "actor-control"
                     and response.get("case") == case and response.get("command") in ("continue", "abort-io"),
                     "Invalid create parent control.")
            channel.setblocking(False)
            try:
                channel.recv(1, socket.MSG_PEEK)
                raise ActorFailure("Extra create parent control data or EOF.")
            except BlockingIOError:
                pass
            if response["command"] == "abort-io":
                injected = True
                raise OSError("Fixed post-fsync create interruption.")

        os.fsync = checkpoint
        try:
            try:
                result = asyncio.run(subscriptions.create("anthropic", "Local create fixture", "anthropic-2"))
            except OSError:
                _require(injected, "Create failed without the requested post-fsync interruption.")
                outcome = "injected-failure"
            else:
                _require(fired and result["id"] == "anthropic-2", "Create acknowledgement was not reached.")
                outcome = "completed"
        finally:
            os.fsync = real_fsync
        return outcome
    except BaseException as error:
        original = error
        raise
    finally:
        os.fsync = real_fsync
        failures: list[BaseException] = []
        if state is not None:
            try:
                state.close()
            except BaseException as error:
                failures.append(error)
        for directory in reversed(directories):
            try:
                directory.close()
            except BaseException as error:
                failures.append(error)
        try:
            channel.close()
        except BaseException as error:
            failures.append(error)
        if failures:
            if original is not None:
                for failure in failures:
                    original.add_note("Create actor cleanup failed: " + repr(failure))
            else:
                raise BaseExceptionGroup("Create actor cleanup failed.", failures)


def _bootstrap() -> int:
    global _FINISH_MAY_HAVE_BEGUN

    script = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(script))
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode
            or not sys.flags.ignore_environment or not sys.flags.safe_path
            or os.path.realpath(root) != root or os.path.basename(root) != "driver-payload"
            or os.path.realpath(script) != script
            or any(not _inside(path, root) for path in (sys.executable, sys.prefix, sys.base_prefix,
                                                       sys.exec_prefix, sys.base_exec_prefix, *sys.path))
            or os.__spec__ is None or os.__spec__.origin != "frozen"):
        _preterminal_diagnostic(b"H1 helper requires the fixed isolated admitted payload.\n")
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
        _require(selector == "anthropic-2" and ((action == "lease-hold" and window == "H1-independent-lease")
                 or (action == "create-stop" and window in (
                     "C1-prepared-durable", "C2-record-durable", "C3-sidecar-durable",
                     "C4-committed-durable", "C5-clear-durable")))
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
            if action == "lease-hold":
                _lease_hold(admission, channel, case)
                outcome = "completed"
            else:
                outcome = _create_stop(admission, channel, case, window)
            channel = None
        except BaseException:
            # No success/terminal reconstruction follows a failed body or EOF.
            # The kernel closes leases on exit; durable unknown custody remains.
            raw_result = canonical({"version": 1, "kind": "actor-result", "case": case,
                                    "action": action, "outcome": "refused"})
            os.write(1, raw_result)
            _preterminal_diagnostic(b"H1 local action failed; preserve custody and private evidence.\n")
            return 1
        raw_result = canonical({"version": 1, "kind": "actor-result", "case": case,
                                "action": action, "outcome": outcome})
        written = os.write(1, raw_result)
        _require(written == len(raw_result), "H1 final result output was incomplete.")
        # All state/loop/control/directory resources and final output are done.
        # Only the genuine API can publish this actor's terminal record.
        _FINISH_MAY_HAVE_BEGUN = True
        admission.finish()
        return 1 if outcome == "injected-failure" else 0
    except BaseException:
        if _FINISH_MAY_HAVE_BEGUN:
            # Keep the actual API exception/disposition. The standalone owner
            # converts it to a quiet unsuccessful exit, never another output.
            raise
        _preterminal_diagnostic(b"H1 helper unavailable; preserve the actual failed admission.\n")
        return 2
    finally:
        original = sys.exception()
        cleanup_errors: list[BaseException] = []
        if channel is not None:
            try:
                channel.close()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            signal.signal(signal.SIGALRM, previous_alarm)
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if original is not None:
                # Exception notes are in-memory only: no post-finish file,
                # application output, State access or custody reconstruction.
                for cleanup_error in cleanup_errors:
                    original.add_note(f"Actor final resource cleanup failed: {cleanup_error!r}.")
            else:
                raise BaseExceptionGroup("Actor final resource cleanup failed.", cleanup_errors)


def _standalone() -> None:
    status = 2
    try:
        status = _bootstrap()
    except BaseException:
        _preterminal_diagnostic(b"H1 helper failed before a complete standalone exit.\n")
    finally:
        if _FINISH_MAY_HAVE_BEGUN:
            # Real finish/cleanup failure keeps status2. Normal status0 exists
            # only after finish AND finally return. Bypass Python finalizers
            # and automatic tracebacks at the post-finish process boundary.
            os._exit(status)
    raise SystemExit(status)


def _unhandled(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
    # A final footer/interrupt escape must not invoke Python's stderr traceback
    # printer after finish may have begun. Preterminal diagnostics stay bounded.
    try:
        _preterminal_diagnostic(b"H1 helper escaped its bounded standalone owner.\n")
    finally:
        os._exit(2)


# No runtime application imports, test-root insertion or source fallback occurs
# on library import. The standalone bootstrap above is the only actor entry.
if __name__ != "__main__":
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from pathlib import Path
        from socket import socket as Socket
        from types import FrameType, TracebackType
        from arbol_claude_driver.admission import Admission

if __name__ == "__main__":
    sys.excepthook = _unhandled
    _standalone()
