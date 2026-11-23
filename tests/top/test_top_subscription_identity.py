"""Prospective local proof and final H1 cases; no runner or inventory wiring."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import time
import unittest
import uuid
from dataclasses import dataclass
from typing import ClassVar

from arbol_claude_driver.identity_protocol import (
    Object, canonical, decode, fields, inspect_process, read_bytes, sha256, token, valid_hash, valid_token,
)
from arbol_claude_driver.admission import current_admission
from arbol_claude_driver.state import State
from arbol_claude_driver.core import Failure
from arbol_claude_driver.subscriptions import Authentication, Subscriptions
from identity_support import (
    ClaudeCapture, ClaudeInvocation, ClaudeLocalSubject, Directory, Fixture, H1Child, H1_ID,
    LocalResult, LocalResults, SuiteStreams, require,
)


class LocalIdentityTests(unittest.TestCase):
    def test_create_publication_five_real_fsync_windows(self) -> None:
        windows = ("C1-prepared-durable", "C2-record-durable", "C3-sidecar-durable",
                   "C4-committed-durable", "C5-clear-durable")
        for window in windows:
            for command in ("continue", "abort-io"):
                with self.subTest(window=window, command=command):
                    self._create_publication_window(window, command)

    def _create_publication_window(self, window: str, command: str) -> None:
        admission = current_admission()
        fixture = Fixture.create(admission, uuid.uuid4().hex)
        state = State(fixture.root.path / "state")
        parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        ticket = None
        child: subprocess.Popen[bytes] | None = None
        owner: Object | None = None
        guard: H1Child | None = None
        original: BaseException | None = None
        try:
            state.initialize(admission)
            state.close()
            parent.set_inheritable(False)
            peer.set_inheritable(False)
            ticket = admission.prepare_helper("tests/identity_actor.py")
            self.assertEqual(len(ticket.pass_fds), 1)
            self.assertEqual(len({*ticket.pass_fds, parent.fileno(), peer.fileno(), admission.lease}), 4)
            child = subprocess.Popen([*ticket.prefix, "create-stop", "anthropic-2", window, fixture.case,
                                      str(peer.fileno())], env=ticket.environment(dict(os.environ)),
                                     cwd=fixture.root.path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, close_fds=True,
                                     pass_fds=(*ticket.pass_fds, peer.fileno()), start_new_session=True)
            peer.close()
            observed = inspect_process(child.pid)
            require(observed is not None and observed.parent == os.getpid(), "Create child identity unavailable.")
            assert observed is not None
            owner = observed.owner
            guard = H1Child(fixture, child, ticket, parent, owner, time.monotonic() + 60,
                            selectors.DefaultSelector())
            assert child.stdout is not None and child.stderr is not None
            for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
                os.set_blocking(stream.fileno(), False)
                guard.selector.register(stream, selectors.EVENT_READ, name)
            parent.setblocking(False)
            guard.selector.register(parent, selectors.EVENT_READ, "control")
            ticket.acknowledge(child.pid)
            self.assertTrue(ticket.completed)
            guard._verify_helper("active")
            deadline = time.monotonic() + 10
            raw = bytearray()
            while b"\n" not in raw:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "Create checkpoint timed out.")
                for key, _ in guard.selector.select(remaining):
                    if key.data == "control":
                        part = parent.recv(4097 - len(raw))
                        require(bool(part), "Create checkpoint ended before readiness.")
                        raw.extend(part)
                        require(len(raw) <= 4096, "Create checkpoint exceeded its bound.")
                    else:
                        guard._drain(key)
            self.assertEqual(decode(bytes(raw)), {"version": 1, "kind": "actor-ready", "case": fixture.case,
                                                   "action": "create-stop", "window": window})
            guard._verify_helper("active")
            opened: list[Directory] = []
            try:
                directory = fixture.root
                for name in ("state", "subscriptions", "anthropic-2"):
                    directory = _existing(directory, name)
                    opened.append(directory)
                def read_optional(name: str) -> Object | None:
                    try:
                        return decode(directory.read(name), exact=False)
                    except FileNotFoundError:
                        return None
                journal = read_optional("identity-pending.json")
                record = read_optional("record.json")
                sidecar = read_optional("identity.json")
                index = int(window[1])
                self.assertEqual(record is not None, index >= 2)
                self.assertEqual(sidecar is not None, index >= 3)
                self.assertEqual(journal is not None, index <= 4)
                if record is not None:
                    self.assertEqual((record["id"], record["family"], record["state"]),
                                     ("anthropic-2", "anthropic", "signed_out"))
                if journal is not None:
                    self.assertEqual((journal["operation"], journal["phase"]),
                                     ("create", "committed" if index == 4 else "prepared"))
                witness = journal["witness"] if journal is not None else sidecar["witness"]  # type: ignore[index]
                self.assertTrue(valid_token(witness))
                if sidecar is not None:
                    self.assertEqual(sidecar, {"version": 1, "state": "active", "witness": witness})
                fixture.retain("create-checkpoint.json", canonical({"version": 1, "window": window,
                               "command": command, "journal": journal, "record": record, "sidecar": sidecar}))
            finally:
                for directory in reversed(opened):
                    directory.close()
            response = canonical({"version": 1, "kind": "actor-control", "case": fixture.case, "command": command})
            parent.setblocking(True)
            parent.settimeout(10)
            parent.sendall(response)
            guard.selector.unregister(parent)
            deadline = time.monotonic() + 30
            while guard.selector.get_map() or child.poll() is None:
                require(time.monotonic() < deadline, "Create actor settlement timed out.")
                for key, _ in guard.selector.select(.05):
                    guard._drain(key)
            child.wait(timeout=max(.001, deadline - time.monotonic()))
            guard.reaped = True
            result = decode(bytes(guard.output["stdout"]))
            outcome = "completed" if command == "continue" else "injected-failure"
            self.assertEqual(result, {"version": 1, "kind": "actor-result", "case": fixture.case,
                                      "action": "create-stop", "outcome": outcome})
            self.assertEqual(child.returncode, 0 if command == "continue" else 1)
            self.assertEqual(bytes(guard.output["stderr"]), b"")
            guard._verify_helper("terminal")
            after_dirs: list[Directory] = []
            try:
                after = fixture.root
                for name in ("state", "subscriptions", "anthropic-2"):
                    after = _existing(after, name)
                    after_dirs.append(after)
                def after_optional(name: str) -> Object | None:
                    try:
                        return decode(after.read(name), exact=False)
                    except FileNotFoundError:
                        return None
                final_journal = after_optional("identity-pending.json")
                final_record = after_optional("record.json")
                final_sidecar = after_optional("identity.json")
                if command == "abort-io":
                    self.assertEqual((final_journal, final_record, final_sidecar), (journal, record, sidecar))
                else:
                    self.assertIsNone(final_journal)
                    self.assertIsNotNone(final_record)
                    self.assertEqual(final_sidecar, {"version": 1, "state": "active", "witness": witness})
                fixture.retain("create-settlement.json", canonical({"version": 1, "window": window,
                               "command": command, "journal": final_journal, "record": final_record,
                               "sidecar": final_sidecar}))
            finally:
                for directory in reversed(after_dirs):
                    directory.close()
            fixture.retain("create-actor-result.json", canonical(result))
            guard._close_endpoints()
            guard.settled = True
        except BaseException as error:
            original = error
            raise
        finally:
            if guard is not None and not guard.settled:
                errors = guard.abort()
                if original is not None:
                    for note in errors:
                        original.add_note(note)
                elif errors:
                    raise AssertionError("Create actor cleanup unresolved: " + "; ".join(errors))
            elif guard is None:
                peer.close()
                parent.close()
                if child is not None and child.poll() is None:
                    actual = inspect_process(child.pid)
                    if owner is not None and actual is not None and actual.parent == os.getpid() and actual.owner == owner:
                        os.kill(child.pid, signal.SIGTERM)
                    try:
                        child.communicate(timeout=20)
                    except BaseException as cleanup:
                        if original is not None:
                            original.add_note("Create child cleanup unresolved: " + repr(cleanup))
                if ticket is not None:
                    ticket.child_socket.close()
                    ticket.frames.close()
            for close in (state.close, fixture.close):
                try:
                    close()
                except BaseException as cleanup:
                    if original is not None:
                        original.add_note("Create parent cleanup failed: " + repr(cleanup))
                    else:
                        raise

    def test_two_implicit_captures_contend_and_a_fresh_invocation_accepts_exact_proof(self) -> None:
        asyncio.run(self._concurrent_capture())

    async def _concurrent_capture(self) -> None:
        subject = ClaudeLocalSubject(guarded_settings=True)
        state = State(subject.state)
        calls: list[ClaudeInvocation] = []
        try:
            state.initialize(subject.fixture.admission)
            subscriptions = subject.state / "subscriptions"
            subscriptions.mkdir(mode=0o700)
            folder = subscriptions / "anthropic"
            root_before = subject.image(subject.state)
            try:
                async with state.lock("anthropic"):
                    self.assertFalse(os.path.lexists(folder / "identity.json"))
                    self.assertFalse(os.path.lexists(folder / "record.json"))
                    for _ in range(2):
                        calls.append(await subject.spawn("identity", "anthropic"))
                    witnesses = await self._waiting_at_short_lock(subject, calls)
                    self.assertFalse(os.path.lexists(folder / "identity.json"))
                    subject.fixture.retain("observed-contenders.json", canonical({"contenders": witnesses}))
                async with asyncio.TaskGroup() as group:
                    captures = [group.create_task(subject.collect(call, f"capture-{index}"))
                                for index, call in enumerate(calls, 1)]
            finally:
                async with asyncio.TaskGroup() as cleanup:
                    for call in calls:
                        cleanup.create_task(subject.stop(call))
            first, second = [self._proof(capture.result(), "anthropic") for capture in captures]
            self.assertEqual(first, second)
            saved = read_bytes(folder / "identity.json")
            self.assertEqual(saved, canonical({"version": 1, "state": "active", "witness": first["witness"]}))
            self.assertEqual(decode(read_bytes(subject.state / "identity.json")),
                             {"version": 1, "generation": first["generation"]})
            self.assertFalse(os.path.lexists(folder / "record.json"))
            image = subject.image(subject.state)
            for filename in ("format", "identity.guard", "identity.json"):
                self.assertEqual(image[filename], root_before[filename])
            expected = str(first["generation"]) + "." + str(first["witness"])
            restarted = await subject.request("restarted-exact-expect", "identity", "anthropic", "--expect", expected)
            self.assertNotIn(restarted.owner, [call.owner for call in calls])
            self.assertEqual(self._proof(restarted, "anthropic"), first)
            self.assertEqual(subject.image(subject.state), image)
        finally:
            try:
                state.close()
            finally:
                subject.close()

    async def _waiting_at_short_lock(
        self, subject: ClaudeLocalSubject, calls: list[ClaudeInvocation],
    ) -> list[Object]:
        folder = subject.state / "subscriptions/anthropic"
        lock = folder / "lock"
        original = read_bytes(lock)
        self.assertEqual(json.loads(original)["owner"]["pid"], os.getpid())
        async with asyncio.timeout(8):
            while True:
                seen: dict[int, Object] = {}
                for path in folder.iterdir():
                    if not re.fullmatch(r"\.write-[0-9a-f-]{36}", path.name):
                        continue
                    try:
                        row = json.loads(read_bytes(path))
                    except (FileNotFoundError, ValueError):
                        continue
                    pid = row.get("owner", {}).get("pid")
                    if type(pid) is not int or pid == os.getpid():
                        continue
                    contender = inspect_process(pid)
                    if contender is None:
                        continue
                    for call in calls:
                        provider = inspect_process(call.process.pid)
                        if (provider is not None and provider.owner == call.owner and provider.parent == os.getpid()
                                and contender.parent == call.process.pid):
                            seen[call.process.pid] = {"main": contender.owner, "provider": provider.owner}
                if len(seen) == len(calls):
                    self.assertEqual(read_bytes(lock), original)
                    return [seen[call.process.pid] for call in calls]
                self.assertTrue(all(call.process.returncode is None for call in calls))
                await asyncio.sleep(0.02)

    def test_legacy_list_keeps_supported_record_and_rejects_retired_aliases(self) -> None:
        asyncio.run(self._legacy_catalogue())

    @staticmethod
    def _written_state(subject: ClaudeLocalSubject, marker: bytes) -> Directory:
        state = Directory.root(subject.state)
        try:
            state.exclusive("format", marker)
            state.exclusive("identity.guard", b"")
            return state
        except BaseException:
            state.close()
            raise

    async def _legacy_catalogue(self) -> None:
        subject = ClaudeLocalSubject()
        try:
            state = self._written_state(subject, b"1\n")
            subject.fixture.directories.append(state)
            subscriptions = state.child("subscriptions", fresh=True)
            subject.fixture.directories.append(subscriptions)
            local = subscriptions.child("anthropic-2", fresh=True)
            subject.fixture.directories.append(local)
            retired = subscriptions.child("zai", fresh=True)
            subject.fixture.directories.append(retired)
            local.exclusive("record.json", canonical({
                "id": "anthropic-2", "family": "anthropic", "label": "Local record", "auth": "vendor_login",
                "state": "signed_out", "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
            }))
            retired.exclusive("record.json", canonical({"id": "zai", "family": "zai", "archived": subject.secret}))
            before = subject.image(subject.state)
            listed = await subject.request("legacy-list", "list")
            self.assertEqual((listed.exit, listed.stderr), (0, b""))
            rows = json.loads(listed.stdout)
            self.assertEqual([(row["id"], row["state"], row["auth"]) for row in rows],
                             [("anthropic", "signed_out", "vendor_login"),
                              ("anthropic-2", "signed_out", "vendor_login")])
            for selector in ("anthropic", "anthropic-2", "zai", "claude-zai"):
                refused = await subject.request("legacy-proof-" + selector, "identity", selector)
                error = self._failure(refused, "invalid_input")
                if selector in {"zai", "claude-zai"}:
                    self.assertIn("Pi Driver", error["remedy"])
                else:
                    self.assertEqual(error["remedy"],
                                     "Upgrade Driver state under exclusive maintenance before using Subscription identity.")
                self.assertEqual(subject.image(subject.state), before)
        finally:
            subject.close()

    def test_interrupted_conversion_refuses_list_and_proof_without_recovering(self) -> None:
        for phase in ("before-format-fence", "after-root-publication"):
            with self.subTest(phase=phase):
                asyncio.run(self._interrupted_conversion(phase))

    async def _interrupted_conversion(self, phase: str) -> None:
        subject = ClaudeLocalSubject()
        try:
            late = phase == "after-root-publication"
            state = self._written_state(subject, b"2\n" if late else b"1\n")
            subject.fixture.directories.append(state)
            before = subject.fixture.root.child("historical-before", fresh=True)
            subject.fixture.directories.append(before)
            before.exclusive("format", b"1\n")
            entries = [{"path": name, "present": False, "size": 0, "sha256": None}
                       for name in ("identity-maintenance.json", "identity-upgrade.json", "identity.json")]
            entries.insert(0, {"path": "format", "present": True, "size": 2, "sha256": sha256(b"1\n")})
            manifest = canonical({"version": 1, "kind": "driver-identity-backup", "format": 1, "files": entries})
            before.exclusive("manifest.json", manifest)
            generation = token()
            state.exclusive("identity-upgrade.json", canonical({
                "version": 1, "from": 1, "to": 2, "generation": generation,
                "backup": str(before.path), "backupSha256": sha256(manifest),
            }))
            if late:
                state.exclusive("identity.json", canonical({"version": 1, "generation": generation}))
            archive, images = subject.image(before.path), subject.image(subject.state)
            self._failure(await subject.request("interrupted-list-" + phase, "list"), "internal")
            self.assertEqual(subject.image(subject.state), images)
            self._failure(await subject.request("interrupted-identity-" + phase, "identity", "anthropic"), "internal")
            self.assertEqual(subject.image(subject.state), images)
            self.assertEqual(subject.image(before.path), archive)
        finally:
            subject.close()

    def test_lost_or_corrupt_proof_refuses_without_regeneration(self) -> None:
        for fault in ("missing-sidecar", "corrupt-sidecar", "missing-root", "corrupt-root"):
            with self.subTest(fault=fault):
                asyncio.run(self._lost_or_corrupt_proof(fault))

    def test_native_refusals_preserve_proof_and_native_state(self) -> None:
        asyncio.run(self._native_refusals_and_local_retirement())

    async def _native_refusals_and_local_retirement(self) -> None:
        subject = ClaudeLocalSubject()
        try:
            original = self._proof(await subject.request("native-proof", "identity", "anthropic"), "anthropic")
            baseline = subject.image(subject.state)
            for operation in ("sign-out", "remove"):
                for flags in ((), ("--force",)):
                    with self.subTest(operation=operation, flags=flags):
                        result = await subject.request(operation + ("-force" if flags else ""),
                                                       operation, "anthropic", *flags)
                        error = self._failure(result, "invalid_input")
                        self.assertIn("Claude Code", error["message"])
                        self.assertEqual(subject.image(subject.state), baseline)
                        subject.check()
            for alias in ("zai", "claude-zai"):
                error = self._failure(await subject.request("alias-" + alias, "identity", alias), "invalid_input")
                self.assertIn("Pi Driver", error["remedy"])
                self.assertEqual(subject.image(subject.state), baseline)
            expected = str(original["generation"]) + "." + str(original["witness"])
            self.assertEqual(self._proof(await subject.request("native-after-refusals", "identity", "anthropic",
                                                              "--expect", expected), "anthropic"), original)
            created = await subject.request("local-create", "create", "--family", "anthropic", "--id",
                                            "anthropic-2", "--label", "Local")
            self.assertEqual((created.exit, created.stderr), (0, b""))
            local = self._proof(await subject.request("local-proof", "identity", "anthropic-2"), "anthropic-2")
            removed = await subject.request("local-remove", "remove", "anthropic-2")
            self.assertEqual((removed.exit, removed.stderr), (0, b""))
            sidecar = decode(read_bytes(subject.state / "subscriptions/anthropic-2/identity.json"))
            self.assertEqual(sidecar, {"version": 1, "state": "retired", "witness": local["witness"]})
            self._failure(await subject.request("local-removed-proof", "identity", "anthropic-2"), "not_found")
            recreated = await subject.request("local-recreate", "create", "--family", "anthropic", "--id",
                                              "anthropic-2", "--label", "Recreated")
            self.assertEqual((recreated.exit, recreated.stderr), (0, b""))
            successor = self._proof(await subject.request("local-successor", "identity", "anthropic-2"),
                                    "anthropic-2")
            self.assertEqual(successor["generation"], local["generation"])
            self.assertNotEqual(successor["witness"], local["witness"])
            self.assertEqual(self._proof(await subject.request("native-after-local", "identity", "anthropic",
                                                              "--expect", expected), "anthropic"), original)
            subject.check()
        finally:
            subject.close()

    def test_duplicate_local_create_refuses_without_changing_proof(self) -> None:
        asyncio.run(self._duplicate_local_create())

    async def _duplicate_local_create(self) -> None:
        subject = ClaudeLocalSubject()
        try:
            arguments = ("--family", "anthropic", "--id", "anthropic-2", "--label", "Local")
            created = await subject.request("duplicate-before", "create", *arguments)
            self.assertEqual((created.exit, created.stderr), (0, b""))
            proof = self._proof(await subject.request("duplicate-proof", "identity", "anthropic-2"), "anthropic-2")
            before = subject.image(subject.state)
            self._failure(await subject.request("duplicate-refusal", "create", *arguments), "in_use")
            self.assertEqual(subject.image(subject.state), before)
            self.assertEqual(self._proof(await subject.request("duplicate-proof-after", "identity", "anthropic-2"),
                                         "anthropic-2"), proof)
            subject.check()
        finally:
            subject.close()

    def test_historical_prepared_create_settles_only_its_recorded_witness(self) -> None:
        for present in (False, True):
            with self.subTest(present=present):
                asyncio.run(self._historical_create_settlement(present))

    async def _historical_create_settlement(self, present: bool) -> None:
        subject = ClaudeLocalSubject()
        state = State(subject.state)
        try:
            state.initialize(subject.fixture.admission)
            subscriptions = Subscriptions(state, {}, [], subject.fixture.root.path / "vendor/settings.json")
            identifier = "anthropic-2"
            witness = token()
            async with state.lock(identifier):
                folder = state.subscription(identifier)
                if present:
                    directory = Directory.root(folder)
                    try:
                        directory.exclusive("record.json", canonical({
                            "id": identifier, "family": "anthropic", "label": "Historical local",
                            "auth": "vendor_login", "state": "signed_out", "createdAt": "2026-01-01T00:00:00Z",
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }))
                    finally:
                        directory.close()
                assert state.identity is not None
                state.identity.journal(identifier, "create", witness)
                await subscriptions.settle_local(identifier)
                self.assertIsNone(state.identity.read_pending(identifier))
                self.assertEqual(state.identity.read_sidecar(identifier), {
                    "version": 1, "witness": witness, "state": "active" if present else "retired",
                })
            if present:
                captured = await subscriptions.identity(identifier)
                self.assertEqual(captured["witness"], witness)
            else:
                self.assertFalse(os.path.lexists(folder / "record.json"))
            subject.check()
        finally:
            try:
                state.close()
            finally:
                subject.close()

    async def _lost_or_corrupt_proof(self, fault: str) -> None:
        subject = ClaudeLocalSubject()
        try:
            original = self._proof(await subject.request("proof-before-" + fault, "identity", "anthropic"),
                                   "anthropic")
            updated = await subject.request("record-before-" + fault, "update", "anthropic", "--label", "Preserved")
            self.assertEqual((updated.exit, updated.stderr), (0, b""))
            self.assertEqual(decode(updated.stdout, exact=False)["label"], "Preserved")
            folder = subject.state / "subscriptions/anthropic"
            root = Directory.root(subject.state)
            subscription = Directory.root(folder)
            try:
                record = subscription.read("record.json")
                guard = os.stat("identity.guard", dir_fd=root.descriptor, follow_symlinks=False)
                root_before = root.read("identity.json")
                sidecar_before = subscription.read("identity.json")
                self.assertEqual(decode(sidecar_before)["witness"], original["witness"])
                target = subscription if fault.endswith("sidecar") else root
                os.unlink("identity.json", dir_fd=target.descriptor)
                os.fsync(target.descriptor)
                if fault.startswith("corrupt"):
                    target.exclusive("identity.json", canonical({"version": 1, "invalid": True}))
                image = subject.image(subject.state)
                expected = str(original["generation"]) + "." + str(original["witness"])
                if fault == "missing-sidecar":
                    self._failure(await subject.request("old-expect-" + fault, "identity", "anthropic",
                                                        "--expect", expected), "in_use")
                    fresh = self._proof(await subject.request("new-proof-" + fault, "identity", "anthropic"),
                                        "anthropic")
                    self.assertEqual(fresh["generation"], original["generation"])
                    self.assertNotEqual(fresh["witness"], original["witness"])
                    self.assertEqual(root.read("identity.json"), root_before)
                else:
                    self._failure(await subject.request("identity-refusal-" + fault, "identity", "anthropic"),
                                  "internal")
                    self.assertEqual(subject.image(subject.state), image)
                    if fault.endswith("root"):
                        self._failure(await subject.request("list-refusal-" + fault, "list"), "internal")
                        self.assertEqual(subject.image(subject.state), image)
                    else:
                        self.assertEqual(root.read("identity.json"), root_before)
                self.assertEqual(subscription.read("record.json"), record)
                current_guard = os.stat("identity.guard", dir_fd=root.descriptor, follow_symlinks=False)
                self.assertEqual((current_guard.st_dev, current_guard.st_ino), (guard.st_dev, guard.st_ino))
                subject.check()
            finally:
                subscription.close()
                root.close()
        finally:
            subject.close()

    def _proof(self, capture: ClaudeCapture, selector: str) -> Object:
        self.assertEqual((capture.exit, capture.stderr), (0, b""))
        proof = decode(capture.stdout, exact=False)
        self.assertEqual(set(proof), {"version", "driver", "subscription", "generation", "witness"})
        self.assertIs(type(proof["version"]), int)
        self.assertEqual((proof["version"], proof["driver"], proof["subscription"]), (1, "claude", selector))
        for name in ("generation", "witness"):
            encoded = proof[name]
            self.assertRegex(encoded, r"\A[A-Za-z0-9_-]{43}\Z")
            raw = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
            self.assertEqual(len(raw), 32)
            self.assertEqual(base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"), encoded)
        return proof

    def _failure(self, capture: ClaudeCapture, code: str) -> Object:
        self.assertEqual((capture.exit, capture.stderr), (1, b""))
        result = decode(capture.stdout, exact=False)
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], code)
        return dict(result["error"])


class LifetimeRaceTests(unittest.TestCase):
    def test_matching_and_displaced_record_save(self) -> None:
        asyncio.run(self._conditional_record_save())

    async def _conditional_record_save(self) -> None:
        subject = ClaudeLocalSubject()
        state = State(subject.state)
        try:
            state.initialize(subject.fixture.admission)
            subscriptions = Subscriptions(state, {}, [], subject.fixture.root.path / "vendor/settings.json")
            proof = await subscriptions.identity("anthropic")
            lifetime = {key: proof[key] for key in ("generation", "witness")}
            auth = Authentication(subscriptions, {}, asyncio.Event())
            await auth.save("anthropic", lambda record: {**record, "label": "Matching lifetime"}, lifetime=lifetime)
            record_path = subject.state / "subscriptions/anthropic/record.json"
            self.assertEqual(decode(read_bytes(record_path))["label"], "Matching lifetime")
            assert state.identity is not None
            async with state.lock("anthropic"):
                successor_witness = token()
                self.assertNotEqual(successor_witness, lifetime["witness"])
                state.identity.publish_sidecar("anthropic", successor_witness, "active")
            successor = await subscriptions.identity("anthropic")
            self.assertEqual(successor["generation"], lifetime["generation"])
            self.assertEqual(successor["witness"], successor_witness)
            before = subject.image(subject.state)
            with self.assertRaises(Failure) as failed:
                await auth.save("anthropic", lambda record: {**record, "label": "Stale write"}, lifetime=lifetime)
            self.assertEqual(failed.exception.result["code"], "in_use")
            self.assertEqual(subject.image(subject.state), before)
            self.assertEqual(decode(read_bytes(record_path))["label"], "Matching lifetime")
            self.assertEqual(await subscriptions.identity("anthropic"), successor)
            subject.check()
        finally:
            try:
                state.close()
            finally:
                subject.close()


@dataclass
class H1CaseBinding:
    """The later source-admitted full runner owns this single fixed binding.

    It records the actual unittest outcome before consuming provisional_result,
    retains child across this case's return, then uses real support coordination
    and cleanup. Binding supplies no selector, path, operation or control frame.
    """

    fixture: Fixture
    results: LocalResults
    streams: SuiteStreams
    child: H1Child | None = None
    provisional_result: LocalResult | None = None


def _existing(parent: Directory, name: str) -> Directory:
    parent.verify()
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=parent.descriptor)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "Local H1 observation directory is unsafe.")
        result = Directory(parent.path / name, descriptor, (info.st_dev, info.st_ino))
        result.verify()
        parent.verify()
        return result
    except BaseException:
        os.close(descriptor)
        raise


def _images(fixture: Fixture) -> dict[str, bytes]:
    """Independent physical observations, without acquiring a short lock."""
    opened: list[Directory] = []
    original: BaseException | None = None
    try:
        state = _existing(fixture.root, "state")
        opened.append(state)
        subscriptions = _existing(state, "subscriptions")
        opened.append(subscriptions)
        subscription = _existing(subscriptions, "anthropic-2")
        opened.append(subscription)
        return {"root": state.read("identity.json"), "sidecar": subscription.read("identity.json"),
                "record": subscription.read("record.json"), "format": state.read("format")}
    except BaseException as error:
        original = error
        raise
    finally:
        errors: list[BaseException] = []
        for directory in reversed(opened):
            try:
                directory.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            if original is not None:
                for cleanup in errors:
                    original.add_note(f"H1 physical observation cleanup failed: {cleanup!r}.")
            else:
                raise BaseExceptionGroup("H1 physical observation cleanup failed.", errors)


async def _prepare_local_record(fixture: Fixture) -> None:
    """Only the real unsigned local metadata create path; no native/auth lookup."""
    fixture.verify()
    state = State(fixture.root.path / "state")
    original: BaseException | None = None
    try:
        state.initialize(fixture.admission)
        subscriptions = Subscriptions(state, {}, [], fixture.root.path / "vendor/settings.json")
        created = await subscriptions.create("anthropic", "H1 local metadata fixture", "anthropic-2")
        require(created["id"] == "anthropic-2" and created["state"] == "signed_out", "Local H1 create did not finish.")
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            state.close()
        except BaseException as cleanup:
            if original is None:
                raise
            original.add_note(f"Local H1 preparation state cleanup failed: {cleanup!r}.")


class HelperSpanTests(unittest.TestCase):
    _binding: ClassVar[H1CaseBinding | None] = None

    @classmethod
    def bind(cls, binding: H1CaseBinding) -> None:
        # This is a direct source-level adapter API, never an environment or
        # control-data selector. No adapter invokes it in this source step.
        require(cls._binding is None and binding.results.fixture is binding.fixture
                and binding.streams.fixture is binding.fixture and not binding.streams.closed
                and not binding.streams.exchanged, "H1 binding is missing, repeated or foreign.")
        binding.fixture.verify()
        cls._binding = binding

    def test_helper_independent_lease(self) -> None:
        binding = self._binding
        if binding is None:
            self.fail("H1 requires the admitted complete-runner binding; no filtered or stand-alone pass is available.")
        assert binding is not None
        fixture, results = binding.fixture, binding.results
        fixture.verify()
        self.assertEqual(results.order[-1], H1_ID)
        self.assertEqual(len(results.rows), len(results.order) - 1)
        self.assertIsNone(binding.child)
        self.assertIsNone(binding.provisional_result)
        # Every earlier result is the actual full ordered runner observation.
        # The independent final body/integration review verifies that adapter;
        # arbitrary status flags never establish a full gate.
        evidence_directory = fixture.directories[-1]
        self.assertEqual(evidence_directory.path, fixture.root.path / "local-evidence")
        for index, (case_id, row) in enumerate(zip(results.order[:-1], results.rows, strict=True)):
            self.assertEqual(row["id"], case_id)
            self.assertEqual(row["localStatus"], "passed")
            self.assertEqual(row["helperSpanStatus"], "not-applicable")
            self.assertTrue(valid_hash(row["resultSha256"]))
            self.assertEqual(sha256(evidence_directory.read(f"{index:04d}.result", limit=65536)), row["resultSha256"])
        state_directory = next(directory for directory in fixture.directories
                               if directory.path == fixture.root.path / "state")
        self.assertEqual(os.listdir(state_directory.descriptor), [], "H1 requires its own genuinely fresh local state.")
        asyncio.run(_prepare_local_record(fixture))
        before = _images(fixture)
        self.assertEqual(before["format"], b"2\n")
        root, sidecar = decode(before["root"]), decode(before["sidecar"])
        fields(root, "version generation")
        fields(sidecar, "version witness state")
        self.assertTrue(valid_token(root["generation"]))
        self.assertTrue(valid_token(sidecar["witness"]))
        self.assertEqual(sidecar["state"], "active")
        child: H1Child | None = None
        try:
            child = H1Child.start(fixture)
            # This is the real actor wrapper return, checked separately against
            # persisted before/after bytes. It is not a trusted gate result.
            capture_raw = fixture.root.read("h1-capture.json")
            capture = decode(capture_raw)
            fields(capture, "version driver subscription generation witness")
            self.assertEqual(capture, {"version": 1, "driver": "claude", "subscription": "anthropic-2",
                                       "generation": root["generation"], "witness": sidecar["witness"]})
            self.assertEqual(_images(fixture), before)
            self.assertIsNotNone(child.ready_raw)
            self.assertIsNotNone(child.ack_raw)
            self.assertIsNotNone(child.helper_context_raw)
            self.assertFalse(child.reaped)
            self.assertFalse(child.settled)
            self.assertFalse(fixture.admission.closed)
            proof: Object = {"version": 1, "kind": "abr38-h1-local-preconditions", "id": H1_ID,
                             "captureSha256": sha256(capture_raw),
                             "before": {name: sha256(raw) for name, raw in before.items()},
                             "after": {name: sha256(raw) for name, raw in _images(fixture).items()},
                             "actorReadySha256": sha256(child.ready_raw or b""),
                             "ackSha256": sha256(child.ack_raw or b""),
                             "contextSha256": sha256(child.helper_context_raw or b"")}
            # The actual unittest success callback in the future full adapter
            # consumes this sole provisional result. This case neither records
            # itself passed nor continues/finishes the helper or whole Suite.
            binding.provisional_result = LocalResult(H1_ID, "h1-preconditions-done", canonical(proof))
            binding.child = child
        except BaseException as error:
            if child is not None:
                for cleanup in child.abort():
                    error.add_note(cleanup)
            raise
