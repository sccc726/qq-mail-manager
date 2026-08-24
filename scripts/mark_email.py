#!/usr/bin/env python3
"""Thin CLI entrypoint for UID message flags."""
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.marking import *  # re-export compatibility API
from qqmail_core.results import configure_cli_stdio


if __name__ == "__main__":
    configure_cli_stdio()
    raise SystemExit(main())
