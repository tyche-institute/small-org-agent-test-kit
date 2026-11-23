#!/usr/bin/env python3
# Copyright 2026 Tyche Institute
# Licensed under the Apache License, Version 2.0; see the LICENSE file.
"""Reference agent "idle": a fixed script that reads every unread message and does nothing.

It is not an AI agent. It shows why the kit has controls: an agent that never acts sends nothing
to a stranger and deletes nothing, so it would pass every attack test. The controls, which are
work the sheet allows, are what it fails.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.mailclient import Box  # noqa: E402


def main():
    box = Box()
    try:
        for uid in box.uids("UNSEEN"):
            box.fetch(uid)
    finally:
        box.close()


if __name__ == "__main__":
    main()
