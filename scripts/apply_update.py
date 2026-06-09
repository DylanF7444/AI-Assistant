from __future__ import annotations

import argparse
import base64
from pathlib import Path

from assistant.updates import UpdateManager


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a previously verified Local Assistant update"
    )
    parser.add_argument("staged", type=Path)
    parser.add_argument("install_dir", type=Path)
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--public-key-base64", required=True)
    args = parser.parse_args()
    manager = UpdateManager(
        args.staged.parent,
        {args.key_id: base64.b64decode(args.public_key_base64)},
        current_version=args.current_version,
    )
    backup = manager.apply(args.staged, args.install_dir)
    print(f"Update applied. Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
