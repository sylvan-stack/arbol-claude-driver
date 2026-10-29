"""Commented mid-tier selections; see MID_TIER.md before restoring a case."""

# Restore the needed imports and class header only for an explicit testing task.
# import test_packaging as _support
# from test_packaging import (
#     PackagingTests,
#     Path,
#     sys,
#     unittest,
# )


# class PackagingTests(_support.PackagingTests):
# TEST-TIER-DISABLED-BEGIN tests/test_packaging.py::PackagingTests.test_corrupted_cached_distribution_is_refused_without_a_download
#     def test_corrupted_cached_distribution_is_refused_without_a_download(self) -> None:
#         with tempfile.TemporaryDirectory() as name:
#             cache = Path(name)
#             expected = hashlib.sha256(b"expected archive").hexdigest()
#             (cache / f"{expected}.tar.gz").write_bytes(b"corrupt archive")
#             with patch.dict(os.environ, {"ARBOL_PYTHON_ARCHIVE_CACHE": name}), patch("urllib.request.urlopen") as download:
#                 with self.assertRaisesRegex(ValueError, "wrong SHA-256"):
#                     package_build.archive({"url": "https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz", "sha256": expected})
#                 download.assert_not_called()
# TEST-TIER-DISABLED-END tests/test_packaging.py::PackagingTests.test_corrupted_cached_distribution_is_refused_without_a_download

# TEST-TIER-DISABLED-BEGIN tests/test_packaging.py::PackagingTests.test_archive_pin_rejects_nonhex_digests_and_untrusted_sources
#     def test_archive_pin_rejects_nonhex_digests_and_untrusted_sources(self) -> None:
#         for distribution in ({"url": "https://example.invalid/python.tar.gz", "sha256": "f" * 64},
#                              {"url": "https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz", "sha256": "/" * 64}):
#             with self.subTest(distribution=distribution), patch("urllib.request.urlopen") as download:
#                 with self.assertRaisesRegex(ValueError, "pinned URL and SHA-256"):
#                     package_build.archive(distribution)
#                 download.assert_not_called()
# TEST-TIER-DISABLED-END tests/test_packaging.py::PackagingTests.test_archive_pin_rejects_nonhex_digests_and_untrusted_sources

# TEST-TIER-DISABLED-BEGIN tests/test_packaging.py::PackagingTests.test_candidate_cannot_overwrite_a_numbered_build
#     def test_candidate_cannot_overwrite_a_numbered_build(self) -> None:
#         with tempfile.TemporaryDirectory() as name:
#             (Path(name) / "build.json").write_text("{}\n")
#             with self.assertRaisesRegex(ValueError, "cannot replace a numbered Build"):
#                 build_support.build_folder(name)
# TEST-TIER-DISABLED-END tests/test_packaging.py::PackagingTests.test_candidate_cannot_overwrite_a_numbered_build

# TEST-TIER-DISABLED-BEGIN tests/test_packaging.py::PackagingTests.test_build_record_requires_matching_number_entry_and_version
#     def test_build_record_requires_matching_number_entry_and_version(self) -> None:
#         with tempfile.TemporaryDirectory() as name:
#             folder = Path(name)
#             record = {"component": build_support.COMPONENT, "build": "a", "packageVersion": "1.0.0", "entry": build_support.ENTRY}
#             (folder / "build.json").write_text(json.dumps(record))
#             with patch.dict(os.environ, {"ARBOL_BUILD_DIR": name, "ARBOL_BUILD_NUMBER": "b"}), patch.object(build_support, "metadata", return_value=("1.0.0", {})):
#                 with self.assertRaisesRegex(ValueError, "match this component, number and package version"):
#                     build_support.build_folder()
#             record["build"] = "b"
#             record["entry"] = "bin/another-program"
#             (folder / "build.json").write_text(json.dumps(record))
#             with patch.dict(os.environ, {"ARBOL_BUILD_DIR": name, "ARBOL_BUILD_NUMBER": "b"}), patch.object(build_support, "metadata", return_value=("1.0.0", {})):
#                 with self.assertRaisesRegex(ValueError, "executable entry"):
#                     build_support.build_folder()
# TEST-TIER-DISABLED-END tests/test_packaging.py::PackagingTests.test_build_record_requires_matching_number_entry_and_version

# TEST-TIER-DISABLED-BEGIN tests/test_packaging.py::PackagingTests.test_live_gate_uses_the_test_state_directory
#     def test_live_gate_uses_the_test_state_directory(self) -> None:
#         home = Path(pwd.getpwuid(os.getuid()).pw_dir)
#         environment = {key: value for key, value in os.environ.items() if key != "ARBOL_BUILD_TEST_STATE_DIR"}
#         with patch.dict(os.environ, environment, clear=True), patch.object(check_build, "run", return_value=0) as execute:
#             self.assertEqual(check_build.live(Path("/tmp/candidate")), 0)
#         self.assertEqual(execute.call_args.args[0][1:], ["dev", "build-check", "--state-dir", str(home / "Library/Application Support/Arbol/drivers/arbol-claude-driver")])
#         with patch.dict(os.environ, {"ARBOL_BUILD_TEST_STATE_DIR": "/tmp/other state"}), patch.object(check_build, "run", return_value=0) as execute:
#             check_build.live(Path("/tmp/candidate"))
#         self.assertEqual(execute.call_args.args[0][-1], "/tmp/other state")
#         for directory in ("", "relative/state"):
#             with self.subTest(directory=directory), patch.dict(os.environ, {"ARBOL_BUILD_TEST_STATE_DIR": directory}), patch.object(check_build, "run") as execute:
#                 with self.assertRaisesRegex(ValueError, "absolute test state"):
#                     check_build.live(Path("/tmp/candidate"))
#                 execute.assert_not_called()
# TEST-TIER-DISABLED-END tests/test_packaging.py::PackagingTests.test_live_gate_uses_the_test_state_directory

# TEST-TIER-DISABLED-BEGIN tests/test_packaging.py::PackagingTests.test_unit_gate_refuses_an_empty_suite
#     def test_unit_gate_refuses_an_empty_suite(self) -> None:
#         with tempfile.TemporaryDirectory() as name:
#             folder = Path(name)
#             (folder / "tests").mkdir()
#             previous = Path.cwd()
#             try:
#                 with self.assertRaisesRegex(ValueError, "no tests"):
#                     check_build.unit(folder)
#             finally:
#                 os.chdir(previous)
# TEST-TIER-DISABLED-END tests/test_packaging.py::PackagingTests.test_unit_gate_refuses_an_empty_suite


# Original deferred imports (restore at the required scope, never blindly at the tail).
# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:5
# import hashlib
# TEST-TIER-IMPORTS-END tests/test_packaging.py:5

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:6
# import json
# TEST-TIER-IMPORTS-END tests/test_packaging.py:6

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:7
# import os
# TEST-TIER-IMPORTS-END tests/test_packaging.py:7

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:8
# import pwd
# TEST-TIER-IMPORTS-END tests/test_packaging.py:8

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:10
# import tempfile
# TEST-TIER-IMPORTS-END tests/test_packaging.py:10

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:13
# from unittest.mock import patch
# TEST-TIER-IMPORTS-END tests/test_packaging.py:13

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:16
# import build_support  # noqa: E402
# TEST-TIER-IMPORTS-END tests/test_packaging.py:16

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:17
# import check_build  # noqa: E402
# TEST-TIER-IMPORTS-END tests/test_packaging.py:17

# TEST-TIER-IMPORTS-BEGIN tests/test_packaging.py:18
# import package_build  # noqa: E402
# TEST-TIER-IMPORTS-END tests/test_packaging.py:18

raise RuntimeError("Mid-tier tests require an explicitly requested testing task; see MID_TIER.md.")
