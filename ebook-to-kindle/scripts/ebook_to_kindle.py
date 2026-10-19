#!/usr/bin/env python3
"""Stable user-facing entrypoint; argparse help is implemented in ebook_kindle.cli."""

SCRIPT_INTERFACE = "cli"
SCRIPT_INTERFACE_REASON = "Stable public conversion and doctor command."

from ebook_kindle.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
