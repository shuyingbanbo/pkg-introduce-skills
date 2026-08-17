#!/usr/bin/env python3
"""Compatibility wrapper forwarding to build-rpm/scripts/cascade_package_check.py."""

from __future__ import annotations

import runpy
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "../../build-rpm/scripts" / "cascade_package_check.py"
_globals = runpy.run_path(str(TARGET), run_name=__name__)
globals().update(_globals)
