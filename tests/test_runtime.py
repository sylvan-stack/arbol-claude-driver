# Shared fixtures; case bodies live in top/ and mid/. See TESTS.md.
"""Offline boundaries, format-1 compatibility and asynchronous lifecycle regressions."""
from __future__ import annotations
# Re-exported: tests/top and tests/mid import these names from this module (TESTS.md).
import signal  # noqa: F401
import shlex  # noqa: F401
import stat  # noqa: F401
import subprocess  # noqa: F401
from unittest.mock import AsyncMock, patch  # noqa: F401


import asyncio
import os  # noqa: F401
import sys
import tempfile
import time  # noqa: F401
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from arbol_claude_driver.continuity import Chats, Runs  # noqa: E402, F401
from arbol_claude_driver.core import Failure, Record, clean_environment, compact, loads, now, number, parse_arguments, read_settings, safe_failure, utf8  # noqa: E402, F401
from arbol_claude_driver.history import MessageDigest, digest, encode_folder, result_text, session_lines  # noqa: E402, F401
from arbol_claude_driver.dev import live_check  # noqa: E402, F401
from arbol_claude_driver.processes import child_command, kill_group, python_launcher, remaining_groups, supported_version  # noqa: E402, F401
from arbol_claude_driver.protocol import Output, RunEvents, RunProtocol, limit_report, run_failure, truncate_result  # noqa: E402, F401
from arbol_claude_driver.request import read_request, run_arguments  # noqa: E402, F401
from arbol_claude_driver.state import State, owner_alive, parse_owner, process_owner, read_json, write_json  # noqa: E402, F401
from arbol_claude_driver.subscriptions import Authentication, Secrets, Subscriptions, family_settings, keychain_service, normalize_quota, validate_key  # noqa: E402, F401

from arbol_claude_driver.models import ValidatedRequest  # noqa: E402

# The names tests/top and tests/mid import from this module, exported explicitly (TESTS.md).
__all__ = [
    'Any',
    'Authentication',
    'Chats',
    'Failure',
    'MessageDigest',
    'Output',
    'Path',
    'ROOT',
    'Record',
    'Runs',
    'Secrets',
    'Subscriptions',
    'asyncio',
    'child_command',
    'compact',
    'digest',
    'kill_group',
    'loads',
    'now',
    'os',
    'patch',
    'python_launcher',
    'read_json',
    'run_failure',
    'shlex',
    'signal',
    'stat',
    'subprocess',
    'sys',
    'tempfile',
    'time',
    'write_json',
]

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
    pass  # Shared fixtures; case bodies live in top/ and mid/.


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
