from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from assistant.contracts import (
    ChartSpec,
    FlowchartSpec,
    TableSpec,
    TimelineSpec,
    VisualArtifact,
)
from assistant.presentation.session import MediaSessionRegistry
from assistant.ui.desktop.media_viewer import MediaViewer
from assistant.ui.desktop.result_window import ResultWindow
from assistant.ui.desktop.visualization_view import VisualizationView


def artifact_for(spec, title="Visual test"):
    return VisualArtifact(title=title, alt_text=f"Accessible description for {title}.", spec=spec)


@pytest.mark.ui
@pytest.mark.parametrize(
    "spec",
    [
        ChartSpec(labels=("A", "B", "C"), values=(1, 4, 2)),
        FlowchartSpec(
            nodes=(
                {"node_id": "one", "label": "One"},
                {"node_id": "two", "label": "Two"},
            ),
            edges=({"source": "one", "target": "two"},),
        ),
        TimelineSpec(
            events=(
                {"when": "9 AM", "label": "Start"},
                {"when": "10 AM", "label": "Finish"},
            )
        ),
        TableSpec(columns=("Name", "State"), rows=(("Alice", "Ready"),)),
    ],
)
def test_visualization_view_paints_each_supported_artifact(qtbot, spec):
    view = VisualizationView()
    qtbot.addWidget(view)
    view.resize(900, 620)
    view.set_artifact(artifact_for(spec))

    image = view.grab()

    assert not image.isNull()
    assert view.artifact is not None
    assert view.accessibleDescription() == "Accessible description for Visual test."


@pytest.mark.ui
def test_result_window_can_show_visual_without_changing_media_viewer(qtbot):
    media_viewer = MediaViewer(MediaSessionRegistry())
    window = ResultWindow(media_viewer)
    qtbot.addWidget(window)
    qtbot.addWidget(media_viewer)
    qtbot.addWidget(window.visualization_view)

    window.show_visual(
        artifact_for(
            ChartSpec(labels=("Jan", "Feb"), values=(2, 3)),
            title="Sales",
        )
    )

    assert window.stack.currentWidget() is window.visualization_view
    assert window.visualization_view.artifact is not None
    assert window.windowTitle() == "Local Assistant — Sales"
    window.hide()
    media_viewer.shutdown()
