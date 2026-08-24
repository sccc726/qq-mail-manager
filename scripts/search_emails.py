#!/usr/bin/env python3
"""Thin CLI entrypoint for searching mail."""
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.readers import *  # re-export compatibility API
from qqmail_core.readers import _internaldate  # legacy test/import compatibility


if __name__ == "__main__":
    raise SystemExit(main())
