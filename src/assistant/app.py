from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.policy.network import offline_lock
from assistant.ui.main import run_desktop


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline Local Assistant")
    parser.add_argument("--text", help="Run one text command without opening the UI")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Use an isolated assistant data directory for this process.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--isolated-skill-worker"]:
        from assistant.skills.worker import main as run_skill_worker

        return run_skill_worker(arguments[1:])
    args = _parse_args(arguments)
    settings = Settings(data_dir=args.data_dir) if args.data_dir is not None else None
    container = build_container(settings)
    with offline_lock(container.settings.offline_lock):
        if args.text:
            response, _ = asyncio.run(
                container.orchestrator.handle_text(args.text, session_id="cli")
            )
            print(response.display_text)
            return 0
        return run_desktop(container)


if __name__ == "__main__":
    raise SystemExit(main())
