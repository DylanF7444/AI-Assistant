from __future__ import annotations

import argparse
import asyncio

from assistant.config import Settings
from assistant.integrations.google_calendar import GoogleCalendarConnectionService
from assistant.storage.secrets import SecretVault


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compatibility helper for Google Calendar setup. The same flow is available "
            "inside the Extension Hub."
        )
    )
    parser.add_argument(
        "--client-id",
        help="Optional override for the bundled Google Desktop OAuth client ID",
    )
    parser.add_argument("--client-secret", help="Optional Google Desktop OAuth client secret")
    parser.add_argument("--disconnect", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


async def _run() -> int:
    args = _arguments()
    service = GoogleCalendarConnectionService(SecretVault(Settings().data_dir / "secrets"))
    if args.disconnect:
        await service.disconnect()
        print("Google Calendar connection removed.")
        return 0
    status = await service.connect(
        args.client_id,
        client_secret=args.client_secret,
        timeout_seconds=args.timeout,
    )
    label = f" ({status.account})" if status.account else ""
    print(f"Google Calendar is connected{label}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
