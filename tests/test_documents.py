from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, SkillCall
from assistant.language.embeddings import LexicalFallbackEmbeddingProvider
from assistant.policy.file_access import FileAccessPolicy
from assistant.response import response_from_result
from assistant.skills.documents import (
    DocumentContentSearchSkill,
    DocumentExtractionError,
    DocumentExtractor,
    LocalDocumentSkill,
)
from assistant.storage.database import Database
from assistant.storage.vector_index import VectorIndex


def _call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="documents-test"),
    )


def _write_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)


def test_extracts_text_csv_and_office_locators(tmp_path: Path):
    extractor = DocumentExtractor()
    text_path = tmp_path / "notes.txt"
    text_path.write_text("First line\nSecond line", encoding="utf-8")
    csv_path = tmp_path / "budget.csv"
    csv_path.write_text("team,cost\nCore,42", encoding="utf-8")
    docx_path = tmp_path / "brief.docx"
    _write_zip(
        docx_path,
        {
            "word/document.xml": (
                '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Launch brief</w:t>'
                "</w:r></w:p></w:body></w:document>"
            )
        },
    )
    xlsx_path = tmp_path / "plan.xlsx"
    _write_zip(
        xlsx_path,
        {
            "xl/workbook.xml": (
                '<workbook xmlns="urn:x"><sheets><sheet name="Milestones"/></sheets></workbook>'
            ),
            "xl/sharedStrings.xml": (
                '<sst xmlns="urn:x"><si><t>Release</t></si><si><t>Friday</t></si></sst>'
            ),
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="urn:x"><sheetData><row r="2"><c t="s"><v>0</v></c>'
                '<c t="s"><v>1</v></c></row></sheetData></worksheet>'
            ),
        },
    )
    pptx_path = tmp_path / "deck.pptx"
    _write_zip(
        pptx_path,
        {
            "ppt/slides/slide1.xml": (
                '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Roadmap</a:t>'
                "<a:t>Phase one</a:t></p:sld>"
            )
        },
    )

    text = extractor.extract(text_path, max_characters=10_000)
    csv_doc = extractor.extract(csv_path, max_characters=10_000)
    docx = extractor.extract(docx_path, max_characters=10_000)
    xlsx = extractor.extract(xlsx_path, max_characters=10_000)
    pptx = extractor.extract(pptx_path, max_characters=10_000)

    assert text.sections[0].locator == "lines 1-2"
    assert csv_doc.sections[1].locator == "row 2"
    assert docx.sections[0].locator == "paragraph 1"
    assert xlsx.sections[0].locator == "sheet Milestones, row 2"
    assert xlsx.sections[0].text == "Release | Friday"
    assert pptx.sections[0].locator == "slide 1"


def test_extraction_is_bounded_and_rejects_binary_large_and_unsupported(tmp_path: Path):
    extractor = DocumentExtractor()
    long_path = tmp_path / "long.txt"
    long_path.write_text("a" * 5_000, encoding="utf-8")
    extracted = extractor.extract(long_path, max_characters=1_000)
    assert extracted.truncated is True
    assert sum(len(section.text) for section in extracted.sections) == 1_000

    binary_path = tmp_path / "binary.txt"
    binary_path.write_bytes(b"\x00not text")
    with pytest.raises(DocumentExtractionError, match="binary") as binary_error:
        extractor.extract(binary_path, max_characters=10_000)
    assert binary_error.value.code == "binary_content"

    unsupported_path = tmp_path / "legacy.doc"
    unsupported_path.write_text("legacy", encoding="utf-8")
    with pytest.raises(DocumentExtractionError, match="Unsupported") as unsupported_error:
        extractor.extract(unsupported_path, max_characters=10_000)
    assert unsupported_error.value.code == "unsupported_format"


@pytest.mark.asyncio
async def test_document_reader_has_content_permission_locators_and_grounded_answer(tmp_path: Path):
    path = tmp_path / "launch.md"
    path.write_text(
        "Launch is Friday.\nAction: Rowan must verify rollback on Thursday.",
        encoding="utf-8",
    )
    skill = LocalDocumentSkill()
    call = _call(
        "documents.read",
        {
            "operation": "answer",
            "paths": [str(path)],
            "question": "When is launch?",
        },
    )

    permission = skill.required_permissions(call)[0]
    result = await skill.execute(call, asyncio.Event())

    assert permission.capability == "filesystem.content.read"
    assert permission.targets == (str(path.resolve()),)
    assert "Launch is Friday" in (result.display_text or "")
    assert "lines 1-2" in (result.display_text or "")
    assert result.sensitive is True
    assert result.items and result.items[0].metadata["local_reference"].startswith("document:")
    response = response_from_result(result)
    assert response.citations[0].source_type.value == "local_document"
    assert response.citations[0].local_reference.startswith("document:")
    assert response_from_result(result, hide_sensitive_display=True).citations == ()


@pytest.mark.asyncio
async def test_document_index_is_incremental_searchable_and_removes_deleted_files(tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir()
    path = root / "launch.txt"
    path.write_text("The release rollback rehearsal happens Thursday.", encoding="utf-8")
    database = Database(f"sqlite:///{tmp_path / 'documents.db'}")
    database.migrate()
    index = VectorIndex(database)
    skill = DocumentContentSearchSkill(index, LexicalFallbackEmbeddingProvider())

    index_call = _call("documents.content_search", {"operation": "index", "root": str(root)})
    permissions = skill.required_permissions(index_call)
    first = await skill.execute(index_call, asyncio.Event())
    second = await skill.execute(index_call, asyncio.Event())
    search = await skill.execute(
        _call(
            "documents.content_search",
            {"operation": "search", "root": str(root), "query": "rollback rehearsal"},
        ),
        asyncio.Event(),
    )

    assert [permission.capability for permission in permissions] == [
        "filesystem.content.read",
        "filesystem.content.index",
    ]
    assert first.data["files_indexed"] == 1
    assert second.data["files_unchanged"] == 1
    assert search.data["hits"][0]["locator"] == "lines 1-1"
    assert search.items and search.items[0].sensitive is True

    path.unlink()
    removed = await skill.execute(index_call, asyncio.Event())
    assert removed.data["files_removed"] == 1
    assert removed.data["indexed_sections"] == 0


def test_file_access_policy_applies_to_content_read_and_index(tmp_path: Path):
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    policy = FileAccessPolicy(read_whitelist=(str(allowed),), read_blacklist=(str(blocked),))

    assert policy.denial_reason("filesystem.content.read", str(allowed / "a.pdf")) is None
    assert "only inside" in (
        policy.denial_reason("filesystem.content.index", str(tmp_path / "other")) or ""
    )
    assert "blocks read" in (
        policy.denial_reason("filesystem.content.read", str(blocked / "secret.txt")) or ""
    )
