#!/usr/bin/env python3
"""Run every test module in this directory with the stdlib unittest runner.

Usage::

    ~/.hermes/hermes-agent/.venv/bin/python tests/run_tests.py -v

Puts the plugin directory (for ``progress_block``) and this tests directory on
``sys.path``, then loads ``test_*.py`` from here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

for path in (str(PLUGIN_DIR), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)
    print(f"\n{result.testsRun} tests, {len(result.failures)} failures, "
          f"{len(result.errors)} errors, {len(result.skipped)} skipped")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
