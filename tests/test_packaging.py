# Shared fixtures; case bodies live in top/ and mid/. See TESTS.md.
"""Delivery boundaries: pinned archives, numbered records and nonempty checks."""

from __future__ import annotations
import hashlib
import json
import os
import pwd
import tempfile
from unittest.mock import patch


import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_support  # noqa: E402
import check_build  # noqa: E402
import package_build  # noqa: E402


class PackagingTests(unittest.TestCase):
    pass  # TEST-TIER-EMPTY-SUITE; helpers remain importable.


# Archived complete imports; restore needed names in their original scope, not at this file tail.
