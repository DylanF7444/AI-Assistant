from __future__ import annotations

import sys
from pathlib import Path


def packaged_entries(executable: Path) -> set[str]:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as error:
        raise RuntimeError("PyInstaller is required for packaged media verification") from error

    archive = CArchiveReader(str(executable))
    entries = {str(name).replace("\\", "/").casefold() for name in archive.toc}
    for name in tuple(archive.toc):
        if not str(name).casefold().endswith(".pyz"):
            continue
        try:
            embedded = archive.open_embedded_archive(name)
        except Exception:
            continue
        entries.update(
            str(item).replace("\\", "/").casefold() for item in getattr(embedded, "toc", {})
        )
    return entries


def verify(executable: Path) -> tuple[str, ...]:
    if not executable.is_file():
        raise FileNotFoundError(f"Packaged executable does not exist: {executable}")
    entries = packaged_entries(executable)
    requirements = {
        "Qt Multimedia Python module": (
            "pyside6.qtmultimedia",
            "pyside6/qtmultimedia.pyd",
        ),
        "Qt Multimedia Widgets Python module": (
            "pyside6.qtmultimediawidgets",
            "pyside6/qtmultimediawidgets.pyd",
        ),
        "Qt Multimedia runtime": ("qt6multimedia.dll",),
        "Qt media backend plugin": (
            "plugins/multimedia/ffmpegmediaplugin.dll",
            "plugins/multimedia/windowsmediaplugin.dll",
        ),
        "Qt image decoder plugins": (
            "plugins/imageformats/qjpeg.dll",
            "plugins/imageformats/qgif.dll",
            "plugins/imageformats/qwebp.dll",
        ),
    }
    missing = []
    for label, alternatives in requirements.items():
        if not any(any(candidate in entry for entry in entries) for candidate in alternatives):
            missing.append(label)
    return tuple(missing)


def main(arguments: list[str] | None = None) -> int:
    values = arguments if arguments is not None else sys.argv[1:]
    if len(values) != 1:
        print("Usage: verify_packaged_media.py <LocalAssistant.exe>", file=sys.stderr)
        return 2
    executable = Path(values[0]).resolve()
    try:
        missing = verify(executable)
    except Exception as error:
        print(f"Packaged media verification failed: {error}", file=sys.stderr)
        return 1
    if missing:
        print(
            "Packaged media verification is missing: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    print("Packaged media runtime verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
