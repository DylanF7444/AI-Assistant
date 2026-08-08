import json
from urllib.parse import urlsplit, urlunsplit

from assistant.contracts import (
    CitationSourceType,
    RequestResultItem,
    Response,
    ResponseCitation,
    ResponseType,
    ResultStatus,
    SkillResult,
)


def response_from_result(
    result: SkillResult,
    *,
    detail: str = "normal",
    allow_sensitive_speech: bool = False,
    hide_sensitive_display: bool = False,
) -> Response:
    severity = {
        ResultStatus.COMPLETED: "success",
        ResultStatus.PARTIAL: "warning",
        ResultStatus.FAILED: "error",
        ResultStatus.DENIED: "warning",
        ResultStatus.CANCELLED: "warning",
        ResultStatus.TIMED_OUT: "error",
    }[result.status]
    display = result.display_text or _display_with_items(result)
    if result.sensitive and hide_sensitive_display:
        display = "Sensitive result hidden by privacy settings."
    elif detail == "detailed":
        details = []
        # A skill-provided display is already the human-facing rendering of its
        # structured result. Do not append the internal payload over it.
        if result.data and not result.display_text:
            details.append(json.dumps(result.data, indent=2, default=str))
        if result.error:
            details.append(f"{result.error.code}: {result.error.message}")
        if details:
            display = display + "\n\n" + "\n".join(details)
    elif result.error and result.status in {
        ResultStatus.FAILED,
        ResultStatus.TIMED_OUT,
    }:
        display = display + f"\n\n{result.error.code}: {result.error.message}"
    speech = result.speech_text or result.summary
    if result.sensitive and not allow_sensitive_speech:
        speech = "The requested action has been processed. See the screen for details."
    if detail == "short" and result.status == ResultStatus.COMPLETED:
        speech = result.speech_text or "Done."
    response_type = (
        ResponseType.CLARIFICATION
        if result.error and result.error.code == "clarification_required"
        else ResponseType.SKILL
    )
    visual_artifact = (
        None if result.sensitive and hide_sensitive_display else result.visual_artifact
    )
    return Response(
        display_text=display,
        speech_text=speech,
        response_type=response_type,
        severity=severity,
        speak=True,
        sensitive=result.sensitive,
        media_directive=result.media_directive,
        visual_artifact=visual_artifact,
        items=_safe_response_items(result),
        citations=_citations_from_items(result, hide_sensitive=hide_sensitive_display),
    )


def _safe_response_items(result: SkillResult) -> tuple[RequestResultItem, ...]:
    safe_items = []
    for item in result.items or ():
        metadata = {
            key: value
            for key, value in item.metadata.items()
            if key.casefold() not in {"token", "secret", "password", "authorization", "api_key"}
        }
        raw_url = metadata.get("url")
        if isinstance(raw_url, str):
            parsed = urlsplit(raw_url)
            if parsed.scheme == "https" and parsed.hostname:
                metadata["url"] = urlunsplit(("https", parsed.hostname, parsed.path or "/", "", ""))
            else:
                metadata.pop("url", None)
        safe_items.append(item.model_copy(update={"metadata": metadata}))
    return tuple(safe_items)


def _citations_from_items(
    result: SkillResult, *, hide_sensitive: bool = False
) -> tuple[ResponseCitation, ...]:
    if result.sensitive and hide_sensitive:
        return ()
    citations = []
    seen = set()
    for item in result.items or ():
        local_reference = item.metadata.get("local_reference")
        if isinstance(local_reference, str) and local_reference.startswith("document:"):
            source_id = str(item.metadata.get("source_id") or item.item_id)
            if source_id in seen:
                continue
            seen.add(source_id)
            citations.append(
                ResponseCitation(
                    source_id=source_id,
                    title=item.title,
                    source_type=CitationSourceType.LOCAL_DOCUMENT,
                    local_reference=local_reference,
                    item_id=item.item_id,
                    excerpt_locator=(
                        str(item.metadata["locator"]) if item.metadata.get("locator") else None
                    ),
                    namespace="local_document",
                )
            )
            continue
        if result.sensitive:
            continue
        raw_url = item.metadata.get("url")
        if not isinstance(raw_url, str):
            continue
        parsed = urlsplit(raw_url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        sanitized = urlunsplit(("https", parsed.hostname, parsed.path or "/", "", ""))
        source_id = str(item.metadata.get("source_id") or item.item_id)
        if source_id in seen:
            continue
        seen.add(source_id)
        citations.append(
            ResponseCitation(
                source_id=source_id,
                title=str(item.metadata.get("source") or item.title),
                source_type=CitationSourceType.WEB,
                url=sanitized,
                item_id=item.item_id,
                excerpt_locator=(
                    str(item.metadata["excerpt_locator"])
                    if item.metadata.get("excerpt_locator")
                    else None
                ),
                namespace=str(item.metadata.get("namespace") or "skill_result"),
            )
        )
    return tuple(citations)


def _display_with_items(result: SkillResult) -> str:
    if not result.items:
        return result.summary
    lines = [result.summary]
    for index, item in enumerate(result.items, 1):
        line = f"{index}. {item.title}"
        if item.description:
            line += f" — {item.description}"
        lines.append(line)
    return "\n".join(lines)
