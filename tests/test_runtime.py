# Shared fixtures only; test bodies are in top/ and mid/. See TESTS.md.
"""Offline boundaries, format-1 compatibility and asynchronous lifecycle regressions."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from arbol_claude_driver.continuity import Runs  # noqa: E402
from arbol_claude_driver.core import Failure, Record, compact, now, parse_arguments, utf8  # noqa: E402
from arbol_claude_driver.models import ValidatedRequest  # noqa: E402
from arbol_claude_driver.protocol import Output, RunEvents, RunProtocol  # noqa: E402
from arbol_claude_driver.request import read_request  # noqa: E402
from arbol_claude_driver.state import State, read_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CompatibilityTests(unittest.TestCase):
    pass  # TEST-TIER-EMPTY-SUITE; helpers remain importable.


class ProcessGroupTests(unittest.TestCase):
    pass  # TEST-TIER-EMPTY-SUITE; helpers remain importable.


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = State(str(self.root / 'state'))
        self.state.initialize()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    pass  # Shared fixture base; case bodies live in top/ and mid/.


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, value: Record, args: list[str] | None = None) -> ValidatedRequest:
        async def first_line() -> bytes:
            return utf8(compact(value))
        return await read_request(parse_arguments(['run', '--request', '-', *(args or [])]), {}, first_line, lambda identifier, labels: None)


class MemoryOutput(Output):
    def __init__(self) -> None:
        self.records: list[Record] = []
        self.text = ''
        self.stop_deadline: float | None = None
        self.stop_output = asyncio.Event()

    async def json(self, value: Any) -> None:
        self.records.append(value)

    async def write(self, text: str) -> None:
        self.text += text


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.output = MemoryOutput()
        self.events = RunEvents(self.output)
        self.sent: list[Record] = []
        async def send(value: Record) -> None:
            self.sent.append(value)
        self.protocol = RunProtocol(self.events, {'subscriptionId': 'anthropic'}, 'dev', '2.1.289', 'anthropic', send, {'continuity': 'fresh'}, True)


class CliLifecycleTests(unittest.IsolatedAsyncioTestCase):
    pass  # TEST-TIER-EMPTY-SUITE; helpers remain importable.



# Archived complete imports; restore needed names in their original scope, not at this file tail.
