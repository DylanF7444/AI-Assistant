from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from assistant.contracts import (
    ChartSpec,
    FlowchartSpec,
    TableSpec,
    TimelineSpec,
    VisualArtifact,
)


class VisualizationView(QWidget):
    """Small dependency-free renderer for safe, structured visual artifacts."""

    BACKGROUND = QColor("#0d192a")
    PANEL = QColor("#12243a")
    PANEL_ALT = QColor("#172d47")
    TEXT = QColor("#e8f0fa")
    MUTED = QColor("#91a6bf")
    GRID = QColor("#2b4664")
    ACCENT = QColor("#64c7ff")
    ACCENT_2 = QColor("#72e2bd")
    WARNING = QColor("#f0b866")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("visualizationView")
        self.setAccessibleName("Assistant visualization")
        self.setMinimumSize(640, 440)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._artifact: VisualArtifact | None = None

    @property
    def artifact(self) -> VisualArtifact | None:
        return self._artifact

    def set_artifact(self, artifact: VisualArtifact) -> None:
        self._artifact = artifact
        self.setAccessibleDescription(artifact.alt_text)
        self.update()

    def clear(self) -> None:
        self._artifact = None
        self.setAccessibleDescription("")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(920, 640)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.BACKGROUND)
        if self._artifact is None:
            self._draw_empty(painter)
            return

        margin = 28.0
        self._draw_text(
            painter,
            QRectF(margin, 22, self.width() - margin * 2, 42),
            self._artifact.title,
            font_size=19,
            color=self.TEXT,
            bold=True,
        )
        body = QRectF(margin, 82, self.width() - margin * 2, self.height() - 112)
        spec = self._artifact.spec
        if isinstance(spec, ChartSpec):
            self._draw_chart(painter, body, spec)
        elif isinstance(spec, FlowchartSpec):
            self._draw_flowchart(painter, body, spec)
        elif isinstance(spec, TimelineSpec):
            self._draw_timeline(painter, body, spec)
        elif isinstance(spec, TableSpec):
            self._draw_table(painter, body, spec)

    def _draw_empty(self, painter: QPainter) -> None:
        self._draw_text(
            painter,
            QRectF(28, self.height() / 2 - 25, self.width() - 56, 50),
            "No visualization selected",
            font_size=15,
            color=self.MUTED,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

    def _draw_chart(self, painter: QPainter, rect: QRectF, spec: ChartSpec) -> None:
        left = rect.left() + 50
        top = rect.top() + 20
        right = rect.right() - 20
        bottom = rect.bottom() - 54
        plot = QRectF(left, top, max(10.0, right - left), max(10.0, bottom - top))
        values = list(spec.values)
        minimum = min(0.0, min(values))
        maximum = max(0.0, max(values))
        if math.isclose(minimum, maximum):
            minimum -= 1
            maximum += 1
        scale = plot.height() / (maximum - minimum)

        painter.setPen(QPen(self.GRID, 1))
        for index in range(6):
            value = minimum + (maximum - minimum) * index / 5
            y = plot.bottom() - (value - minimum) * scale
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            self._draw_text(
                painter,
                QRectF(rect.left(), y - 10, 44, 20),
                f"{value:g}",
                font_size=9,
                color=self.MUTED,
                alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )

        painter.setPen(QPen(self.GRID, 1.5))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())
        count = len(values)
        points: list[QPointF] = []
        for index, value in enumerate(values):
            x = plot.left() + plot.width() * (index + 0.5) / count
            y = plot.bottom() - (value - minimum) * scale
            points.append(QPointF(x, y))
            label_width = max(44.0, plot.width() / count - 6)
            self._draw_text(
                painter,
                QRectF(x - label_width / 2, plot.bottom() + 8, label_width, 38),
                spec.labels[index],
                font_size=9,
                color=self.MUTED,
                alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            )

        if spec.chart_type == "bar":
            zero_y = plot.bottom() - (0 - minimum) * scale
            bar_width = min(70.0, plot.width() / count * 0.68)
            for point, _value in zip(points, values, strict=True):
                value_y = point.y()
                bar_top = min(zero_y, value_y)
                bar_height = max(2.0, abs(zero_y - value_y))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.ACCENT)
                painter.drawRoundedRect(
                    QRectF(point.x() - bar_width / 2, bar_top, bar_width, bar_height),
                    5,
                    5,
                )
        else:
            painter.setPen(QPen(self.ACCENT_2, 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for first, second in zip(points, points[1:], strict=False):
                painter.drawLine(first, second)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.ACCENT_2)
            for point in points:
                painter.drawEllipse(point, 5, 5)

        if spec.x_label:
            self._draw_text(
                painter,
                QRectF(plot.left(), rect.bottom() - 30, plot.width(), 20),
                spec.x_label,
                font_size=9,
                color=self.MUTED,
                alignment=Qt.AlignmentFlag.AlignCenter,
            )
        if spec.y_label:
            painter.save()
            painter.translate(rect.left() + 8, plot.center().y())
            painter.rotate(-90)
            self._draw_text(
                painter,
                QRectF(-plot.height() / 2, -10, plot.height(), 20),
                spec.y_label,
                font_size=9,
                color=self.MUTED,
                alignment=Qt.AlignmentFlag.AlignCenter,
            )
            painter.restore()

    def _draw_flowchart(self, painter: QPainter, rect: QRectF, spec: FlowchartSpec) -> None:
        columns = min(4, max(1, len(spec.nodes)))
        rows = math.ceil(len(spec.nodes) / columns)
        cell_width = rect.width() / columns
        cell_height = rect.height() / max(1, rows)
        node_width = min(220.0, cell_width - 24)
        node_height = min(68.0, cell_height - 24)
        node_rects: dict[str, QRectF] = {}
        for index, node in enumerate(spec.nodes):
            row, column = divmod(index, columns)
            x = rect.left() + column * cell_width + (cell_width - node_width) / 2
            y = rect.top() + row * cell_height + (cell_height - node_height) / 2
            node_rects[node.node_id] = QRectF(x, y, node_width, node_height)

        for edge in spec.edges:
            source = node_rects[edge.source]
            target = node_rects[edge.target]
            start, end = _connection_points(source, target)
            painter.setPen(QPen(self.ACCENT, 2))
            painter.drawLine(start, end)
            _draw_arrow(painter, start, end, self.ACCENT)
            if edge.label:
                middle = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
                self._draw_text(
                    painter,
                    QRectF(middle.x() - 55, middle.y() - 18, 110, 36),
                    edge.label,
                    font_size=8,
                    color=self.MUTED,
                    alignment=Qt.AlignmentFlag.AlignCenter,
                )

        for node in spec.nodes:
            node_rect = node_rects[node.node_id]
            painter.setPen(QPen(self.ACCENT_2, 1.5))
            painter.setBrush(self.PANEL)
            painter.drawRoundedRect(node_rect, 10, 10)
            self._draw_text(
                painter,
                node_rect.adjusted(10, 7, -10, -7),
                node.label,
                font_size=11,
                color=self.TEXT,
                alignment=Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            )

    def _draw_timeline(self, painter: QPainter, rect: QRectF, spec: TimelineSpec) -> None:
        count = len(spec.events)
        line_y = rect.center().y()
        painter.setPen(QPen(self.ACCENT, 3))
        painter.drawLine(QPointF(rect.left() + 20, line_y), QPointF(rect.right() - 20, line_y))
        spacing = (rect.width() - 40) / max(1, count - 1)
        card_width = min(190.0, max(100.0, rect.width() / max(1, count) - 16))
        for index, event in enumerate(spec.events):
            x = rect.left() + 20 + spacing * index if count > 1 else rect.center().x()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.WARNING)
            painter.drawEllipse(QPointF(x, line_y), 8, 8)
            card_y = line_y - 122 if index % 2 == 0 else line_y + 28
            card = QRectF(x - card_width / 2, card_y, card_width, 94)
            painter.setPen(QPen(self.GRID, 1))
            painter.setBrush(self.PANEL)
            painter.drawRoundedRect(card, 8, 8)
            self._draw_text(
                painter,
                card.adjusted(10, 8, -10, -8),
                f"{event.when}\n{event.label}" + (f"\n{event.detail}" if event.detail else ""),
                font_size=10,
                color=self.TEXT,
                alignment=Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            )

    def _draw_table(self, painter: QPainter, rect: QRectF, spec: TableSpec) -> None:
        rows = (spec.columns, *spec.rows)
        row_height = min(42.0, max(24.0, rect.height() / max(1, len(rows))))
        column_width = rect.width() / len(spec.columns)
        for row_index, row in enumerate(rows):
            y = rect.top() + row_index * row_height
            for column_index, value in enumerate(row):
                cell = QRectF(
                    rect.left() + column_index * column_width,
                    y,
                    column_width,
                    row_height,
                )
                painter.setPen(QPen(self.GRID, 1))
                painter.setBrush(self.PANEL_ALT if row_index == 0 else self.PANEL)
                painter.drawRect(cell)
                self._draw_text(
                    painter,
                    cell.adjusted(9, 3, -9, -3),
                    value,
                    font_size=10,
                    color=self.TEXT if row_index == 0 else self.MUTED,
                    bold=row_index == 0,
                    alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                )

    @staticmethod
    def _draw_text(
        painter: QPainter,
        rect: QRectF,
        text: str,
        *,
        font_size: float,
        color: QColor,
        alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        bold: bool = False,
    ) -> None:
        font = QFont(painter.font())
        font.setPointSizeF(font_size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, int(alignment), text)


def _connection_points(source: QRectF, target: QRectF) -> tuple[QPointF, QPointF]:
    if abs(target.center().x() - source.center().x()) >= abs(
        target.center().y() - source.center().y()
    ):
        start_x = source.right() if target.center().x() >= source.center().x() else source.left()
        end_x = target.left() if target.center().x() >= source.center().x() else target.right()
        return QPointF(start_x, source.center().y()), QPointF(end_x, target.center().y())
    start_y = source.bottom() if target.center().y() >= source.center().y() else source.top()
    end_y = target.top() if target.center().y() >= source.center().y() else target.bottom()
    return QPointF(source.center().x(), start_y), QPointF(target.center().x(), end_y)


def _draw_arrow(painter: QPainter, start: QPointF, end: QPointF, color: QColor) -> None:
    direction = end - start
    length = math.hypot(direction.x(), direction.y())
    if length == 0:
        return
    unit_x, unit_y = direction.x() / length, direction.y() / length
    side_x, side_y = -unit_y, unit_x
    tip = end
    left = QPointF(end.x() - unit_x * 10 + side_x * 5, end.y() - unit_y * 10 + side_y * 5)
    right = QPointF(end.x() - unit_x * 10 - side_x * 5, end.y() - unit_y * 10 - side_y * 5)
    path = QPainterPath()
    path.moveTo(tip)
    path.lineTo(left)
    path.lineTo(right)
    path.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
