from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant.contracts import (
    BackgroundMode,
    PermissionScope,
    RequestResultItem,
    ResultStatus,
    RiskLevel,
    SkillCall,
    SkillResult,
)
from assistant.integrations.gmail import GmailAdapter, quote_id
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest

READ_OPERATIONS = {
    "get_profile",
    "list_labels",
    "list_messages",
    "get_message",
    "list_threads",
    "get_thread",
    "list_drafts",
    "get_draft",
}
SEND_OPERATIONS = {"send_message", "reply_message", "send_draft"}
MESSAGE_ID_OPERATIONS = {
    "get_message",
    "reply_message",
    "modify_message",
    "mark_read",
    "mark_unread",
    "archive_message",
    "move_to_inbox",
    "star_message",
    "unstar_message",
    "trash_message",
    "untrash_message",
}
THREAD_ID_OPERATIONS = {
    "get_thread",
    "modify_thread",
    "trash_thread",
    "untrash_thread",
}
DRAFT_ID_OPERATIONS = {"get_draft", "send_draft", "delete_draft"}


class GmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal[
        "get_profile",
        "list_labels",
        "list_messages",
        "get_message",
        "list_threads",
        "get_thread",
        "list_drafts",
        "get_draft",
        "create_draft",
        "delete_draft",
        "send_draft",
        "send_message",
        "reply_message",
        "modify_message",
        "mark_read",
        "mark_unread",
        "archive_message",
        "move_to_inbox",
        "star_message",
        "unstar_message",
        "trash_message",
        "untrash_message",
        "modify_thread",
        "trash_thread",
        "untrash_thread",
    ]
    message_id: str | None = Field(default=None, min_length=1, max_length=1024)
    thread_id: str | None = Field(default=None, min_length=1, max_length=1024)
    draft_id: str | None = Field(default=None, min_length=1, max_length=1024)
    query: str | None = Field(default=None, max_length=2000)
    label_ids: tuple[str, ...] = Field(default=(), max_length=50)
    add_label_ids: tuple[str, ...] = Field(default=(), max_length=50)
    remove_label_ids: tuple[str, ...] = Field(default=(), max_length=50)
    include_spam_trash: bool = False
    max_results: int = Field(default=20, ge=1, le=50)
    page_token: str | None = Field(default=None, max_length=2048)
    to: tuple[str, ...] = Field(default=(), max_length=50)
    cc: tuple[str, ...] = Field(default=(), max_length=50)
    bcc: tuple[str, ...] = Field(default=(), max_length=50)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=100_000)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def _addresses_are_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _name, address = parseaddr(value)
            if (
                not address
                or "@" not in address
                or len(value) > 500
                or any(character in value for character in "\r\n")
            ):
                raise ValueError(f"Invalid email address: {value}")
        return values

    @field_validator("subject")
    @classmethod
    def _subject_cannot_inject_headers(cls, value: str | None) -> str | None:
        if value is not None and any(character in value for character in "\r\n"):
            raise ValueError("Email subject cannot contain line breaks")
        return value

    @field_validator("label_ids", "add_label_ids", "remove_label_ids")
    @classmethod
    def _labels_are_unique_and_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 1024 for value in values):
            raise ValueError("Label IDs must be non-empty and at most 1024 characters")
        if len(values) != len(set(values)):
            raise ValueError("Label IDs must be unique")
        return values

    @model_validator(mode="after")
    def _operation_has_required_fields(self) -> GmailInput:
        if self.operation in MESSAGE_ID_OPERATIONS and not self.message_id:
            raise ValueError(f"{self.operation} requires message_id")
        if self.operation in THREAD_ID_OPERATIONS and not self.thread_id:
            raise ValueError(f"{self.operation} requires thread_id")
        if self.operation in DRAFT_ID_OPERATIONS and not self.draft_id:
            raise ValueError(f"{self.operation} requires draft_id")
        if self.operation in {"send_message", "create_draft"}:
            if not (self.to or self.cc or self.bcc):
                raise ValueError(f"{self.operation} requires at least one recipient")
            if self.subject is None or self.body is None:
                raise ValueError(f"{self.operation} requires subject and body")
        if self.operation == "reply_message" and self.body is None:
            raise ValueError("reply_message requires body")
        if self.operation in {"modify_message", "modify_thread"}:
            if not (self.add_label_ids or self.remove_label_ids):
                raise ValueError(
                    f"{self.operation} requires add_label_ids or remove_label_ids"
                )
            overlap = set(self.add_label_ids) & set(self.remove_label_ids)
            if overlap:
                raise ValueError("The same label cannot be added and removed")
        return self


class GmailOutput(BaseModel):
    operation: str
    resources: list[dict[str, Any]] = Field(default_factory=list)
    next_page_token: str | None = None
    result_size_estimate: int | None = None


class GmailSkill(Skill):
    input_model = GmailInput
    output_model = GmailOutput

    def __init__(
        self,
        adapter: GmailAdapter,
        account: Callable[[], str | None] | None = None,
    ) -> None:
        self.adapter = adapter
        self._account = account or (lambda: None)
        self.manifest = manifest(
            skill_id="integration.gmail",
            display_name="Gmail",
            description=(
                "Read and search Gmail messages and threads, manage labels and inbox state, "
                "create drafts, and send or reply to email. Permanent deletion and "
                "attachments are intentionally excluded."
            ),
            input_model=self.input_model,
            output_model=self.output_model,
            permissions=(
                PermissionScope(
                    capability="integration.read",
                    targets=(),
                    reason="Read approved Gmail messages, threads, drafts, labels, and profile data.",
                ),
                PermissionScope(
                    capability="integration.write",
                    targets=(),
                    reason="Change approved Gmail drafts, labels, and mailbox state.",
                ),
                PermissionScope(
                    capability="messaging",
                    targets=(),
                    reason="Send approved email messages and replies.",
                ),
                PermissionScope(
                    capability="network",
                    targets=("https://gmail.googleapis.com", "https://oauth2.googleapis.com"),
                    reason="Connect to Gmail and refresh its OAuth token.",
                ),
            ),
            risk=RiskLevel.HIGH,
            safe_to_retry=False,
            background_mode=BackgroundMode.CAPABILITY_GATED,
        ).model_copy(update={"entry_point": f"{__name__}:GmailSkill"})

    def risk_for(self, call: SkillCall) -> RiskLevel:
        operation = self.validate(call.arguments).operation
        if operation in READ_OPERATIONS:
            return RiskLevel.LOW
        if operation in SEND_OPERATIONS or operation == "delete_draft":
            return RiskLevel.HIGH
        return RiskLevel.MEDIUM

    def required_permissions(self, call: SkillCall):
        args = self.validate(call.arguments)
        if args.operation in SEND_OPERATIONS:
            primary = PermissionScope(
                capability="messaging",
                targets=(_send_target(args),),
                reason=f"Allow Gmail operation {args.operation} for this exact send target.",
            )
        else:
            primary = PermissionScope(
                capability=(
                    "integration.read"
                    if args.operation in READ_OPERATIONS
                    else "integration.write"
                ),
                targets=(_permission_target(args),),
                reason=f"Allow Gmail operation {args.operation} on this exact target.",
            )
        return (
            primary,
            PermissionScope(
                capability="network",
                targets=("https://gmail.googleapis.com", "https://oauth2.googleapis.com"),
                reason="Connect to Gmail and refresh its OAuth token.",
            ),
        )

    async def preview(self, call: SkillCall) -> str:
        args = self.validate(call.arguments)
        target = _send_target(args) if args.operation in SEND_OPERATIONS else _permission_target(args)
        return f"Gmail: {args.operation} on {target}"

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        if cancel_event.is_set():
            return SkillResult(status=ResultStatus.CANCELLED, summary="Gmail action cancelled.")
        payload = await self._execute(args, cancel_event)
        resources = _resources(args.operation, payload)
        output = GmailOutput(
            operation=args.operation,
            resources=resources,
            next_page_token=_string(payload.get("nextPageToken")),
            result_size_estimate=_integer(payload.get("resultSizeEstimate")),
        )
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=_summary(args.operation, len(resources)),
            display_text=_display(args.operation, resources),
            data=output.model_dump(mode="json"),
            items=tuple(_result_item(item, index) for index, item in enumerate(resources, 1))
            or None,
            sensitive=True,
        )

    async def _execute(
        self,
        args: GmailInput,
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        operation = args.operation
        message_id = quote_id(args.message_id) if args.message_id else None
        thread_id = quote_id(args.thread_id) if args.thread_id else None
        draft_id = quote_id(args.draft_id) if args.draft_id else None
        root = "/users/me"

        if operation == "get_profile":
            return await self.adapter.request("GET", f"{root}/profile")
        if operation == "list_labels":
            return await self.adapter.request("GET", f"{root}/labels")
        if operation == "list_messages":
            payload = await self.adapter.request(
                "GET",
                f"{root}/messages",
                query={
                    "q": args.query,
                    "labelIds": args.label_ids,
                    "includeSpamTrash": args.include_spam_trash,
                    "maxResults": args.max_results,
                    "pageToken": args.page_token,
                },
            )
            payload["messages"] = await self._hydrate_messages(
                payload.get("messages"), cancel_event
            )
            return payload
        if operation == "get_message":
            payload = await self.adapter.request(
                "GET", f"{root}/messages/{message_id}", query={"format": "full"}
            )
            return _normalize_message(payload)
        if operation == "list_threads":
            payload = await self.adapter.request(
                "GET",
                f"{root}/threads",
                query={
                    "q": args.query,
                    "labelIds": args.label_ids,
                    "includeSpamTrash": args.include_spam_trash,
                    "maxResults": args.max_results,
                    "pageToken": args.page_token,
                },
            )
            payload["threads"] = await self._hydrate_threads(
                payload.get("threads"), cancel_event
            )
            return payload
        if operation == "get_thread":
            payload = await self.adapter.request(
                "GET", f"{root}/threads/{thread_id}", query={"format": "full"}
            )
            return _normalize_thread(payload)
        if operation == "list_drafts":
            payload = await self.adapter.request(
                "GET",
                f"{root}/drafts",
                query={
                    "q": args.query,
                    "includeSpamTrash": args.include_spam_trash,
                    "maxResults": args.max_results,
                    "pageToken": args.page_token,
                },
            )
            payload["drafts"] = await self._hydrate_drafts(payload.get("drafts"), cancel_event)
            return payload
        if operation == "get_draft":
            payload = await self.adapter.request(
                "GET", f"{root}/drafts/{draft_id}", query={"format": "full"}
            )
            return _normalize_draft(payload)
        if operation == "create_draft":
            sender = self._sender_address()
            return await self.adapter.request(
                "POST",
                f"{root}/drafts",
                body={"message": {"raw": _encoded_message(args, from_address=sender)}},
            )
        if operation == "delete_draft":
            return await self.adapter.request("DELETE", f"{root}/drafts/{draft_id}")
        if operation == "send_draft":
            return await self.adapter.request(
                "POST", f"{root}/drafts/send", body={"id": args.draft_id}
            )
        if operation == "send_message":
            sender = self._sender_address()
            return await self.adapter.request(
                "POST",
                f"{root}/messages/send",
                body={"raw": _encoded_message(args, from_address=sender)},
            )
        if operation == "reply_message":
            original = await self.adapter.request(
                "GET", f"{root}/messages/{message_id}", query={"format": "metadata"}
            )
            reply = _reply_arguments(args, original)
            original_headers = _headers(original)
            message_identifier = original_headers.get("message-id")
            sender = self._sender_address()
            return await self.adapter.request(
                "POST",
                f"{root}/messages/send",
                body={
                    "raw": _encoded_message(
                        reply,
                        from_address=sender,
                        in_reply_to=message_identifier,
                        references=message_identifier,
                    ),
                    "threadId": original.get("threadId"),
                },
            )
        if operation in {"modify_message", "modify_thread"}:
            resource = "messages" if operation == "modify_message" else "threads"
            resource_id = message_id if operation == "modify_message" else thread_id
            return await self.adapter.request(
                "POST",
                f"{root}/{resource}/{resource_id}/modify",
                body={
                    "addLabelIds": list(args.add_label_ids),
                    "removeLabelIds": list(args.remove_label_ids),
                },
            )

        label_changes = {
            "mark_read": ((), ("UNREAD",)),
            "mark_unread": (("UNREAD",), ()),
            "archive_message": ((), ("INBOX",)),
            "move_to_inbox": (("INBOX",), ()),
            "star_message": (("STARRED",), ()),
            "unstar_message": ((), ("STARRED",)),
        }
        if operation in label_changes:
            add, remove = label_changes[operation]
            return await self.adapter.request(
                "POST",
                f"{root}/messages/{message_id}/modify",
                body={"addLabelIds": list(add), "removeLabelIds": list(remove)},
            )
        if operation in {"trash_message", "untrash_message"}:
            action = "trash" if operation == "trash_message" else "untrash"
            return await self.adapter.request(
                "POST", f"{root}/messages/{message_id}/{action}"
            )
        if operation in {"trash_thread", "untrash_thread"}:
            action = "trash" if operation == "trash_thread" else "untrash"
            return await self.adapter.request(
                "POST", f"{root}/threads/{thread_id}/{action}"
            )
        raise ValueError(f"Unsupported Gmail operation: {operation}")

    def _sender_address(self) -> str:
        account = self._account()
        if account:
            return account
        raise ValueError(
            "Gmail did not identify the connected sender account. Reconnect Gmail "
            "from Extension Hub before drafting or sending email."
        )

    async def _hydrate_messages(
        self,
        raw_items: Any,
        cancel_event: asyncio.Event,
    ) -> list[dict[str, Any]]:
        items = [item for item in raw_items or [] if isinstance(item, dict) and item.get("id")]
        semaphore = asyncio.Semaphore(5)

        async def hydrate(item: dict[str, Any]) -> dict[str, Any]:
            if cancel_event.is_set():
                return _normalize_message(item)
            async with semaphore:
                payload = await self.adapter.request(
                    "GET",
                    f"/users/me/messages/{quote_id(str(item['id']))}",
                    query={
                        "format": "metadata",
                        "metadataHeaders": ["From", "To", "Subject", "Date", "Message-ID"],
                    },
                )
            return _normalize_message(payload)

        return list(await asyncio.gather(*(hydrate(item) for item in items)))

    async def _hydrate_threads(
        self,
        raw_items: Any,
        cancel_event: asyncio.Event,
    ) -> list[dict[str, Any]]:
        items = [item for item in raw_items or [] if isinstance(item, dict) and item.get("id")]
        semaphore = asyncio.Semaphore(5)

        async def hydrate(item: dict[str, Any]) -> dict[str, Any]:
            if cancel_event.is_set():
                return _normalize_thread(item)
            async with semaphore:
                payload = await self.adapter.request(
                    "GET",
                    f"/users/me/threads/{quote_id(str(item['id']))}",
                    query={
                        "format": "metadata",
                        "metadataHeaders": ["From", "To", "Subject", "Date", "Message-ID"],
                    },
                )
            return _normalize_thread(payload)

        return list(await asyncio.gather(*(hydrate(item) for item in items)))

    async def _hydrate_drafts(
        self,
        raw_items: Any,
        cancel_event: asyncio.Event,
    ) -> list[dict[str, Any]]:
        items = [item for item in raw_items or [] if isinstance(item, dict) and item.get("id")]
        semaphore = asyncio.Semaphore(5)

        async def hydrate(item: dict[str, Any]) -> dict[str, Any]:
            if cancel_event.is_set():
                return _normalize_draft(item)
            async with semaphore:
                payload = await self.adapter.request(
                    "GET",
                    f"/users/me/drafts/{quote_id(str(item['id']))}",
                    query={
                        "format": "metadata",
                        "metadataHeaders": ["From", "To", "Subject", "Date", "Message-ID"],
                    },
                )
            return _normalize_draft(payload)

        return list(await asyncio.gather(*(hydrate(item) for item in items)))


def _permission_target(args: GmailInput) -> str:
    value = args.message_id or args.thread_id or args.draft_id or args.query or "*"
    kind = (
        "message"
        if args.message_id
        else "thread"
        if args.thread_id
        else "draft"
        if args.draft_id
        else "query"
    )
    return f"gmail:{kind}:{value}"


def _send_target(args: GmailInput) -> str:
    recipients = sorted(set((*args.to, *args.cc, *args.bcc)), key=str.casefold)
    if recipients:
        return "gmail:recipients:" + ",".join(recipients)
    if args.message_id:
        return f"gmail:reply:{args.message_id}"
    return f"gmail:draft:{args.draft_id or '*'}"


def _encoded_message(
    args: GmailInput,
    *,
    from_address: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    message = EmailMessage()
    if from_address:
        message["From"] = from_address
    if args.to:
        message["To"] = ", ".join(args.to)
    if args.cc:
        message["Cc"] = ", ".join(args.cc)
    if args.bcc:
        message["Bcc"] = ", ".join(args.bcc)
    message["Subject"] = args.subject or ""
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(args.body or "")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def _reply_arguments(args: GmailInput, original: dict[str, Any]) -> GmailInput:
    headers = _headers(original)
    recipients = args.to or tuple(
        value for value in (headers.get("reply-to") or headers.get("from"),) if value
    )
    if not recipients:
        raise ValueError("The original message does not identify a reply recipient")
    subject = args.subject or headers.get("subject") or ""
    if subject and not re.match(r"^re\s*:", subject, flags=re.IGNORECASE):
        subject = f"Re: {subject}"
    reply = args.model_copy(update={"to": recipients, "subject": subject})
    return reply


def _resources(operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    key = {
        "list_labels": "labels",
        "list_messages": "messages",
        "list_threads": "threads",
        "list_drafts": "drafts",
    }.get(operation)
    if key:
        values = payload.get(key)
        return [item for item in values or [] if isinstance(item, dict)]
    return [payload] if payload else []


def _normalize_message(payload: dict[str, Any]) -> dict[str, Any]:
    headers = _headers(payload)
    normalized: dict[str, Any] = {
        key: payload[key]
        for key in ("id", "threadId", "labelIds", "snippet", "historyId", "internalDate")
        if key in payload
    }
    if headers:
        normalized["headers"] = headers
    body = _message_body(payload.get("payload"))
    if body:
        normalized["body"] = body
    attachments = _attachments(payload.get("payload"))
    if attachments:
        normalized["attachments"] = attachments
    return normalized


def _normalize_thread(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        key: payload[key]
        for key in ("id", "historyId", "snippet")
        if key in payload
    }
    messages = payload.get("messages")
    if isinstance(messages, list):
        normalized["messages"] = [
            _normalize_message(item) for item in messages if isinstance(item, dict)
        ][:50]
    return normalized


def _normalize_draft(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {"id": payload["id"]} if "id" in payload else {}
    message = payload.get("message")
    if isinstance(message, dict):
        normalized["message"] = _normalize_message(message)
    return normalized


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    message_payload = payload.get("payload")
    if not isinstance(message_payload, dict):
        return {}
    values = message_payload.get("headers")
    if not isinstance(values, list):
        return {}
    allowed = {"from", "to", "cc", "bcc", "reply-to", "subject", "date", "message-id"}
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.casefold() in allowed:
            result[name.casefold()] = value[:4000]
    return result


def _message_body(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates: list[tuple[str, str]] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str) and mime_type in {"text/plain", "text/html"}:
            candidates.append((mime_type, data))
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    if not candidates:
        return None
    mime_type, encoded = next(
        (item for item in candidates if item[0] == "text/plain"), candidates[0]
    )
    try:
        padding = "=" * (-len(encoded) % 4)
        text = base64.urlsafe_b64decode(encoded + padding).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return None
    if mime_type == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    return text[:16_000]


def _attachments(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result: list[dict[str, Any]] = []

    def visit(part: dict[str, Any]) -> None:
        filename = part.get("filename")
        body = part.get("body")
        if isinstance(filename, str) and filename and isinstance(body, dict):
            result.append(
                {
                    "filename": filename[:500],
                    "mime_type": str(part.get("mimeType") or "application/octet-stream")[:200],
                    "size": _integer(body.get("size")),
                    "attachment_id": _string(body.get("attachmentId")),
                }
            )
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    return result[:50]


def _result_item(item: dict[str, Any], index: int) -> RequestResultItem:
    message = item.get("message") if isinstance(item.get("message"), dict) else item
    if isinstance(item.get("messages"), list) and item["messages"]:
        last = item["messages"][-1]
        if isinstance(last, dict):
            message = last
    headers = message.get("headers", {}) if isinstance(message, dict) else {}
    subject = headers.get("subject") if isinstance(headers, dict) else None
    title = str(subject or item.get("name") or item.get("emailAddress") or item.get("id") or "Gmail item")
    description = _message_description(message if isinstance(message, dict) else item)
    return RequestResultItem(
        item_id=f"gmail-{index}-{_short_id(item)}",
        title=title[:500],
        description=description,
        value={
            "message_id": message.get("id") if isinstance(message, dict) else None,
            "thread_id": item.get("id") if "messages" in item else item.get("threadId"),
            "draft_id": item.get("id") if "message" in item else None,
            "resource": item,
        },
        kind="email",
        sensitive=True,
    )


def _message_description(item: dict[str, Any]) -> str | None:
    headers = item.get("headers")
    if isinstance(headers, dict):
        values = [headers.get("from"), headers.get("date")]
        heading = " • ".join(str(value)[:1000] for value in values if value)
    else:
        heading = ""
    snippet = item.get("snippet")
    pieces = [heading, str(snippet)[:2000] if snippet else ""]
    return "\n".join(value for value in pieces if value) or None


def _summary(operation: str, count: int) -> str:
    if operation.startswith("list_"):
        return f"I found {count} Gmail item(s)." if count else "I couldn't find any Gmail items."
    if operation in SEND_OPERATIONS:
        return "Gmail sent the email."
    if operation == "create_draft":
        return "Gmail created the draft."
    return f"Gmail {operation} completed."


def _display(operation: str, resources: list[dict[str, Any]]) -> str:
    if operation in SEND_OPERATIONS:
        return "The email was sent through Gmail."
    if operation == "create_draft":
        return "The email draft was saved in Gmail."
    if not resources:
        return "No matching Gmail items were found." if operation.startswith("list_") else f"Gmail {operation} completed."
    lines: list[str] = []
    for item in resources[:50]:
        result = _result_item(item, len(lines) + 1)
        lines.append(f"- {result.title}" + (f"\n  {result.description}" if result.description else ""))
    return "\n".join(lines)


def _short_id(item: dict[str, Any]) -> str:
    raw = str(item.get("id") or item.get("emailAddress") or "item").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
