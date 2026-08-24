#!/usr/bin/env python3
"""Thin CLI entrypoint for attachment downloads."""
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.attachments import *  # re-export compatibility API


if __name__ == "__main__":
    raise SystemExit(main())
