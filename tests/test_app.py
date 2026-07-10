from pathlib import Path

from assistant.app import _parse_args


def test_parse_args_accepts_isolated_data_directory(tmp_path: Path) -> None:
    args = _parse_args(["--data-dir", str(tmp_path), "--text", "calculate 1 plus 2"])

    assert args.data_dir == tmp_path
    assert args.text == "calculate 1 plus 2"
