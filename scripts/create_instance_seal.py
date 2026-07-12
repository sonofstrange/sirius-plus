#!/usr/bin/env python3
"""Activate the official Sirius Plus instance using its private data volume."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


def main() -> int:
    config.create_instance_seal()
    print(f"Instance activation file created: {config.INSTANCE_SEAL_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
