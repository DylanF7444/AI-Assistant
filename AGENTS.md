# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12 project using a `src/` layout. Core application code lives in `src/assistant/`, with modules grouped by responsibility: `audio/`, `execution/`, `integrations/`, `language/`, `policy/`, `router/`, `skills/`, `storage/`, `stt/`, `tts/`, `ui/`, and `windows/`. Production regression tests are in `tests/`. Guided, explanatory tests are in `learning_tests/`. User and architecture documentation lives in `docs/`; release packaging files are in `packaging/`; helper and benchmark scripts are in `scripts/` and `benchmarks/`. Build outputs such as `build/`, `dist/`, and `.benchmark-data/` should not be treated as source.

## Build, Test, and Development Commands

- `python -m pip install -e .[test]`: install the package in editable mode with pytest dependencies.
- `python -m pytest`: run the main regression suite configured in `pyproject.toml`.
- `python -m pytest learning_tests -v`: run the guided learning suite.
- `python -m assistant.app`: start the local assistant application.
- `python -m pip install -e ".[desktop,audio,stt,build]"`: install optional desktop/audio/STT/build dependencies.
- `powershell -ExecutionPolicy Bypass -File scripts/build.ps1`: run tests, build the PyInstaller artifact, and optionally compile the installer if Inno Setup is installed.

## Coding Style & Naming Conventions

Follow idiomatic Python with 4-space indentation, type-focused interfaces, and clear module boundaries. Keep imports explicit and prefer existing package patterns over new abstractions. Use `snake_case` for modules, functions, variables, and test names; use `PascalCase` for classes and Pydantic models. Ruff formatting and lint rules are configured in `pyproject.toml`; run both checks before submitting changes.

## Testing Guidelines

Use pytest for all tests. Add regression coverage in `tests/` for production behavior and use `learning_tests/` only for explanatory examples. Name files `test_<topic>.py` and tests `test_<behavior>()`. Prefer fake providers, temporary directories, and explicit permission fixtures so tests remain offline and deterministic. Run `python -m pytest` before submitting changes; run targeted tests while iterating, for example `python -m pytest tests/test_router.py -v`.

## Commit & Pull Request Guidelines

Recent commit messages are informal, often just `update`, so prefer improving clarity with short imperative summaries such as `Add permission audit coverage` or `Fix device routing normalization`. Keep each commit focused. Pull requests should include a concise description, tests run, linked issues or checklist items, and screenshots only for UI-visible changes.

## Security & Configuration Tips

This assistant treats transcripts and model output as untrusted input. Preserve schema validation, scoped permissions, confirmations for sensitive actions, and local audit behavior. Do not commit secrets, local model files, generated installers, or machine-specific configuration.

## Codex Agent Roles

- Use `gpt-5.6-sol` with `xhigh` reasoning as the main planning agent for this project.
- Use `gpt-5.6-luna` with `max` reasoning for delegated implementation work. When spawning an implementation sub-agent, set the explicit model and reasoning override because child agents otherwise inherit the parent model.
