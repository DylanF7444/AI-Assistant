from __future__ import annotations

import argparse
import asyncio

from assistant.bootstrap import build_container
from assistant.policy.network import offline_lock
from assistant.ui.main import run_desktop


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Local Assistant")
    parser.add_argument("--text", help="Run one text command without opening the UI")
    args = parser.parse_args()
    container = build_container()
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
