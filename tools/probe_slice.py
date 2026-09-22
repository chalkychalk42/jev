#!/usr/bin/env python3
"""Compatibility entrypoint for the shared runtime. Use --check without a game client."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jev.run.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
