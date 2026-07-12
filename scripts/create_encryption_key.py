#!/usr/bin/env python3
"""Create the Fernet key used to encrypt Sirius tokens at rest."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet


DEFAULT_PATH = Path("/app/data/encryption_key.txt")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Fernet encryption key without overwriting an existing one."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_PATH,
        help=f"key file path (default: {DEFAULT_PATH})",
    )
    args = parser.parse_args()
    path: Path = args.path

    if path.exists():
        print(f"Key already exists: {path}. It was not changed.", file=sys.stderr)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        print(f"Key already exists: {path}. It was not changed.", file=sys.stderr)
        return 0

    with os.fdopen(fd, "wb") as key_file:
        key_file.write(Fernet.generate_key() + b"\n")

    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"Encryption key created: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
