# Mid-tier source from scripts/check_build.py; see MID_TIER.md.
# Shared helpers and refusal entry points remain at the original path.

# TEST-TIER-DISABLED-BEGIN scripts/check_build.py::unit
# def unit(folder: Path) -> int:
#     sys.path.insert(0, str(folder))
#     sys.path.insert(0, str(folder / "src"))
#     os.chdir(folder)
#     suite = unittest.TestLoader().discover(str(folder / "tests"))
#     if suite.countTestCases() == 0:
#         raise ValueError("The packaged unit check discovered no tests.")
#     return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
# TEST-TIER-DISABLED-END scripts/check_build.py::unit

# TEST-TIER-DISABLED-BEGIN scripts/check_build.py::contract
# def contract(folder: Path) -> int:
#     # By default the clone of the last merged main, never the repository's own folder, which can hold unmerged work (BLD-D18).
#     repo = Path(os.environ.get("ARBOL_CONFORMANCE_REPO", str(Path.home() / "repos/sylvan-stack/readonly/arbol"))).resolve()
#     if read_object(repo / "package.json").get("name") != "@sylvan-stack/arbol":
#         raise ValueError("ARBOL_CONFORMANCE_REPO must name the Arbol repository.")
#     # The shared suite is standard-library Python; the Build's own interpreter runs it, with nothing to install.
#     return run([str(folder / "runtime/python/bin/python3.14"), "-I", "-B", str(repo / "conformance/bin/arbol-conformance.py"),
#                 "--driver", shlex.quote(str(folder / ENTRY))], cwd=folder)
# TEST-TIER-DISABLED-END scripts/check_build.py::contract

# TEST-TIER-DISABLED-BEGIN scripts/check_build.py::live
# def live(folder: Path) -> int:
#     home = Path(pwd.getpwuid(os.getuid()).pw_dir)
#     # The test Subscriptions live in a state directory of their own, not in the default one (DRV-D41).
#     state = os.environ.get("ARBOL_BUILD_TEST_STATE_DIR", str(home / "Library/Application Support/Arbol/drivers/arbol-claude-driver"))
#     if not state or not Path(state).is_absolute():
#         raise ValueError("ARBOL_BUILD_TEST_STATE_DIR must be an absolute test state directory.")
#     env = {"HOME": str(home), "PATH": f"{home / '.local/bin'}:/usr/bin:/bin", "LANG": os.environ.get("LANG", "C")}
#     return run([str(folder / ENTRY), "dev", "build-check", "--state-dir", state], cwd=folder, env=env)
# TEST-TIER-DISABLED-END scripts/check_build.py::live

# TEST-TIER-IMPORTS-BEGIN scripts/check_build.py:8
# import pwd
# TEST-TIER-IMPORTS-END scripts/check_build.py:8

# TEST-TIER-IMPORTS-BEGIN scripts/check_build.py:9
# import shlex
# TEST-TIER-IMPORTS-END scripts/check_build.py:9

# TEST-TIER-IMPORTS-BEGIN scripts/check_build.py:12
# import unittest
# TEST-TIER-IMPORTS-END scripts/check_build.py:12

raise RuntimeError("Mid-tier checks require an explicitly requested testing task; see MID_TIER.md.")
