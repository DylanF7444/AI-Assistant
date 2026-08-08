from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field, model_validator

from assistant.contracts import (
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.language.embeddings import EmbeddingProvider
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.storage.vector_index import VectorIndex
from assistant.windows.paths import resolve_user_path

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 200_000
MAX_FILES_PER_OPERATION = 200
MAX_SECTIONS_PER_FILE = 500
DOCUMENT_NAMESPACE = "local_document"

TEXT_EXTENSIONS = {
    ".csv",
    ".json",
    ".log",
    ".md",
    ".rst",
    ".text",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | OFFICE_EXTENSIONS | {".pdf"}


class DocumentExtractionError(ValueError):
    """An expected, user-actionable document extraction failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DocumentSection(BaseModel):
    locator: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=MAX_EXTRACTED_CHARACTERS)


class ExtractedDocument(BaseModel):
    path: str
    format: str
    size_bytes: int = Field(ge=0)
    sections: list[DocumentSection] = Field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class LocalDocumentInput(BaseModel):
    operation: Literal["extract", "summarize", "action_items", "compare", "answer"]
    paths: list[str] = Field(min_length=1, max_length=5)
    question: str | None = Field(default=None, min_length=1, max_length=2_000)
    max_characters: int = Field(default=40_000, ge=1_000, le=MAX_EXTRACTED_CHARACTERS)

    @model_validator(mode="after")
    def operation_fields_match(self) -> LocalDocumentInput:
        if self.operation == "compare" and len(self.paths) < 2:
            raise ValueError("compare requires at least two documents")
        if self.operation == "answer" and not self.question:
            raise ValueError("answer requires a question")
        if self.operation != "answer" and self.question is not None:
            raise ValueError("question is only supported by answer")
        return self


class LocalDocumentOutput(BaseModel):
    operation: str
    documents: list[ExtractedDocument]
    answer: str | None = None
    action_items: list[str] = Field(default_factory=list)


class DocumentSearchInput(BaseModel):
    operation: Literal["search", "index", "clear_index", "status"]
    root: str
    query: str | None = Field(default=None, min_length=1, max_length=1_000)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def search_requires_query(self) -> DocumentSearchInput:
        if self.operation == "search" and not self.query:
            raise ValueError("search requires a query")
        if self.operation != "search" and self.query is not None:
            raise ValueError("query is only supported by search")
        return self


class DocumentSearchHit(BaseModel):
    path: str
    locator: str
    excerpt: str
    score: float = Field(ge=-1, le=1)


class DocumentSearchOutput(BaseModel):
    operation: str
    root: str
    files_seen: int = Field(default=0, ge=0)
    files_indexed: int = Field(default=0, ge=0)
    files_removed: int = Field(default=0, ge=0)
    files_unchanged: int = Field(default=0, ge=0)
    failures: list[dict[str, str]] = Field(default_factory=list)
    hits: list[DocumentSearchHit] = Field(default_factory=list)
    indexed_sections: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class _IndexedFile:
    path: str
    signature: str
    source_ids: tuple[str, ...]


class DocumentExtractor:
    """Bounded, offline document extraction with no implicit folder traversal."""

    def extract(self, path: Path, *, max_characters: int) -> ExtractedDocument:
        path = path.resolve()
        if not path.is_file():
            raise DocumentExtractionError("not_found", f"Document does not exist: {path}")
        suffix = path.suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise DocumentExtractionError(
                "unsupported_format",
                f"Unsupported document format {suffix or '(none)'}: {path.name}",
            )
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise DocumentExtractionError(
                "too_large",
                f"Document exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB extraction limit: "
                f"{path.name}",
            )
        try:
            if suffix in TEXT_EXTENSIONS:
                sections = self._extract_text(path, suffix)
            elif suffix == ".docx":
                sections = self._extract_docx(path)
            elif suffix == ".xlsx":
                sections = self._extract_xlsx(path)
            elif suffix == ".pptx":
                sections = self._extract_pptx(path)
            else:
                sections = self._extract_pdf(path)
        except DocumentExtractionError:
            raise
        except (
            OSError,
            UnicodeError,
            ValueError,
            zipfile.BadZipFile,
            ElementTree.ParseError,
        ) as exc:
            raise DocumentExtractionError(
                "corrupt_document",
                f"Could not extract {path.name}; it may be corrupt: {exc.__class__.__name__}",
            ) from exc
        bounded, truncated = _bounded_sections(sections, max_characters)
        return ExtractedDocument(
            path=str(path),
            format=suffix.removeprefix("."),
            size_bytes=size,
            sections=bounded,
            truncated=truncated,
            warnings=(
                [f"Extraction stopped at {max_characters:,} characters."] if truncated else []
            ),
        )

    @staticmethod
    def _extract_text(path: Path, suffix: str) -> list[DocumentSection]:
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            raise DocumentExtractionError(
                "binary_content", f"{path.name} appears to contain binary data."
            )
        text = raw.decode("utf-8-sig")
        if suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            rows = csv.reader(io.StringIO(text), delimiter=delimiter)
            sections = []
            for row_number, row in enumerate(rows, 1):
                value = " | ".join(cell.strip() for cell in row).strip(" |")
                if value:
                    sections.append(DocumentSection(locator=f"row {row_number}", text=value))
                if len(sections) >= MAX_SECTIONS_PER_FILE:
                    break
            return sections
        return [
            DocumentSection(
                locator=f"lines {start}-{start + len(lines) - 1}", text="\n".join(lines)
            )
            for start, lines in _line_chunks(text)
            if any(line.strip() for line in lines)
        ]

    @staticmethod
    def _extract_docx(path: Path) -> list[DocumentSection]:
        with zipfile.ZipFile(path) as archive:
            _reject_encrypted_zip(archive, path)
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        paragraphs = []
        for paragraph_number, paragraph in enumerate(_xml_local_elements(root, "p"), 1):
            text = "".join(element.text or "" for element in _xml_local_elements(paragraph, "t"))
            if text.strip():
                paragraphs.append(
                    DocumentSection(locator=f"paragraph {paragraph_number}", text=text.strip())
                )
            if len(paragraphs) >= MAX_SECTIONS_PER_FILE:
                break
        return paragraphs

    @staticmethod
    def _extract_xlsx(path: Path) -> list[DocumentSection]:
        with zipfile.ZipFile(path) as archive:
            _reject_encrypted_zip(archive, path)
            shared = _xlsx_shared_strings(archive)
            sheet_names = _xlsx_sheet_names(archive)
            sections = []
            sheet_paths = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
            for sheet_index, sheet_path in enumerate(sheet_paths, 1):
                sheet_name = sheet_names.get(sheet_index, f"Sheet {sheet_index}")
                root = ElementTree.fromstring(archive.read(sheet_path))
                for row in _xml_local_elements(root, "row"):
                    values = []
                    for cell in _xml_local_elements(row, "c"):
                        values.append(_xlsx_cell_value(cell, shared))
                    text = " | ".join(value for value in values if value).strip()
                    if text:
                        row_number = row.attrib.get("r", "?")
                        sections.append(
                            DocumentSection(
                                locator=f"sheet {sheet_name}, row {row_number}", text=text
                            )
                        )
                    if len(sections) >= MAX_SECTIONS_PER_FILE:
                        return sections
        return sections

    @staticmethod
    def _extract_pptx(path: Path) -> list[DocumentSection]:
        with zipfile.ZipFile(path) as archive:
            _reject_encrypted_zip(archive, path)
            slide_paths = sorted(
                (
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=_numeric_filename_key,
            )
            sections = []
            for slide_number, slide_path in enumerate(slide_paths, 1):
                root = ElementTree.fromstring(archive.read(slide_path))
                text = "\n".join(
                    value.strip()
                    for element in _xml_local_elements(root, "t")
                    if (value := element.text or "").strip()
                )
                if text:
                    sections.append(DocumentSection(locator=f"slide {slide_number}", text=text))
        return sections

    @staticmethod
    def _extract_pdf(path: Path) -> list[DocumentSection]:
        try:
            from pypdf import PdfReader
            from pypdf.errors import FileNotDecryptedError, PdfReadError
        except ImportError as exc:
            raise DocumentExtractionError(
                "extractor_unavailable",
                "PDF extraction requires the optional documents dependency.",
            ) from exc
        try:
            reader = PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(""):
                raise DocumentExtractionError(
                    "encrypted_document", f"{path.name} is password protected."
                )
            sections = []
            for page_number, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    sections.append(DocumentSection(locator=f"page {page_number}", text=text))
                if len(sections) >= MAX_SECTIONS_PER_FILE:
                    break
            return sections
        except DocumentExtractionError:
            raise
        except (FileNotDecryptedError, PdfReadError) as exc:
            code = "encrypted_document" if "decrypt" in str(exc).casefold() else "corrupt_document"
            raise DocumentExtractionError(code, f"Could not read {path.name}: {exc}") from exc


class LocalDocumentSkill(Skill):
    input_model = LocalDocumentInput
    output_model = LocalDocumentOutput

    def __init__(self, extractor: DocumentExtractor | None = None) -> None:
        self.extractor = extractor or DocumentExtractor()
        self.manifest = manifest(
            skill_id="documents.read",
            display_name="Local Document Reader",
            description=(
                "Extract, summarize, compare, find action items in, or answer a question from "
                "up to five explicitly selected local documents. Supports bounded text, CSV, "
                "PDF, DOCX, XLSX, and PPTX extraction with source locators."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="filesystem.content.read",
                    reason="Read the contents of explicitly selected local documents.",
                ),
            ),
            safe_to_retry=True,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        return (
            PermissionScope(
                capability="filesystem.content.read",
                targets=tuple(str(resolve_user_path(path)) for path in args.paths),
                reason="Read the contents of these explicitly selected local documents.",
            ),
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        documents = []
        for value in args.paths:
            if cancel_event.is_set():
                return SkillResult(
                    status=ResultStatus.CANCELLED,
                    summary="Document reading was cancelled.",
                    data=LocalDocumentOutput(
                        operation=args.operation, documents=documents
                    ).model_dump(),
                    sensitive=True,
                )
            document = await asyncio.to_thread(
                self.extractor.extract,
                resolve_user_path(value),
                max_characters=args.max_characters,
            )
            documents.append(document)

        answer = None
        action_items: list[str] = []
        if args.operation == "summarize":
            answer = _summarize_documents(documents)
        elif args.operation == "action_items":
            action_items = _action_items(documents)
            answer = "\n".join(f"- {item}" for item in action_items) or "No action items found."
        elif args.operation == "compare":
            answer = _compare_documents(documents)
        elif args.operation == "answer":
            answer = _answer_from_documents(args.question or "", documents)

        output = LocalDocumentOutput(
            operation=args.operation,
            documents=documents,
            answer=answer,
            action_items=action_items,
        )
        display = answer or _format_extracted_documents(documents)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=f"Read {len(documents)} local document{'s' if len(documents) != 1 else ''}.",
            data=output.model_dump(),
            items=tuple(_document_items(documents)),
            display_text=display,
            sensitive=True,
        )


class DocumentContentSearchSkill(Skill):
    input_model = DocumentSearchInput
    output_model = DocumentSearchOutput

    def __init__(
        self,
        index: VectorIndex,
        embedding_provider: EmbeddingProvider,
        extractor: DocumentExtractor | None = None,
    ) -> None:
        self.index = index
        self.embedding_provider = embedding_provider
        self.extractor = extractor or DocumentExtractor()
        self.manifest = manifest(
            skill_id="documents.content_search",
            display_name="Document Content Search",
            description=(
                "Search or explicitly build, inspect, and clear a persistent content index for "
                "supported documents in one approved folder. Indexing is bounded and detects "
                "modified and deleted files."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="filesystem.content.read",
                    reason="Read document contents in the selected folder.",
                ),
                PermissionScope(
                    capability="filesystem.content.index",
                    reason="Retain an explicit local document content index.",
                ),
            ),
            risk=RiskLevel.MEDIUM,
            safe_to_retry=True,
        )

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        root = str(resolve_user_path(args.root))
        if args.operation in {"status", "search"}:
            return (
                PermissionScope(
                    capability="filesystem.content.read",
                    targets=(root,),
                    reason="Search retained document content for this approved folder.",
                ),
            )
        scopes = []
        if args.operation == "index":
            scopes.append(
                PermissionScope(
                    capability="filesystem.content.read",
                    targets=(root,),
                    reason="Read supported document contents in this approved folder.",
                )
            )
        scopes.append(
            PermissionScope(
                capability="filesystem.content.index",
                targets=(root,),
                reason=(
                    "Retain an explicit local document content index for this folder."
                    if args.operation == "index"
                    else "Remove the retained local document content index for this folder."
                ),
            )
        )
        return tuple(scopes)

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        root = resolve_user_path(args.root)
        descriptions = {
            "index": f"Read supported documents under {root} and retain a local content index",
            "clear_index": f"Delete retained document content for {root} from the local index",
            "search": f"Search retained document content for {root}",
            "status": f"Inspect retained document-index status for {root}",
        }
        return descriptions[args.operation]

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        root = resolve_user_path(args.root).resolve()
        if not root.is_dir():
            raise DocumentExtractionError("not_found", f"Folder does not exist: {root}")
        if args.operation == "index":
            output = await self._refresh(root, cancel_event)
        elif args.operation == "clear_index":
            removed = self.index.delete_where(DOCUMENT_NAMESPACE, root=str(root))
            output = DocumentSearchOutput(
                operation=args.operation, root=str(root), files_removed=removed
            )
        elif args.operation == "status":
            records = self.index.items_where(DOCUMENT_NAMESPACE, root=str(root))
            output = DocumentSearchOutput(
                operation=args.operation,
                root=str(root),
                files_seen=len({item.metadata.get("path") for item in records}),
                indexed_sections=len(records),
            )
        else:
            output = await self._search(root, args.query or "", args.limit)
        display = _format_search_output(output)
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=display.splitlines()[0],
            data=output.model_dump(),
            items=tuple(_search_items(output.hits)),
            display_text=display,
            sensitive=True,
        )

    async def _refresh(self, root: Path, cancel_event: asyncio.Event) -> DocumentSearchOutput:
        files = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
            ),
            key=lambda path: str(path).casefold(),
        )[:MAX_FILES_PER_OPERATION]
        existing_records = self.index.items_where(DOCUMENT_NAMESPACE, root=str(root))
        existing = _group_indexed_files(existing_records)
        current_paths = {str(path.resolve()) for path in files}
        removed = 0
        for path, indexed in existing.items():
            if path not in current_paths:
                removed += self.index.delete_sources(DOCUMENT_NAMESPACE, set(indexed.source_ids))
        indexed_count = 0
        unchanged = 0
        failures = []
        for path in files:
            if cancel_event.is_set():
                break
            resolved = str(path.resolve())
            signature = _file_signature(path)
            old = existing.get(resolved)
            if old and old.signature == signature:
                unchanged += 1
                continue
            if old:
                self.index.delete_sources(DOCUMENT_NAMESPACE, set(old.source_ids))
            try:
                document = await asyncio.to_thread(
                    self.extractor.extract, path, max_characters=MAX_EXTRACTED_CHARACTERS
                )
                texts = [section.text for section in document.sections]
                embeddings = await self.embedding_provider.embed(texts) if texts else []
                file_id = hashlib.sha256(resolved.casefold().encode("utf-8")).hexdigest()[:24]
                for section_number, (section, embedding) in enumerate(
                    zip(document.sections, embeddings, strict=True), 1
                ):
                    self.index.upsert(
                        namespace=DOCUMENT_NAMESPACE,
                        source_id=f"{file_id}:{section_number}",
                        text=section.text,
                        metadata={
                            "root": str(root),
                            "path": resolved,
                            "locator": section.locator,
                            "signature": signature,
                            "format": document.format,
                        },
                        embedding=embedding,
                        embedding_model=self.embedding_provider.__class__.__name__,
                    )
                indexed_count += 1
            except DocumentExtractionError as exc:
                failures.append({"path": resolved, "code": exc.code})
        return DocumentSearchOutput(
            operation="index",
            root=str(root),
            files_seen=len(files),
            files_indexed=indexed_count,
            files_removed=removed,
            files_unchanged=unchanged,
            failures=failures,
            indexed_sections=len(self.index.items_where(DOCUMENT_NAMESPACE, root=str(root))),
        )

    async def _search(self, root: Path, query: str, limit: int) -> DocumentSearchOutput:
        embeddings = await self.embedding_provider.embed([query])
        candidates = (
            self.index.search(embeddings[0], namespaces={DOCUMENT_NAMESPACE}, limit=500)
            if embeddings
            else ()
        )
        hits = []
        for candidate in candidates:
            if candidate.metadata.get("root") != str(root):
                continue
            hits.append(
                DocumentSearchHit(
                    path=str(candidate.metadata.get("path", "")),
                    locator=str(candidate.metadata.get("locator", "section")),
                    excerpt=_excerpt(candidate.text),
                    score=candidate.score,
                )
            )
            if len(hits) >= limit:
                break
        return DocumentSearchOutput(
            operation="search",
            root=str(root),
            hits=hits,
            indexed_sections=len(self.index.items_where(DOCUMENT_NAMESPACE, root=str(root))),
        )


def _bounded_sections(
    sections: list[DocumentSection], max_characters: int
) -> tuple[list[DocumentSection], bool]:
    bounded = []
    remaining = min(max_characters, MAX_EXTRACTED_CHARACTERS)
    truncated = len(sections) > MAX_SECTIONS_PER_FILE
    for section in sections[:MAX_SECTIONS_PER_FILE]:
        if remaining <= 0:
            truncated = True
            break
        text = section.text[:remaining]
        if text.strip():
            bounded.append(section.model_copy(update={"text": text}))
        if len(text) < len(section.text):
            truncated = True
        remaining -= len(text)
    return bounded, truncated


def _line_chunks(text: str, chunk_lines: int = 40):
    lines = text.splitlines()
    for index in range(0, len(lines), chunk_lines):
        yield index + 1, lines[index : index + chunk_lines]


def _xml_local_elements(root: ElementTree.Element, local_name: str):
    return (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == local_name)


def _reject_encrypted_zip(archive: zipfile.ZipFile, path: Path) -> None:
    if any(info.flag_bits & 0x1 for info in archive.infolist()):
        raise DocumentExtractionError("encrypted_document", f"{path.name} is encrypted.")


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(element.text or "" for element in _xml_local_elements(item, "t"))
        for item in _xml_local_elements(root, "si")
    ]


def _xlsx_sheet_names(archive: zipfile.ZipFile) -> dict[int, str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except KeyError:
        return {}
    return {
        index: sheet.attrib.get("name", f"Sheet {index}")
        for index, sheet in enumerate(_xml_local_elements(root, "sheet"), 1)
    }


def _xlsx_cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(element.text or "" for element in _xml_local_elements(cell, "t"))
    value = next((element.text or "" for element in _xml_local_elements(cell, "v")), "")
    if kind == "s" and value.isdigit() and int(value) < len(shared):
        return shared[int(value)]
    return value


def _numeric_filename_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)(?=\.xml$)", value)
    return (int(match.group(1)) if match else 0, value)


def _document_text(document: ExtractedDocument) -> str:
    return "\n".join(section.text for section in document.sections)


def _sentences(text: str) -> list[str]:
    return [value.strip() for value in re.split(r"(?<=[.!?])\s+|\n+", text) if value.strip()]


def _summarize_documents(documents: list[ExtractedDocument]) -> str:
    lines = []
    for document in documents:
        sentences = _sentences(_document_text(document))
        summary = " ".join(sentences[:5])
        lines.append(f"{Path(document.path).name}: {summary or 'No extractable text.'}")
    return "\n\n".join(lines)


def _action_items(documents: list[ExtractedDocument]) -> list[str]:
    pattern = re.compile(r"\b(todo|action|follow[- ]?up|must|need to|please)\b", re.IGNORECASE)
    items = []
    for document in documents:
        for sentence in _sentences(_document_text(document)):
            if pattern.search(sentence) and sentence not in items:
                items.append(sentence[:500])
            if len(items) >= 50:
                return items
    return items


def _compare_documents(documents: list[ExtractedDocument]) -> str:
    token_sets = []
    for document in documents:
        token_sets.append(set(re.findall(r"[a-z0-9]{3,}", _document_text(document).casefold())))
    common = set.intersection(*token_sets) if token_sets else set()
    lines = ["Shared topics: " + (", ".join(sorted(common)[:20]) or "none detected")]
    for document, tokens in zip(documents, token_sets, strict=True):
        unique = tokens - set().union(*(other for other in token_sets if other is not tokens))
        lines.append(f"{Path(document.path).name} only: {', '.join(sorted(unique)[:20]) or 'none'}")
    return "\n".join(lines)


def _answer_from_documents(question: str, documents: list[ExtractedDocument]) -> str:
    terms = set(re.findall(r"[a-z0-9]{3,}", question.casefold()))
    scored = []
    for document in documents:
        for section in document.sections:
            section_terms = Counter(re.findall(r"[a-z0-9]{3,}", section.text.casefold()))
            score = sum(section_terms[term] for term in terms)
            if score:
                scored.append((score, Path(document.path).name, section.locator, section.text))
    if not scored:
        return "I could not find a grounded passage that answers that question."
    lines = []
    for _, name, locator, text in sorted(scored, reverse=True)[:5]:
        lines.append(f"[{name}, {locator}] {_excerpt(text, 500)}")
    return "Relevant passages:\n" + "\n".join(lines)


def _format_extracted_documents(documents: list[ExtractedDocument]) -> str:
    lines = []
    for document in documents:
        lines.append(f"{Path(document.path).name}:")
        for section in document.sections:
            lines.append(f"[{section.locator}] {section.text}")
        lines.extend(f"Warning: {warning}" for warning in document.warnings)
    return "\n".join(lines)


def _document_items(documents: list[ExtractedDocument]):
    for index, document in enumerate(documents, 1):
        reference = hashlib.sha256(document.path.casefold().encode("utf-8")).hexdigest()[:24]
        yield RequestResultItem(
            item_id=f"document-{index}",
            title=Path(document.path).name,
            value=document.path,
            kind="local_document",
            description=f"{len(document.sections)} extracted sections",
            metadata={
                "local_reference": f"document:{reference}",
                "path": document.path,
                "format": document.format,
                "locators": [section.locator for section in document.sections[:20]],
            },
            sensitive=True,
        )


def _file_signature(path: Path) -> str:
    stat = path.stat()
    payload = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _group_indexed_files(records) -> dict[str, _IndexedFile]:
    grouped: dict[str, dict] = {}
    for record in records:
        path = str(record.metadata.get("path", ""))
        if not path:
            continue
        entry = grouped.setdefault(
            path,
            {"signature": str(record.metadata.get("signature", "")), "source_ids": []},
        )
        entry["source_ids"].append(record.source_id)
    return {
        path: _IndexedFile(path, value["signature"], tuple(value["source_ids"]))
        for path, value in grouped.items()
    }


def _excerpt(text: str, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _search_items(hits: list[DocumentSearchHit]):
    for index, hit in enumerate(hits, 1):
        reference = hashlib.sha256(hit.path.casefold().encode("utf-8")).hexdigest()[:24]
        yield RequestResultItem(
            item_id=f"document-hit-{index}",
            title=f"{Path(hit.path).name} — {hit.locator}",
            value={"path": hit.path, "locator": hit.locator},
            kind="local_document_excerpt",
            description=hit.excerpt,
            metadata={
                "path": hit.path,
                "locator": hit.locator,
                "local_reference": f"document:{reference}",
                "score": hit.score,
            },
            sensitive=True,
        )


def _format_search_output(output: DocumentSearchOutput) -> str:
    if output.operation == "search":
        lines = [f"Found {len(output.hits)} matching document section(s)."]
        lines.extend(
            f"{index}. {Path(hit.path).name}, {hit.locator}: {hit.excerpt}"
            for index, hit in enumerate(output.hits, 1)
        )
        return "\n".join(lines)
    if output.operation == "status":
        return (
            f"The folder index contains {output.indexed_sections} section(s) from "
            f"{output.files_seen} file(s)."
        )
    if output.operation == "clear_index":
        return f"Removed {output.files_removed} retained document section(s)."
    return (
        f"Indexed {output.files_indexed} changed file(s); {output.files_unchanged} unchanged, "
        f"{output.files_removed} removed, {len(output.failures)} failed. "
        f"The index now contains {output.indexed_sections} section(s)."
    )
