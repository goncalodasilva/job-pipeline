#!/usr/bin/env python3
"""Claude Code compatibility entry point; shared implementation is in scripts/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agent_hook import main

if __name__ == "__main__":
    sys.exit(main("claude", "session"))
