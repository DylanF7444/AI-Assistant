import json

from assistant.contracts import Response, ResponseType, ResultStatus, SkillResult


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
        None
        if result.sensitive and hide_sensitive_display
        else result.visual_artifact
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
        items=result.items or (),
    )


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
