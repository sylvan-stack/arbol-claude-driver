"""Delivery boundaries: pinned archives, numbered records and nonempty checks."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_support  # noqa: E402
import check_build  # noqa: E402
import package_build  # noqa: E402


class PackagingTests(unittest.TestCase):
    def test_corrupted_cached_distribution_is_refused_without_a_download(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            cache = Path(name)
            expected = hashlib.sha256(b"expected archive").hexdigest()
            (cache / f"{expected}.tar.gz").write_bytes(b"corrupt archive")
            with patch.dict(os.environ, {"ARBOL_PYTHON_ARCHIVE_CACHE": name}), patch("urllib.request.urlopen") as download:
                with self.assertRaisesRegex(ValueError, "wrong SHA-256"):
                    package_build.archive({"url": "https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz", "sha256": expected})
                download.assert_not_called()

    def test_archive_pin_rejects_nonhex_digests_and_untrusted_sources(self) -> None:
        for distribution in ({"url": "https://example.invalid/python.tar.gz", "sha256": "f" * 64},
                             {"url": "https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz", "sha256": "/" * 64}):
            with self.subTest(distribution=distribution), patch("urllib.request.urlopen") as download:
                with self.assertRaisesRegex(ValueError, "pinned URL and SHA-256"):
                    package_build.archive(distribution)
                download.assert_not_called()

    def test_package_metadata_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text(f'[project]\nname = "{build_support.COMPONENT}"\nversion = "1.0.0"\ndependencies = []\n')
            (root / "package.json").write_text('{"version":"9.0.0"}\n')
            with self.assertRaisesRegex(ValueError, "agree on the package version"):
                build_support.metadata(root)

    def test_candidate_cannot_overwrite_a_numbered_build(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / "build.json").write_text("{}\n")
            with self.assertRaisesRegex(ValueError, "cannot replace a numbered Build"):
                build_support.build_folder(name)

    def test_build_record_requires_matching_number_entry_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            record = {"component": build_support.COMPONENT, "build": "a", "packageVersion": "1.0.0", "entry": build_support.ENTRY}
            (folder / "build.json").write_text(json.dumps(record))
            with patch.dict(os.environ, {"ARBOL_BUILD_DIR": name, "ARBOL_BUILD_NUMBER": "b"}), patch.object(build_support, "metadata", return_value=("1.0.0", {})):
                with self.assertRaisesRegex(ValueError, "match this component, number and package version"):
                    build_support.build_folder()
            record["build"] = "b"
            record["entry"] = "bin/another-program"
            (folder / "build.json").write_text(json.dumps(record))
            with patch.dict(os.environ, {"ARBOL_BUILD_DIR": name, "ARBOL_BUILD_NUMBER": "b"}), patch.object(build_support, "metadata", return_value=("1.0.0", {})):
                with self.assertRaisesRegex(ValueError, "executable entry"):
                    build_support.build_folder()

    def test_unit_gate_refuses_an_empty_suite(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "tests").mkdir()
            previous = Path.cwd()
            try:
                with self.assertRaisesRegex(ValueError, "no tests"):
                    check_build.unit(folder)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
