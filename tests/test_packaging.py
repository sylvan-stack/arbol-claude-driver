# Shared fixtures only; test bodies are in top/ and mid/. See TESTS.md.
"""Delivery boundaries: pinned archives, numbered records and nonempty checks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class PackagingTests(unittest.TestCase):
    pass  # TEST-TIER-EMPTY-SUITE; helpers remain importable.



# Archived complete imports; restore needed names in their original scope, not at this file tail.
