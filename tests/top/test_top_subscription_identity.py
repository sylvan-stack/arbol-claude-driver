"""Prospective final H1 local precondition case; no runner or inventory wiring."""
from __future__ import annotations

import asyncio
import os
import stat
import unittest
from dataclasses import dataclass
from typing import ClassVar

from arbol_claude_driver.identity_protocol import Object, canonical, decode, fields, sha256, valid_hash, valid_token
from arbol_claude_driver.state import State
from arbol_claude_driver.subscriptions import Subscriptions
from identity_support import Directory, Fixture, H1Child, H1_ID, LocalResult, LocalResults, SuiteStreams, require


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
