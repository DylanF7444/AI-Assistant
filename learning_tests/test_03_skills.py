import asyncio

import pytest
from pydantic import ValidationError

from assistant.contracts import PermissionContext, ResultStatus, SkillCall
from assistant.skills.builtin import CalculatorSkill, TimerSkill


def make_call(skill_id: str, arguments: dict) -> SkillCall:
    return SkillCall(
        skill_id=skill_id,
        arguments=arguments,
        permission_context=PermissionContext(session_id="learning-session"),
    )


def test_skill_input_models_reject_invalid_arguments_before_execution():
    skill = TimerSkill()

    with pytest.raises(ValidationError):
        skill.validate({"seconds": -1, "label": "Invalid timer"})


@pytest.mark.asyncio
async def test_calculator_skill_executes_safe_arithmetic():
    skill = CalculatorSkill()
    call = make_call("core.calculator", {"expression": "(2 + 3) * 4"})

    result = await skill.execute(call, asyncio.Event())

    assert result.status == ResultStatus.COMPLETED
    assert result.data["value"] == 20
    skill.validate_result(result)


@pytest.mark.asyncio
async def test_calculator_does_not_execute_python_code():
    skill = CalculatorSkill()
    call = make_call(
        "core.calculator",
        {"expression": "__import__('os').getcwd()"},
    )

    with pytest.raises(ValueError, match="unsupported operations"):
        await skill.execute(call, asyncio.Event())
