from __future__ import annotations

import json
from typing import Any

# This is trusted product context for conversational answers about Local Assistant.
# Keep implementation-specific capability details in live SkillManifest objects; this
# summary describes the stable application architecture and its knowledge boundaries.
APPLICATION_KNOWLEDGE: dict[str, Any] = {
    "identity": (
        "Local Assistant is an offline-first Windows desktop assistant. It accepts "
        "typed or spoken requests and can use local speech-to-text, a local planner, "
        "and local text-to-speech when those optional providers are configured."
    ),
    "request_lifecycle": [
        "Input is normalized and common commands are handled by deterministic routing.",
        (
            "Unmatched or open-ended requests go to a schema-constrained local planner. "
            "The planner interprets the goal using bounded session, device, permission, "
            "preference, knowledge, playbook, and skill context when relevant."
        ),
        (
            "The planner may answer conversationally, ask a clarification, reject an "
            "unsupported request, or select one or more currently enabled registered skills."
        ),
        (
            "A separate execution layer validates skill arguments, checks file policy and "
            "scoped permissions, requests an action-bound confirmation when required, "
            "executes the skill, and records routing, authorization, and outcomes locally."
        ),
        "Structured skill results are converted into on-screen and optional spoken responses.",
    ],
    "skills": (
        "Registered skill manifests are the live source of truth for action capabilities, "
        "input schemas, risk, and permissions. Model output cannot call Windows, the network, "
        "or the filesystem directly and cannot invent a skill or bypass the execution layer."
    ),
    "knowledge_and_memory": (
        "Recent conversation context is bounded and session-scoped. Optional durable "
        "preference memory, local knowledge bases, and user-authored playbooks may be "
        "retrieved when relevant. Knowledge excerpts are reference data; playbooks may guide "
        "planning but neither grants a capability or permission."
    ),
    "availability": (
        "Deterministic commands continue to work without a planner model. Open-ended "
        "conversation and ambiguous goal interpretation require a configured local planner. "
        "Network and external integrations work only when configured, permitted, and not "
        "blocked by offline mode."
    ),
    "media_presentation": (
        "The Assistant has a foreground-only in-app viewer for approved local or "
        "explicitly downloaded public HTTPS images, audio, and video. Local media uses "
        "filesystem permission; remote media uses network permission, is blocked by "
        "offline mode, and is downloaded into a bounded verified cache before display. "
        "Viewer controls affect only the active in-app presentation, not other apps."
    ),
    "visual_presentation": (
        "The Assistant can create local structured visualizations for bounded data: "
        "bar or line charts, flowcharts, timelines, and tables. Visual artifacts use "
        "typed contracts and a local results-window renderer, require no network or "
        "filesystem permission, and retain accessible text and speech fallbacks. The "
        "visualization skill accepts ordinary typed or dictated language without fixed "
        "punctuation, as well as precise typed specifications."
    ),
    "self_knowledge_limits": (
        "The assistant can explain this supplied application design and live context, but it "
        "does not have subjective self-awareness or introspective access to hidden model "
        "activations, private chain-of-thought, source code, or runtime state that was not "
        "included in its context. It should distinguish documented design from observed "
        "current state and say when a requested detail is unavailable."
    ),
}


def application_knowledge_prompt() -> str:
    """Return compact, stable product knowledge for planner prompts."""

    return json.dumps(APPLICATION_KNOWLEDGE, separators=(",", ":"))


def application_knowledge_answer(text: str) -> str:
    """Return a bounded code-owned fallback when a model misclassifies product help."""

    normalized = " ".join(text.casefold().split())
    if "permission" in normalized or "confirmation" in normalized:
        return (
            "Actions are limited to registered, schema-validated skills. A separate policy "
            "layer checks exact scoped permissions and file policy, and sensitive actions "
            "need a short-lived confirmation bound to the proposed call before execution."
        )
    if "offline" in normalized or "network" in normalized:
        return (
            "Core routing, storage, policy, local models, and local skills can work offline. "
            "External integrations require configuration and permission, and the global "
            "offline lock blocks their network access."
        )
    if any(word in normalized for word in ("memory", "context", "knowledge", "playbook")):
        return (
            "Recent conversation context is bounded and session-scoped. Optional preference "
            "memory, local knowledge bases, and user-authored playbooks may guide planning, "
            "but they cannot add a capability, permission, or execution authority."
        )
    if any(word in normalized for word in ("aware", "introspect", "chain of thought")):
        return str(APPLICATION_KNOWLEDGE["self_knowledge_limits"])
    if "what can you do" in normalized or "capabilit" in normalized or "skill" in normalized:
        return (
            "My live registered skill directory is the source of truth for actions, inputs, "
            "risk, and permissions. Deterministic commands work without a planner; optional "
            "local-model and network capabilities work only when configured and permitted."
        )
    return (
        "Typed or spoken input is normalized and common commands use deterministic routing. "
        "Other requests go to a schema-constrained local planner, which may answer, clarify, "
        "reject, or select registered skills. A separate execution layer then validates "
        "arguments, checks policy and permissions, obtains required confirmation, executes, "
        "and records the outcome locally."
    )
