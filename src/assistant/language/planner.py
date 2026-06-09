from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError, validate

from assistant.contracts import Intent, SkillManifest
from assistant.providers import LLMProvider


class SkillRetriever:
    SYNONYMS = {
        "calculate": {"calculator", "arithmetic", "math"},
        "compute": {"calculator", "arithmetic", "math"},
        "launch": {"open", "application"},
        "find": {"search", "file"},
        "copy": {"clipboard", "file"},
        "play": {"media", "music"},
    }

    def __init__(
        self,
        manifests: tuple[SkillManifest, ...]
        | Callable[[], tuple[SkillManifest, ...]],
    ) -> None:
        self.manifests = manifests

    def relevant(self, text: str, limit: int = 6) -> tuple[SkillManifest, ...]:
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        for word in tuple(words):
            words.update(self.SYNONYMS.get(word, ()))
        scored = []
        manifests = self.manifests() if callable(self.manifests) else self.manifests
        for manifest in manifests:
            haystack = set(re.findall(
                r"[a-z0-9]+",
                f"{manifest.skill_id} {manifest.display_name} {manifest.description}"
                .casefold(),
            ))
            score = len(words & haystack)
            scored.append((score, manifest.skill_id, manifest))
        return tuple(item[2] for item in sorted(scored, reverse=True)[:limit] if item[0])


class LocalPlanner:
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "disposition": {
                "type": "string",
                "enum": ["skill", "answer", "reject"],
            },
            "skill_id": {"type": "string"},
            "arguments": {"type": "object"},
            "answer": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "clarification": {"type": ["string", "null"]},
        },
        "required": [
            "disposition",
            "skill_id",
            "arguments",
            "answer",
            "confidence",
            "clarification",
        ],
        "additionalProperties": False,
    }

    def __init__(self, provider: LLMProvider, retriever: SkillRetriever) -> None:
        self.provider = provider
        self.retriever = retriever

    async def plan(
        self, text: str, recent_context: tuple[dict[str, str], ...] = ()
    ) -> Intent:
        if self._looks_like_gibberish(text):
            return self._rejection("I could not understand that request.")
        skills = self.retriever.relevant(text)
        compact = [
            {
                "id": item.skill_id,
                "description": item.description,
                "input_schema": item.input_schema,
            }
            for item in skills
        ]
        prompt = (
            "Classify the request as skill, answer, or reject. Use skill only when an "
            "available skill clearly performs the requested action. Use answer for normal "
            "conversation and questions that need no skill. Use reject for gibberish, "
            "empty/accidental activation, or an unintelligible request. Never invent a "
            "skill or create shell commands. For answer/reject, skill_id must be an empty "
            "string and arguments must be empty. Return only data matching the schema.\n"
            f"Skills: {json.dumps(compact, separators=(',', ':'))}\n"
            f"Recent session context: {json.dumps(recent_context, separators=(',', ':'))}\n"
            f"Request: {text}"
        )
        output = await self.provider.structured_completion(
            prompt, self.OUTPUT_SCHEMA, timeout_seconds=15
        )
        validate(output, self.OUTPUT_SCHEMA)
        if output["disposition"] in {"answer", "reject"}:
            return self._conversation_intent(output)
        manifest = next((item for item in skills if item.skill_id == output["skill_id"]), None)
        if manifest is None:
            answer = (output["answer"] or "").strip()
            if answer:
                return Intent(
                    name="conversation.answer",
                    arguments={"text": answer},
                    confidence=output["confidence"],
                    source="llm",
                )
            return await self._repair_conversational_response(text, recent_context)
        validate(output["arguments"], manifest.input_schema)
        return Intent(
            name=manifest.skill_id,
            arguments=output["arguments"],
            confidence=output["confidence"],
            source="llm",
            ambiguous=bool(output["clarification"]),
            ambiguity_reasons=(output["clarification"],)
            if output["clarification"]
            else (),
        )

    async def _repair_conversational_response(
        self, text: str, recent_context: tuple[dict[str, str], ...]
    ) -> Intent:
        prompt = (
            "Answer the user's request directly using general knowledge, or reject it if "
            "it is gibberish or unintelligible. No tools or skills are available. Never "
            "invent a command. For answer/reject, skill_id must be an empty string and "
            "arguments must be empty. Return only data matching the schema.\n"
            f"Recent session context: {json.dumps(recent_context, separators=(',', ':'))}\n"
            f"Request: {text}"
        )
        output = await self.provider.structured_completion(
            prompt, self.OUTPUT_SCHEMA, timeout_seconds=15
        )
        validate(output, self.OUTPUT_SCHEMA)
        if output["disposition"] not in {"answer", "reject"}:
            return self._rejection("I could not understand that request.")
        return self._conversation_intent(output)

    @staticmethod
    def _conversation_intent(output: dict[str, Any]) -> Intent:
        answer = (output["answer"] or "").strip()
        if not answer:
            raise ValidationError("Model returned an empty conversational response")
        return Intent(
            name=f"conversation.{output['disposition']}",
            arguments={"text": answer},
            confidence=output["confidence"],
            source="llm",
        )

    @staticmethod
    def _looks_like_gibberish(text: str) -> bool:
        words = re.findall(r"[a-z]+", text.casefold())
        if not words:
            return True
        if set(words) <= {"ah", "er", "hmm", "hm", "uh", "um"}:
            return True
        if len("".join(words)) > 3 and not re.search(r"[aeiouy]", "".join(words)):
            return True
        return any(re.search(r"(.)\1{5,}", word) for word in words)

    @staticmethod
    def _rejection(message: str) -> Intent:
        return Intent(
            name="conversation.reject",
            arguments={"text": message},
            confidence=1,
            source="classifier",
        )

    async def cancel(self) -> None:
        await self.provider.cancel()
