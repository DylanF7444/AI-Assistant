from pathlib import Path

from assistant.app import _parse_args, main


def test_parse_args_accepts_isolated_data_directory(tmp_path: Path) -> None:
    args = _parse_args(["--data-dir", str(tmp_path), "--text", "calculate 1 plus 2"])

    assert args.data_dir == tmp_path
    assert args.text == "calculate 1 plus 2"


def test_packaged_worker_mode_routes_without_building_the_desktop(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        "assistant.skills.worker.main",
        lambda arguments: captured.append(arguments) or 0,
    )

    assert main(["--isolated-skill-worker", "package", "plugin:Skill"]) == 0
    assert captured == [["package", "plugin:Skill"]]
