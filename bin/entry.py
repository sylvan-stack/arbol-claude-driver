"""Resolve the local runtime package without trusting the caller's Python path."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("arbol_claude_driver", run_name="__main__")
