import pytest

from assistant.windows.paths import resolve_user_path


def test_assistant_folder_token_requires_allowed_search_resolution():
    with pytest.raises(ValueError, match="allowed search directories"):
        resolve_user_path("${assistant_folder}/my_text_file.txt")
