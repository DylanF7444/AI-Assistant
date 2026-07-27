from __future__ import annotations

import shutil
import sys
from collections.abc import Iterable
from pathlib import Path


def find_piper_voice_models(
    data_dir: Path,
    *,
    configured_model: Path | None = None,
    working_directory: Path | None = None,
    executable_directory: Path | None = None,
) -> tuple[Path, ...]:
    """Find likely Piper voices without scanning arbitrary user directories."""

    candidates: list[Path] = []
    if configured_model is not None and _is_piper_voice(configured_model):
        candidates.append(configured_model)
    for root in _model_roots(
        data_dir,
        working_directory=working_directory,
        executable_directory=executable_directory,
    ):
        if not root.is_dir():
            continue
        for model in root.rglob("*.onnx"):
            if _is_piper_voice(model):
                candidates.append(model)
    return _unique_existing_files(candidates)


def piper_model_dialog_directory(
    data_dir: Path,
    *,
    configured_model: Path | None = None,
    working_directory: Path | None = None,
    executable_directory: Path | None = None,
) -> Path:
    if configured_model is not None:
        expanded = configured_model.expanduser()
        if expanded.is_file():
            return expanded.parent
        if expanded.parent.is_dir():
            return expanded.parent
    discovered = find_piper_voice_models(
        data_dir,
        working_directory=working_directory,
        executable_directory=executable_directory,
    )
    if discovered:
        return discovered[0].parent
    return _first_existing_directory(
        _model_roots(
            data_dir,
            working_directory=working_directory,
            executable_directory=executable_directory,
        ),
        fallback=data_dir,
    )


def find_piper_executable(
    data_dir: Path,
    *,
    configured_executable: Path | None = None,
    working_directory: Path | None = None,
    executable_directory: Path | None = None,
) -> Path | None:
    candidates: list[Path] = []
    if configured_executable is not None:
        candidates.append(configured_executable.expanduser())
    for root in _application_roots(
        data_dir,
        working_directory=working_directory,
        executable_directory=executable_directory,
    ):
        candidates.extend((root / "piper.exe", root / "piper" / "piper.exe"))
    installed = shutil.which("piper")
    if installed:
        candidates.append(Path(installed))
    return next(iter(_unique_existing_files(candidates)), None)


def piper_executable_dialog_directory(
    data_dir: Path,
    *,
    configured_executable: Path | None = None,
    working_directory: Path | None = None,
    executable_directory: Path | None = None,
) -> Path:
    detected = find_piper_executable(
        data_dir,
        configured_executable=configured_executable,
        working_directory=working_directory,
        executable_directory=executable_directory,
    )
    if detected is not None:
        return detected.parent
    return _first_existing_directory(
        _application_roots(
            data_dir,
            working_directory=working_directory,
            executable_directory=executable_directory,
        ),
        fallback=data_dir,
    )


def _model_roots(
    data_dir: Path,
    *,
    working_directory: Path | None,
    executable_directory: Path | None,
) -> tuple[Path, ...]:
    return _unique_paths(
        root / "models"
        for root in _application_roots(
            data_dir,
            working_directory=working_directory,
            executable_directory=executable_directory,
        )
    )


def _application_roots(
    data_dir: Path,
    *,
    working_directory: Path | None,
    executable_directory: Path | None,
) -> tuple[Path, ...]:
    working_directory = working_directory or Path.cwd()
    executable_directory = executable_directory or Path(sys.executable).resolve().parent
    return _unique_paths((data_dir, working_directory, executable_directory))


def _is_piper_voice(model: Path) -> bool:
    if not model.is_file() or model.suffix.casefold() != ".onnx":
        return False
    return model.with_suffix(f"{model.suffix}.json").is_file()


def _unique_existing_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(path for path in _unique_paths(paths) if path.is_file())


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = path.expanduser().resolve()
        key = str(normalized).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(normalized)
    return tuple(unique)


def _first_existing_directory(paths: Iterable[Path], *, fallback: Path) -> Path:
    for path in _unique_paths(paths):
        if path.is_dir():
            return path
    return fallback.expanduser().resolve()
