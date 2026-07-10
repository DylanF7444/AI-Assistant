from __future__ import annotations

import csv
import subprocess

from assistant.windows.interfaces import ProcessInfo


class WindowsProcessAdapter:
    async def list(self, name_filter: str | None = None) -> list[ProcessInfo]:
        completed = await __import__("asyncio").create_subprocess_exec(
            "tasklist",
            "/FO",
            "CSV",
            "/NH",
            stdout=__import__("asyncio").subprocess.PIPE,
            stderr=__import__("asyncio").subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, _ = await completed.communicate()
        rows = []
        for row in csv.reader(stdout.decode(errors="replace").splitlines()):
            if len(row) < 5 or not row[1].isdigit():
                continue
            info = ProcessInfo(row[0], int(row[1]), row[2], row[4])
            if not name_filter or name_filter.casefold() in info.image_name.casefold():
                rows.append(info)
        return rows

    async def terminate(self, pid: int) -> bool:
        completed = await __import__("asyncio").create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            stdout=__import__("asyncio").subprocess.DEVNULL,
            stderr=__import__("asyncio").subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return await completed.wait() == 0
