from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant.contracts import (
    ChartSpec,
    FlowchartSpec,
    PermissionContext,
    Response,
    ResultStatus,
    SkillCall,
    SkillResult,
    Transcript,
    VisualArtifact,
)
from assistant.execution.engine import ExecutionEngine
from assistant.response import response_from_result
from assistant.router.deterministic import DeterministicRouter
from assistant.skills.visualize import VisualizeSkill


def chart_artifact() -> VisualArtifact:
    return VisualArtifact(
        title="Monthly sales",
        alt_text="Bar chart of monthly sales.",
        spec=ChartSpec(labels=("Jan", "Feb"), values=(12, 18)),
    )


def test_visual_artifacts_are_typed_immutable_and_round_trip():
    artifact = chart_artifact()

    assert artifact.kind.value == "chart"
    assert VisualArtifact.model_validate(artifact.model_dump()).spec == artifact.spec
    with pytest.raises(ValidationError):
        artifact.title = "changed"
    with pytest.raises(ValidationError):
        ChartSpec(labels=("Jan",), values=(12, 18))


def test_flowchart_rejects_unknown_edge_targets():
    with pytest.raises(ValidationError, match="existing nodes"):
        FlowchartSpec(
            nodes=({"node_id": "start", "label": "Start"},),
            edges=({"source": "start", "target": "missing"},),
        )


def test_visual_artifact_survives_response_mapping_and_round_trip():
    artifact = chart_artifact()
    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Created a chart.",
            visual_artifact=artifact,
        )
    )

    assert response.visual_artifact == artifact
    assert Response.model_validate(response.model_dump()).visual_artifact == artifact


def test_sensitive_visual_is_hidden_with_sensitive_display_protection():
    artifact = chart_artifact()
    response = response_from_result(
        SkillResult(
            status=ResultStatus.COMPLETED,
            summary="Created a sensitive chart.",
            visual_artifact=artifact,
            sensitive=True,
        ),
        hide_sensitive_display=True,
    )

    assert response.visual_artifact is None
    assert response.display_text == "Sensitive result hidden by privacy settings."


def test_plan_aggregation_preserves_last_visual_artifact():
    artifact = chart_artifact()
    result = ExecutionEngine._aggregate_plan_results(
        {
            "chart": SkillResult(
                status=ResultStatus.COMPLETED,
                summary="Chart created.",
                visual_artifact=artifact,
            ),
            "follow_up": SkillResult(
                status=ResultStatus.COMPLETED,
                summary="Follow-up completed.",
            ),
        }
    )

    assert result.visual_artifact == artifact


@pytest.mark.asyncio
async def test_visualize_skill_returns_accessible_artifact():
    call = SkillCall(
        skill_id="core.visualize",
        arguments={
            "title": "Build pipeline",
            "spec": {
                "kind": "flowchart",
                "nodes": [
                    {"node_id": "plan", "label": "Plan"},
                    {"node_id": "ship", "label": "Ship"},
                ],
                "edges": [{"source": "plan", "target": "ship"}],
            },
        },
        permission_context=PermissionContext(session_id="test"),
    )

    result = await VisualizeSkill().execute(call, asyncio.Event())

    assert result.status == ResultStatus.COMPLETED
    assert result.visual_artifact is not None
    assert result.visual_artifact.alt_text
    assert result.data["artifact"]["spec"]["kind"] == "flowchart"
    assert VisualizeSkill.manifest.background_mode.value == "foreground_only"


@pytest.mark.asyncio
async def test_visualize_skill_accepts_an_ordinary_language_request():
    call = SkillCall(
        skill_id="core.visualize",
        arguments={
            "request": (
                "Can you make a chart showing monthly sales January twelve "
                "February eighteen March fifteen"
            )
        },
        permission_context=PermissionContext(session_id="test"),
    )

    result = await VisualizeSkill().execute(call, asyncio.Event())

    assert result.status == ResultStatus.COMPLETED
    assert result.visual_artifact is not None
    assert result.visual_artifact.title == "monthly sales"
    assert result.visual_artifact.spec.labels == ("January", "February", "March")
    assert result.visual_artifact.spec.values == (12, 18, 15)


@pytest.mark.asyncio
async def test_visualize_skill_requests_clarification_for_an_ambiguous_request():
    call = SkillCall(
        skill_id="core.visualize",
        arguments={"request": "make a table from this information"},
        permission_context=PermissionContext(session_id="test"),
    )

    result = await VisualizeSkill().execute(call, asyncio.Event())
    VisualizeSkill().validate_result(result)

    assert result.status == ResultStatus.DENIED
    assert result.error is not None
    assert result.error.code == "clarification_required"


@pytest.mark.asyncio
async def test_visualize_skill_accepts_newlines_as_natural_table_whitespace():
    call = SkillCall(
        skill_id="core.visualize",
        arguments={"request": "make a table: Name | Status\nAlice | Ready"},
        permission_context=PermissionContext(session_id="test"),
    )

    result = await VisualizeSkill().execute(call, asyncio.Event())

    assert result.status == ResultStatus.COMPLETED
    assert result.visual_artifact is not None
    assert result.visual_artifact.spec.rows == (("Alice", "Ready"),)


@pytest.mark.asyncio
async def test_built_application_executes_visual_request_without_permission(settings):
    from assistant.bootstrap import build_container

    container = build_container(settings)
    response, outcome = await container.orchestrator.handle_text(
        "plot monthly sales: Jan=12, Feb=18"
    )

    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.visual_artifact is not None
    assert response.visual_artifact.kind.value == "chart"
    audit = container.audit.query(event_type="skill_execution", skill_id="core.visualize")[0]
    assert audit["details"]["arguments"]["payload_omitted"] is True
    assert "Jan" not in str(audit["details"])


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("plot monthly sales: Jan=12, Feb=18", "chart"),
        ("draw a flowchart: Plan -> Build -> Ship", "flowchart"),
        ("show a timeline: 9 AM - Standup; 10 AM - Build", "timeline"),
        ("make a table: Name | Status; Alice | Ready", "table"),
    ],
)
def test_explicit_visual_requests_route_without_planner(text, kind):
    route = DeterministicRouter().route(text)

    assert route.needs_llm is False
    assert route.intent is not None
    assert route.intent.name == "core.visualize"
    assert route.intent.arguments["spec"]["kind"] == kind


@pytest.mark.parametrize(
    "text",
    [
        "plot monthly sales: Jan=12, Feb=18",
        "plot monthly sales Jan:12 Feb:18",
        "plot monthly sales Jan is 12 Feb is 18",
        "plot monthly sales Jan at 12 Feb at 18",
        "plot monthly sales Jan 12 Feb 18",
        "plot monthly sales Jan equals twelve Feb equals eighteen",
        "plot monthly sales Jan twelve dollars Feb eighteen dollars",
    ],
)
def test_chart_label_value_separators_are_interchangeable(text):
    route = DeterministicRouter().route(text)

    assert route.intent is not None
    assert route.intent.arguments["title"] == "monthly sales"
    assert route.intent.arguments["spec"]["labels"] == ("Jan", "Feb")
    assert route.intent.arguments["spec"]["values"] == (12, 18)


@pytest.mark.parametrize(
    "text",
    [
        "Could you please make a chart showing Jan twelve Feb eighteen",
        "I'd like a chart Jan twelve Feb eighteen",
        "Can I get a chart Jan twelve Feb eighteen",
        "I would like to see a chart Jan twelve Feb eighteen",
    ],
)
def test_visual_requests_accept_conversational_polite_phrasing(text):
    route = DeterministicRouter().route(text)

    assert route.intent is not None
    assert route.intent.name == "core.visualize"
    assert route.intent.arguments["spec"]["values"] == (12, 18)


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        (
            "Can you make a chart showing monthly sales January twelve "
            "February eighteen March fifteen",
            "chart",
            (12, 18, 15),
        ),
        (
            "Please draw a flowchart plan then build then test then deploy",
            "flowchart",
            ("plan", "build", "test", "deploy"),
        ),
        (
            "I would like a timeline nine a m standup ten am build two p m review",
            "timeline",
            ("standup", "build", "review"),
        ),
        (
            "Make a table with columns name and status and rows Alice ready Bob waiting",
            "table",
            (("Alice", "ready"), ("Bob", "waiting")),
        ),
    ],
)
def test_dictated_visual_requests_do_not_require_punctuation(text, kind, expected):
    route = DeterministicRouter().route(text)

    assert route.intent is not None
    spec = route.intent.arguments["spec"]
    assert spec["kind"] == kind
    if kind == "chart":
        assert spec["values"] == expected
    elif kind == "flowchart":
        assert tuple(node["label"] for node in spec["nodes"]) == expected
    elif kind == "timeline":
        assert tuple(event["label"] for event in spec["events"]) == expected
    else:
        assert spec["rows"] == expected


def test_chart_can_be_requested_with_the_visual_type_at_the_end():
    route = DeterministicRouter().route("turn January twelve February eighteen into a chart")

    assert route.intent is not None
    assert route.intent.name == "core.visualize"
    assert route.intent.arguments["spec"]["labels"] == ("January", "February")


def test_spoken_punctuation_words_are_optional_visual_structure():
    chart = DeterministicRouter().route(
        "make a chart monthly sales colon January colon twelve February equals sign eighteen"
    )
    table = DeterministicRouter().route("make a table Name pipe Status semicolon Alice pipe Ready")

    assert chart.intent is not None
    assert chart.intent.arguments["spec"]["values"] == (12, 18)
    assert table.intent is not None
    assert table.intent.arguments["spec"]["rows"] == (("Alice", "Ready"),)


def test_natural_branching_flow_creates_labeled_decision_edges():
    route = DeterministicRouter().route(
        "draw the release flow start then review then if approved then ship otherwise revise"
    )

    assert route.intent is not None
    spec = route.intent.arguments["spec"]
    assert tuple(node["label"] for node in spec["nodes"]) == (
        "start",
        "review",
        "approved?",
        "ship",
        "revise",
    )
    assert spec["edges"][-2]["label"] == "Yes"
    assert spec["edges"][-1]["label"] == "No"


def test_ambiguous_visual_structure_asks_for_clarification():
    route = DeterministicRouter().route("make a table from customer account information")

    assert route.intent is None
    assert route.clarification is not None
    assert "columns and rows" in route.clarification


def test_data_seeking_visual_request_uses_planner_instead_of_inventing_table_cells():
    route = DeterministicRouter().route(
        "show me a table of the highest earning movies this year"
    )

    assert route.needs_llm is True
    assert route.intent is not None
    assert route.intent.name == "planner.goal"
    assert route.intent.arguments == {
        "text": "show me a table of the highest earning movies this year"
    }
    assert route.clarification is None


def test_existing_media_route_remains_media_route():
    route = DeterministicRouter().route("show C:/Pictures/photo.png")

    assert route.intent is not None
    assert route.intent.name == "media.present"


def test_media_file_named_diagram_remains_a_media_request():
    route = DeterministicRouter().route("show C:/Pictures/diagram.png")

    assert route.intent is not None
    assert route.intent.name == "media.present"


def test_generic_visualize_language_stays_on_planner_path():
    route = DeterministicRouter().route("visualize my schedule")

    assert route.needs_llm is True
    assert route.intent is not None
    assert route.intent.name == "planner.goal"


@pytest.mark.parametrize(
    "text",
    [
        "what is a chart?",
        "how does a flowchart work?",
        "tell me about timeline diagrams",
        "can you show me how a chart works?",
        "could you tell me what a flowchart is?",
    ],
)
def test_explanatory_visual_questions_stay_on_planner_path(text):
    route = DeterministicRouter().route(text)

    assert route.needs_llm is True
    assert route.intent is not None
    assert route.intent.name == "planner.goal"


@pytest.mark.asyncio
async def test_voice_transcript_uses_normalized_speech_to_create_visual(settings):
    from assistant.bootstrap import build_container

    container = build_container(settings)
    transcript = Transcript(
        session_id=uuid4(),
        raw_text=("make a chart monthly sales January twelve February eighteen March fifteen"),
        normalized_text=(
            "make a chart monthly sales january twelve february eighteen march fifteen"
        ),
        confidence=0.95,
    )

    response, outcome = await container.orchestrator.handle_transcript(transcript)

    assert outcome.result is not None
    assert outcome.result.status == ResultStatus.COMPLETED
    assert response.visual_artifact is not None
    assert response.visual_artifact.spec.values == (12, 18, 15)
