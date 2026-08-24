#!/usr/bin/env python3
"""Backwards-compatible entry point: ``python main.py --url ... --email ... --password ...``

The implementation now lives in the ``teachable_dl`` package.
"""

import sys

from teachable_dl.cli import main

if __name__ == "__main__":
    sys.exit(main())
