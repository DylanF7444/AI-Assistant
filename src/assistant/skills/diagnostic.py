from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from assistant.audio.capture import enumerate_devices
from assistant.contracts import ResultStatus, SkillCall, SkillResult
from assistant.diagnostics import hardware_report
from assistant.skills.base import Skill
from assistant.skills.builtin import manifest
from assistant.skills.registry import SkillRegistry


class DiagnosticInput(BaseModel):
    include_skills: bool = True


class DiagnosticOutput(BaseModel):
    hardware: dict[str, Any]
    providers: dict[str, Any]
    skills: list[dict[str, Any]]
    guidance: list[str]


class DiagnosticSkill(Skill):
    input_model = DiagnosticInput
    output_model = DiagnosticOutput

    def __init__(self, registry: SkillRegistry, providers: dict[str, Any]) -> None:
        self.registry = registry
        self.providers = providers
        self.manifest = manifest(
            skill_id="core.diagnostic",
            display_name="Diagnostic",
            description="Report local hardware, microphone, model, and skill health.",
            input_model=self.input_model,
            output_model=self.output_model,
            safe_to_retry=True,
        )

    async def execute(self, call: SkillCall, cancel_event: asyncio.Event) -> SkillResult:
        args = self.validate(call.arguments)
        provider_health = {}
        guidance = []
        for name, provider in self.providers.items():
            if provider is None:
                provider_health[name] = {
                    "healthy": False,
                    "error": "Not configured",
                }
                guidance.append(f"Configure a local {name} provider in Settings.")
                continue
            try:
                provider_health[name] = await provider.health()
            except Exception as exc:
                provider_health[name] = {"healthy": False, "error": str(exc)}
            if not provider_health[name].get("healthy"):
                guidance.append(
                    f"{name.upper()} is unavailable: "
                    f"{provider_health[name].get('error', 'check local model files')}."
                )
        devices = enumerate_devices()
        if not any(device.input_channels for device in devices):
            guidance.append(
                "No microphone is available. Connect or enable an input device."
            )
        if not any(device.output_channels for device in devices):
            guidance.append(
                "No audio output is available. Connect or enable an output device."
            )
        skill_health = []
        if args.include_skills:
            for item in self.registry.list():
                if item.skill_id == self.manifest.skill_id:
                    continue
                try:
                    skill = self.registry.get(item.skill_id)
                    value = await skill.health()
                except Exception as exc:
                    value = {"healthy": False, "error": str(exc)}
                skill_health.append({"skill_id": item.skill_id, **value})
        output = DiagnosticOutput(
            hardware=hardware_report(),
            providers=provider_health,
            skills=skill_health,
            guidance=guidance,
        )
        healthy = not guidance
        return SkillResult(
            status=ResultStatus.COMPLETED,
            summary=(
                "All configured components are healthy."
                if healthy
                else f"Diagnostics found {len(guidance)} item(s) needing attention."
            ),
            data=output.model_dump(),
        )
