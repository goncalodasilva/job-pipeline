#!/usr/bin/env python3
"""Compatibility entry point for existing Claude skills and commands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from scripts.next_task import main

if __name__ == "__main__":
    sys.exit(main())
