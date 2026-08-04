from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from assistant.bootstrap import build_container
from assistant.config import Settings
from assistant.contracts import PermissionContext, ResultStatus, SkillCall, SkillResult
from assistant.logic.contracts import (
    ChoiceFilter,
    ChoiceOption,
    ChoiceOptionDraft,
    CompletionPolicy,
    LogicCallDraft,
    LogicNotificationDraft,
    LogicPredicate,
    LogicSelection,
    LogicSelectionDraft,
    LogicSelectionResultDraft,
    LogicWorkflowDraft,
)
from assistant.logic.evaluator import choose_option, evaluate_predicate, render_template
from assistant.logic.store import LogicStateStore
from assistant.scheduling.contracts import (
    OneTimeTrigger,
    RunPolicy,
    ScheduleRunState,
    ScheduleStatus,
)
from assistant.skills.logic import ChanceSkill, ChooseSkill
from assistant.storage.database import Database


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeNotificationAdapter:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    async def show(self, title: str, message: str) -> None:
        self.shown.append((title, message))


def _call(arguments: dict, *, key: str = "logic-test") -> SkillCall:
    return SkillCall(
        skill_id="core.choose",
        arguments=arguments,
        permission_context=PermissionContext(session_id="logic-test"),
        idempotency_key=key,
    )


def _options() -> tuple[ChoiceOption, ...]:
    return (
        ChoiceOption(option_id="red", label="Red", value="red", metadata={"group": "warm"}),
        ChoiceOption(
            option_id="green", label="Green", value="green", metadata={"group": "cool"}
        ),
        ChoiceOption(option_id="blue", label="Blue", value="blue", metadata={"group": "cool"}),
    )


def test_seeded_selection_is_reproducible_and_respects_history_and_filters():
    selection = LogicSelection(
        options=_options(),
        seed="stable-demo",
        avoid_recent=1,
        filters=(ChoiceFilter(path="metadata.group", operator="equals", value="cool"),),
    )
    first = choose_option(selection, selection_key="run-1")
    repeated = choose_option(selection, selection_key="run-1")
    second = choose_option(selection, selection_key="run-2", recent_ids=(first.option_id,))

    assert repeated == first
    assert first.metadata["group"] == "cool"
    assert second.metadata["group"] == "cool"
    assert second.option_id != first.option_id


def test_round_robin_and_least_recent_are_deterministic():
    round_robin = LogicSelection(options=_options(), strategy="round_robin")
    assert choose_option(round_robin, selection_key="x").option_id == "red"
    assert (
        choose_option(round_robin, selection_key="y", recent_ids=("red",)).option_id
        == "green"
    )

    least_recent = LogicSelection(options=_options(), strategy="least_recent", seed="stable")
    selected = choose_option(
        least_recent,
        selection_key="z",
        recent_ids=("red", "green"),
    )
    assert selected.option_id == "blue"


def test_weighted_selection_is_deterministic_and_honors_relative_weights():
    selection = LogicSelection(
        options=(
            ChoiceOption(option_id="rare", label="Rare", value="rare", weight=1),
            ChoiceOption(option_id="common", label="Common", value="common", weight=3),
        ),
        seed="weighted-demo",
    )
    picks = [
        choose_option(selection, selection_key=f"run-{index}").option_id
        for index in range(400)
    ]

    assert picks == [
        choose_option(selection, selection_key=f"run-{index}").option_id
        for index in range(400)
    ]
    assert 270 <= picks.count("common") <= 330


def test_unseeded_random_selection_uses_fresh_entropy(monkeypatch):
    values = iter((0, 2**63))
    monkeypatch.setattr(
        "assistant.logic.evaluator.secrets.randbits",
        lambda _bits: next(values),
    )
    selection = LogicSelection(
        options=(
            ChoiceOption(option_id="first", label="First", value="first"),
            ChoiceOption(option_id="second", label="Second", value="second"),
        )
    )

    assert choose_option(selection, selection_key="one").option_id == "first"
    assert choose_option(selection, selection_key="two").option_id == "second"


def test_predicates_and_templates_use_only_typed_result_fields():
    result = SkillResult(
        status=ResultStatus.COMPLETED,
        summary="Rain is expected",
        data={"weather": {"raining": True}},
    )
    assert evaluate_predicate(
        LogicPredicate(
            reference="check.data.weather.raining",
            operator="equals",
            value=True,
        ),
        result,
    )
    assert render_template(
        "Status: {check.status}; {check.summary}",
        choice=None,
        check=result,
        max_length=500,
    ) == "Status: completed; Rain is expected"
    with pytest.raises(ValidationError, match="Unsupported notification template"):
        LogicNotificationDraft(title="Weather", message="{check.data.secret}")


@pytest.mark.asyncio
async def test_choose_skill_is_retry_stable_and_keeps_bounded_history(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'logic.db'}")
    database.migrate()
    state = LogicStateStore(database)
    skill = ChooseSkill(state)
    arguments = {
        "items": ["red", "green", "blue"],
        "seed": "demo",
        "history_key": "colors",
        "avoid_recent": 1,
    }

    first = await skill.execute(_call(arguments, key="same"), asyncio.Event())
    repeated = await skill.execute(_call(arguments, key="same"), asyncio.Event())
    second = await skill.execute(_call(arguments, key="next"), asyncio.Event())

    assert first.data["selected_id"] == repeated.data["selected_id"]
    assert second.data["selected_id"] != first.data["selected_id"]
    assert len(state.recent_choices("local-user:colors")) == 2


@pytest.mark.asyncio
async def test_stateful_choose_strategy_gets_automatic_persistent_history(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'automatic-history.db'}")
    database.migrate()
    state = LogicStateStore(database)
    skill = ChooseSkill(state)
    arguments = {
        "items": ["red", "green", "blue"],
        "strategy": "round_robin",
    }

    first = await skill.execute(_call(arguments, key="first-run"), asyncio.Event())
    second = await skill.execute(_call(arguments, key="second-run"), asyncio.Event())

    assert first.data["selected_label"] == "red"
    assert second.data["selected_label"] == "green"
    assert first.data["history_key"].startswith("auto-")
    assert second.data["history_key"] == first.data["history_key"]


@pytest.mark.asyncio
async def test_chance_skill_handles_trigger_and_noop_without_an_external_condition(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'chance.db'}")
    database.migrate()
    notifications = FakeNotificationAdapter()
    skill = ChanceSkill(LogicStateStore(database), notifications)

    triggered = await skill.execute(
        SkillCall(
            skill_id="core.chance",
            arguments={
                "probability_percent": 100,
                "title": "Surprise",
                "message": "Surprise!",
            },
            permission_context=PermissionContext(session_id="chance"),
            idempotency_key="trigger",
        ),
        asyncio.Event(),
    )
    skipped = await skill.execute(
        SkillCall(
            skill_id="core.chance",
            arguments={"probability_percent": 0},
            permission_context=PermissionContext(session_id="chance"),
            idempotency_key="skip",
        ),
        asyncio.Event(),
    )

    assert triggered.data["triggered"] is True
    assert skipped.data["triggered"] is False
    assert notifications.shown == [("Surprise", "Surprise!")]


@pytest.mark.asyncio
async def test_reported_requests_reach_execution_without_planner_guesswork(tmp_path):
    container = build_container(Settings(profile="testing", data_dir=tmp_path))
    advanced_choice = (
        "Choose from red, green, or blue using least-recent selection, avoid the last "
        "choice, and remember the history as colors."
    )

    first_response, first_outcome = await container.orchestrator.handle_text(
        advanced_choice,
        session_id="reported-requests",
    )
    second_response, second_outcome = await container.orchestrator.handle_text(
        advanced_choice,
        session_id="reported-requests",
    )
    _delayed_response, delayed_outcome = await container.orchestrator.handle_text(
        "In one minute, choose tea, coffee, or water at random and notify me with the "
        "choice. Stop after one run.",
        session_id="reported-requests",
    )
    _conditional_response, conditional_outcome = await container.orchestrator.handle_text(
        "Every five minutes, calculate 6 * 7. If the result is greater than 40, notify "
        "me with the result and then stop. Maximum three runs.",
        session_id="reported-requests",
    )
    _chance_response, chance_outcome = await container.orchestrator.handle_text(
        "give me a 20 percent chance of a Surprise notification and an 80 percent chance "
        "of doing nothing.",
        session_id="reported-requests",
    )

    assert first_outcome.call is not None
    assert first_outcome.call.skill_id == "core.choose"
    assert second_outcome.call is not None
    assert second_outcome.call.skill_id == "core.choose"
    assert first_response.display_text != second_response.display_text
    assert delayed_outcome.call is not None
    assert delayed_outcome.call.skill_id == "core.schedule"
    assert delayed_outcome.confirmation is not None
    assert conditional_outcome.call is not None
    assert conditional_outcome.call.skill_id == "core.schedule"
    assert conditional_outcome.confirmation is not None
    assert chance_outcome.call is not None
    assert chance_outcome.call.skill_id == "core.chance"
    assert chance_outcome.permission_requests[0].scope.capability == "notification.create"


def test_logic_contracts_reject_unbounded_or_incoherent_requests():
    with pytest.raises(ValidationError, match="condition requires a check"):
        LogicWorkflowDraft(
            when=LogicPredicate(reference="check.status", operator="equals", value="completed"),
            action=LogicSelectionResultDraft(),
        )
    with pytest.raises(ValidationError):
        CompletionPolicy(max_runs=0)
    with pytest.raises(ValidationError, match="at least two options"):
        LogicSelectionDraft(
            options=(ChoiceOptionDraft(option_id="only", label="Only", value="only"),)
        )
    with pytest.raises(ValidationError, match="stop_after_condition requires a condition"):
        LogicWorkflowDraft(
            action=LogicNotificationDraft(title="Invalid", message="Invalid"),
            completion=CompletionPolicy(stop_after_condition=True),
        )
    with pytest.raises(ValidationError):
        LogicSelectionDraft(
            options=tuple(
                ChoiceOptionDraft(option_id=f"item-{index}", label="Item", value=index)
                for index in range(101)
            )
        )


@pytest.mark.asyncio
async def test_durable_logic_workflow_checks_notifies_and_stops(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        background_actions_enabled=True,
    )
    container = build_container(settings)
    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Notify when calculation is above forty",
        trigger=OneTimeTrigger(run_at=datetime.now(UTC) - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            check=LogicCallDraft(skill_id="core.calculator", arguments={"expression": "6 * 7"}),
            when=LogicPredicate(
                reference="check.data.value",
                operator="greater_than",
                value=40,
            ),
            action=LogicNotificationDraft(
                title="Calculation ready",
                message="The check completed: {check.summary}",
            ),
            completion=CompletionPolicy(stop_after_condition=True, max_runs=5),
        ),
    )

    assert await container.scheduler.run_once() == 1
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    stored = container.schedule_store.get(schedule.schedule_id)
    notification = container.notification_center.history()[-1]

    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result["data"]["condition_met"] is True
    assert run.result["data"]["workflow"]["stopped"] is True
    assert stored.status == ScheduleStatus.COMPLETED
    assert notification.title == "Calculation ready"
    assert notification.message == "The check completed: The result is 42."


@pytest.mark.asyncio
async def test_condition_false_performs_no_action_and_max_runs_stops(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        background_actions_enabled=True,
    )
    container = build_container(settings)
    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Do not notify",
        trigger=OneTimeTrigger(run_at=datetime.now(UTC) - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            check=LogicCallDraft(skill_id="core.calculator", arguments={"expression": "1"}),
            when=LogicPredicate(
                reference="check.data.value",
                operator="greater_than",
                value=40,
            ),
            action=LogicNotificationDraft(title="Should not appear", message="No"),
            completion=CompletionPolicy(max_runs=1),
        ),
    )

    assert await container.scheduler.run_once() == 1
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    assert run.result["data"]["action_executed"] is False
    assert container.notification_center.history() == ()
    assert container.schedule_store.get(schedule.schedule_id).status == ScheduleStatus.COMPLETED


@pytest.mark.asyncio
async def test_random_action_branch_freezes_all_calls_and_executes_only_selected_branch(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        background_actions_enabled=True,
    )
    container = build_container(settings)
    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Choose a calculation",
        trigger=OneTimeTrigger(run_at=datetime.now(UTC) - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            selection=LogicSelectionDraft(
                seed="branch-demo",
                options=(
                    ChoiceOptionDraft(
                        option_id="forty-two",
                        label="Six times seven",
                        value=42,
                        action=LogicCallDraft(
                            skill_id="core.calculator",
                            arguments={"expression": "6 * 7"},
                        ),
                    ),
                    ChoiceOptionDraft(
                        option_id="eighty-one",
                        label="Nine squared",
                        value=81,
                        action=LogicCallDraft(
                            skill_id="core.calculator",
                            arguments={"expression": "9 ** 2"},
                        ),
                    ),
                ),
            ),
            action=LogicSelectionResultDraft(),
            completion=CompletionPolicy(max_runs=1),
        ),
    )

    assert len(schedule.skill_snapshot) == 2
    assert schedule.autonomy_policy.allowed_skill_ids == ("core.calculator",)
    assert await container.scheduler.run_once() == 1
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result["data"]["selected_id"] in {"forty-two", "eighty-one"}
    assert run.result["data"]["action_result"]["data"]["value"] in {42.0, 81.0}


@pytest.mark.asyncio
async def test_logic_workflow_selects_from_live_non_sensitive_result_items(tmp_path):
    settings = Settings(
        profile="testing",
        data_dir=tmp_path,
        background_actions_enabled=True,
    )
    container = build_container(settings)
    tasks = container.registry.get("core.tasks")
    for index, title in enumerate(("Read chapter one", "Review notes"), 1):
        result = await tasks.execute(
            SkillCall(
                skill_id="core.tasks",
                arguments={"operation": "add", "title": title},
                permission_context=PermissionContext(session_id="logic-tasks"),
                idempotency_key=f"add-task-{index}",
            ),
            asyncio.Event(),
        )
        assert result.status == ResultStatus.COMPLETED

    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Choose an open task",
        trigger=OneTimeTrigger(run_at=datetime.now(UTC) - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            check=LogicCallDraft(
                skill_id="core.tasks",
                arguments={"operation": "list"},
            ),
            selection=LogicSelectionDraft(
                source="check_items",
                strategy="least_recent",
                filters=(
                    ChoiceFilter(
                        path="metadata.completed",
                        operator="equals",
                        value=False,
                    ),
                ),
            ),
            action=LogicNotificationDraft(
                title="Task suggestion",
                message="Try this next: {choice.label}",
            ),
            completion=CompletionPolicy(max_runs=1),
        ),
    )

    assert await container.scheduler.run_once() == 1
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    notification = container.notification_center.history()[-1]

    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result["data"]["selected_id"].startswith("task-")
    assert notification.title == "Task suggestion"
    assert notification.message in {
        "Try this next: Read chapter one",
        "Try this next: Review notes",
    }


@pytest.mark.asyncio
async def test_schedule_skill_creates_a_durable_delayed_logic_workflow(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            background_actions_enabled=True,
        )
    )
    started = datetime.now(UTC)
    result = await container.registry.get("core.schedule").execute(
        SkillCall(
            skill_id="core.schedule",
            arguments={
                "operation": "create_logic",
                "name": "Delayed choice",
                "delay_seconds": 60,
                "workflow": {
                    "selection": {
                        "options": [
                            {"option_id": "tea", "label": "Tea", "value": "tea"},
                            {"option_id": "coffee", "label": "Coffee", "value": "coffee"},
                        ],
                        "seed": "drink-demo",
                    },
                    "action": {"kind": "selection_result"},
                    "completion": {"max_runs": 1},
                },
                "max_attempts": 3,
                "retry_delay_seconds": 10,
                "retry_backoff": "fixed",
                "max_retry_delay_seconds": 10,
            },
            permission_context=PermissionContext(session_id="logic-delay"),
        ),
        asyncio.Event(),
    )
    finished = datetime.now(UTC)
    schedule_value = result.data["schedules"][0]
    run_at = datetime.fromisoformat(schedule_value["next_run_at"])

    assert result.status == ResultStatus.COMPLETED
    assert schedule_value["action"]["kind"] == "logic_workflow"
    assert started + timedelta(seconds=59) <= run_at <= finished + timedelta(seconds=61)
    run_policy = schedule_value["run_policy"]
    assert run_policy["max_attempts"] == 3
    assert run_policy["retry_delay_seconds"] == 10
    assert run_policy["retry_backoff"] == "fixed"
    assert run_policy["max_retry_delay_seconds"] == 10


@pytest.mark.asyncio
async def test_expired_workflow_stops_before_running_its_action(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            background_actions_enabled=True,
        )
    )
    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Already expired",
        trigger=OneTimeTrigger(run_at=datetime.now(UTC) - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            action=LogicNotificationDraft(title="Too late", message="Do not deliver"),
            completion=CompletionPolicy(
                max_runs=5,
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            ),
        ),
    )

    assert await container.scheduler.run_once() == 1
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    assert run.state == ScheduleRunState.SUCCEEDED
    assert run.result["data"]["action_executed"] is False
    assert run.result["data"]["workflow"]["stop_reason"] == "Workflow expiry was reached"
    assert container.notification_center.history() == ()


@pytest.mark.asyncio
async def test_dynamic_selection_rejects_sensitive_check_results(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            background_actions_enabled=True,
        )
    )
    notes = container.registry.get("core.notes")
    await notes.execute(
        SkillCall(
            skill_id="core.notes",
            arguments={"operation": "add", "title": "Private", "body": "Secret text"},
            permission_context=PermissionContext(session_id="logic-notes"),
        ),
        asyncio.Event(),
    )
    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Do not select private notes",
        trigger=OneTimeTrigger(run_at=datetime.now(UTC) - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            check=LogicCallDraft(
                skill_id="core.notes",
                arguments={"operation": "search", "query": "Private"},
            ),
            selection=LogicSelectionDraft(source="check_items"),
            action=LogicNotificationDraft(
                title="Private note",
                message="{choice.label}",
            ),
            completion=CompletionPolicy(max_runs=1),
        ),
    )

    assert await container.scheduler.run_once() == 1
    run = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]

    assert run.state == ScheduleRunState.FAILED
    assert run.result["error"]["code"] == "logic_selection_failed"
    assert run.result["data"]["workflow"]["stop_reason"] == (
        "Reached the maximum of 1 runs"
    )
    assert container.notification_center.history()[-1].severity == "error"


@pytest.mark.asyncio
async def test_retries_keep_one_occurrence_count_and_use_bounded_backoff(tmp_path):
    container = build_container(
        Settings(
            profile="testing",
            data_dir=tmp_path,
            background_actions_enabled=True,
        )
    )
    clock = MutableClock(datetime.now(UTC))
    container.schedule_store.clock = clock
    container.scheduler.clock = clock
    container.scheduler.runner.clock = clock
    assert container.scheduler.runner.logic_runner is not None
    container.scheduler.runner.logic_runner.clock = clock
    authoring = container.registry.get("core.schedule").authoring
    schedule = authoring.create_logic(
        name="Bounded failing check",
        trigger=OneTimeTrigger(run_at=clock.now() - timedelta(seconds=1)),
        workflow=LogicWorkflowDraft(
            check=LogicCallDraft(
                skill_id="core.calculator",
                arguments={"expression": "1 / 0"},
            ),
            action=LogicNotificationDraft(title="Unexpected", message="Do not deliver"),
            completion=CompletionPolicy(max_runs=1),
        ),
        run_policy=RunPolicy(
            max_attempts=3,
            retry_delay_seconds=5,
            retry_backoff="exponential",
            max_retry_delay_seconds=8,
        ),
    )

    assert await container.scheduler.run_once() == 1
    first = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert first.state == ScheduleRunState.PENDING
    assert first.retry_at == clock.now() + timedelta(seconds=5)
    assert container.logic_state.workflow_progress(schedule.schedule_id, 1).run_count == 0

    clock.advance(5)
    assert await container.scheduler.run_once() == 1
    second = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    assert second.state == ScheduleRunState.PENDING
    assert second.retry_at == clock.now() + timedelta(seconds=8)
    assert container.logic_state.workflow_progress(schedule.schedule_id, 1).run_count == 0

    clock.advance(8)
    assert await container.scheduler.run_once() == 1
    final = container.schedule_store.list_runs(schedule_id=schedule.schedule_id)[0]
    progress = container.logic_state.workflow_progress(schedule.schedule_id, 1)

    assert final.state == ScheduleRunState.FAILED
    assert final.attempt == 3
    assert progress.run_count == 1
    assert progress.should_stop is True
    assert container.schedule_store.get(schedule.schedule_id).status == ScheduleStatus.COMPLETED


def test_retry_policy_supports_fixed_and_bounded_exponential_delays():
    assert RunPolicy(
        max_attempts=3,
        retry_delay_seconds=10,
        retry_backoff="fixed",
        max_retry_delay_seconds=10,
    ).retry_backoff == "fixed"
    with pytest.raises(ValidationError, match="cannot be shorter"):
        RunPolicy(retry_delay_seconds=30, max_retry_delay_seconds=10)


def test_logic_tables_are_created_by_current_database_schema(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'schema.db'}")
    database.migrate()
    tables = set(inspect(database.engine).get_table_names())
    assert {
        "logic_choice_history",
        "logic_workflow_state",
        "logic_checkpoints",
    } <= tables
