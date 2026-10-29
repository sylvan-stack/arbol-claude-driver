# Shared fixtures only; test bodies are in top/ and mid/. See TESTS.md.
"""Contract-2 retirement refuses before credentials, native startup or record mutation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RetiredRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / 'state'
        self.settings = self.home / '.arbol/arbol-claude-driver.json'
        self.settings.parent.mkdir()
        self.environment = {**os.environ, 'HOME': str(self.home)}
        self.call('info')
        self.retired = self.state / 'subscriptions/old-gateway'
        self.retired.mkdir(parents=True)
        self.record = {'id': 'old-gateway', 'family': 'zai', 'label': 'Retained', 'auth': 'key',
                       'state': 'signed_in', 'createdAt': '2026-10-20T00:00:00Z', 'updatedAt': '2026-10-20T00:00:00Z'}
        (self.retired / 'record.json').write_text(json.dumps(self.record))
        (self.retired / 'history.json').write_text('historical conversation')
        self.sentinel = self.home / 'native-started'
        tool = self.home / 'claude'
        tool.write_text('#!/bin/sh\ntouch "' + str(self.sentinel) + '"\nexit 99\n')
        tool.chmod(0o700)
        self.configuration = {'toolPath': str(tool)}
        self.settings.write_text(json.dumps(self.configuration))

    def call(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, '-I', str(ROOT / 'bin/entry.py'), '--state-dir', str(self.state),
                               *arguments], env=self.environment, capture_output=True, text=True, timeout=10)

    def refuses(self, *arguments: str) -> None:
        result = self.call(*arguments)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(records), 1, records)
        error = records[0]['error']
        self.assertEqual(error['code'], 'invalid_input', records)
        self.assertIn('Pi', error['remedy'])
        self.assertFalse(self.sentinel.exists())
        self.assertNotIn('SECRET-SENTINEL', result.stdout + result.stderr)


# Archived complete imports; restore needed names in their original scope, not at this file tail.
