import pytest

from assistant.windows.paths import resolve_user_path


def test_assistant_folder_token_requires_allowed_search_resolution():
    with pytest.raises(ValueError, match="allowed search directories"):
        resolve_user_path("${assistant_folder}/my_text_file.txt")


def test_unknown_path_placeholder_is_not_resolved_relative_to_working_directory():
    with pytest.raises(ValueError, match="unsupported path placeholder"):
        resolve_user_path("${documents_folder}/roblox_places/*.*")
