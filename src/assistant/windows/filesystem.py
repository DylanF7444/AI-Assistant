from __future__ import annotations

import os
import shutil
from pathlib import Path


class LocalFileSystemAdapter:
    async def search(self, root: Path, query: str, limit: int) -> list[Path]:
        if not root.is_dir():
            raise NotADirectoryError(root)
        needle = query.casefold()
        matches: list[Path] = []
        for path in root.rglob("*"):
            if needle in path.name.casefold():
                matches.append(path)
                if len(matches) >= limit:
                    break
        return matches

    async def copy(self, source: Path, destination: Path) -> Path:
        return Path(shutil.copy2(source, destination))

    async def create_text_file(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    async def move(self, source: Path, destination: Path) -> Path:
        return Path(shutil.move(source, destination))

    async def delete_to_recycle_bin(self, path: Path) -> None:
        try:
            from send2trash import send2trash
        except ImportError as exc:
            raise RuntimeError("Safe deletion requires the optional send2trash package") from exc
        send2trash(str(path))

    async def open(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        os.startfile(path)  # type: ignore[attr-defined]
