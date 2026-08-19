from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from assistant.command_center.contracts import (
    DeliveryPayload,
    DestinationBinding,
    DestinationDefinition,
    DestinationExecutorKind,
    SourceBinding,
    SourceDefinition,
    SourceExecutorKind,
)
from assistant.skills.registry import SkillRegistry

ArgumentBuilder = Callable[[BaseModel], dict[str, Any]]
DestinationArgumentBuilder = Callable[[BaseModel, DeliveryPayload | None], dict[str, Any]]


@dataclass(frozen=True)
class SourceContribution:
    definition: SourceDefinition
    configuration_model: type[BaseModel]
    build_arguments: ArgumentBuilder | None = None


@dataclass(frozen=True)
class DestinationContribution:
    definition: DestinationDefinition
    configuration_model: type[BaseModel]
    build_arguments: DestinationArgumentBuilder | None = None


class CommandCenterRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, SourceContribution] = {}
        self._destinations: dict[str, DestinationContribution] = {}

    def register_source(self, contribution: SourceContribution) -> None:
        definition = SourceDefinition.model_validate(contribution.definition)
        if definition.source_id in self._sources:
            raise ValueError(f"Command-center source already registered: {definition.source_id}")
        self._sources[definition.source_id] = SourceContribution(
            definition=definition,
            configuration_model=contribution.configuration_model,
            build_arguments=contribution.build_arguments,
        )

    def register_destination(self, contribution: DestinationContribution) -> None:
        definition = DestinationDefinition.model_validate(contribution.definition)
        if definition.destination_id in self._destinations:
            raise ValueError(
                f"Command-center destination already registered: {definition.destination_id}"
            )
        self._destinations[definition.destination_id] = DestinationContribution(
            definition=definition,
            configuration_model=contribution.configuration_model,
            build_arguments=contribution.build_arguments,
        )

    def source(self, source_id: str) -> SourceContribution:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise KeyError(f"Unknown command-center source: {source_id}") from exc

    def destination(self, destination_id: str) -> DestinationContribution:
        try:
            return self._destinations[destination_id]
        except KeyError as exc:
            raise KeyError(f"Unknown command-center destination: {destination_id}") from exc

    def source_definitions(self) -> tuple[SourceDefinition, ...]:
        return tuple(
            contribution.definition
            for contribution in sorted(
                self._sources.values(),
                key=lambda value: (
                    value.definition.provider_id,
                    value.definition.display_name.casefold(),
                ),
            )
        )

    def destination_definitions(self) -> tuple[DestinationDefinition, ...]:
        return tuple(
            contribution.definition
            for contribution in sorted(
                self._destinations.values(),
                key=lambda value: (
                    value.definition.provider_id,
                    value.definition.display_name.casefold(),
                ),
            )
        )

    def source_arguments(
        self,
        binding: SourceBinding,
        skills: SkillRegistry,
    ) -> dict[str, Any]:
        contribution = self.source(binding.source_id)
        definition = contribution.definition
        if definition.executor_kind != SourceExecutorKind.SKILL:
            raise ValueError("Provider-backed source does not compile to a skill call")
        arguments = self._arguments(
            contribution.configuration_model,
            contribution.build_arguments,
            binding.configuration,
        )
        assert definition.skill_id is not None
        if definition.operation is not None:
            arguments["operation"] = definition.operation
        skills.inspect(definition.skill_id).validate(arguments)
        return arguments

    def validate_source_configuration(self, binding: SourceBinding) -> None:
        contribution = self.source(binding.source_id)
        contribution.configuration_model.model_validate(binding.configuration)

    def validate_destination_configuration(self, binding: DestinationBinding) -> None:
        contribution = self.destination(binding.destination_id)
        contribution.configuration_model.model_validate(binding.configuration)

    def validate_destination_backing(
        self, binding: DestinationBinding, skills: SkillRegistry
    ) -> None:
        definition = self.destination(binding.destination_id).definition
        if definition.executor_kind != DestinationExecutorKind.SKILL:
            return
        assert definition.skill_id is not None
        skill = skills.inspect(definition.skill_id)
        if definition.operation is None:
            return
        operation_schema = skill.input_schema().get("properties", {}).get("operation", {})
        supported = operation_schema.get("enum")
        if isinstance(supported, list) and definition.operation not in supported:
            raise ValueError(
                f"Destination operation {definition.operation} is unavailable on {definition.skill_id}"
            )

    def destination_arguments(
        self,
        binding: DestinationBinding,
        skills: SkillRegistry,
        payload: DeliveryPayload | None = None,
    ) -> dict[str, Any]:
        contribution = self.destination(binding.destination_id)
        definition = contribution.definition
        if definition.executor_kind != DestinationExecutorKind.SKILL:
            raise ValueError("Local destination does not compile to a skill call")
        validated = contribution.configuration_model.model_validate(binding.configuration)
        arguments = (
            contribution.build_arguments(validated, payload)
            if contribution.build_arguments is not None
            else validated.model_dump(mode="json", exclude_none=True)
        )
        if not isinstance(arguments, dict):
            raise TypeError("Command-center argument builders must return a dictionary")
        arguments = dict(arguments)
        assert definition.operation is not None
        assert definition.skill_id is not None
        arguments["operation"] = definition.operation
        skills.inspect(definition.skill_id).validate(arguments)
        return arguments

    @staticmethod
    def _arguments(
        configuration_model: type[BaseModel],
        builder: ArgumentBuilder | None,
        configuration: dict,
    ) -> dict[str, Any]:
        validated = configuration_model.model_validate(configuration)
        values = (
            builder(validated)
            if builder is not None
            else validated.model_dump(mode="json", exclude_none=True)
        )
        if not isinstance(values, dict):
            raise TypeError("Command-center argument builders must return a dictionary")
        return dict(values)
