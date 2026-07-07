from __future__ import annotations

from pathlib import Path


ASSISTANT_FOLDER_TOKEN = "${assistant_folder}"

KNOWN_USER_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "document": "Documents",
    "downloads": "Downloads",
    "download": "Downloads",
    "pictures": "Pictures",
    "picture": "Pictures",
    "music": "Music",
    "videos": "Videos",
    "video": "Videos",
}

USER_FOLDER_TOKENS = {
    "${user_desktop}": "Desktop",
    "${user_documents}": "Documents",
    "${user_downloads}": "Downloads",
    "${user_pictures}": "Pictures",
    "${user_music}": "Music",
    "${user_videos}": "Videos",
}


def resolve_user_path(value: str | Path) -> Path:
    raw = str(value).strip().strip('"').strip("'")
    if not raw:
        raise ValueError("path cannot be blank")

    folded = raw.casefold()
    if folded == ASSISTANT_FOLDER_TOKEN:
        raise ValueError("assistant folder must be resolved from allowed search directories")
    if folded.startswith(ASSISTANT_FOLDER_TOKEN + "/") or folded.startswith(
        ASSISTANT_FOLDER_TOKEN + "\\"
    ):
        raise ValueError("assistant folder must be resolved from allowed search directories")

    home = Path.home()
    for token, folder in USER_FOLDER_TOKENS.items():
        if folded == token:
            return (home / folder).resolve()
        if folded.startswith(token + "/") or folded.startswith(token + "\\"):
            suffix = raw[len(token) :].lstrip("/\\")
            return (home / folder / suffix).resolve()

    normalized = raw.replace("\\", "/")
    first, _, remainder = normalized.partition("/")
    if first.casefold() in KNOWN_USER_FOLDERS:
        base = home / KNOWN_USER_FOLDERS[first.casefold()]
        return (base / remainder).resolve() if remainder else base.resolve()

    path = Path(raw).expanduser()
    return path.resolve()
