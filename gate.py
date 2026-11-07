#!/usr/bin/env python3
"""Approval gate entry point: `python gate.py` exits 0 when the plan may be generated, 1 when blocked."""
import sys

from regdelta.cli import main

if __name__ == "__main__":
    sys.exit(main(["gate", *sys.argv[1:]]))
