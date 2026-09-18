#!/usr/bin/env python3
"""Compatibility shim. Prefer: `jiffy setup` or `python -m jiffy setup`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jiffy.setup import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
