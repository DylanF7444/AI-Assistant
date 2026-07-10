from __future__ import annotations

import argparse
from pathlib import Path

from assistant.skills.registry import SkillRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Local Assistant skill")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    manifest = SkillRegistry().validate_package(args.path)
    print(f"Valid skill: {manifest.skill_id} {manifest.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
