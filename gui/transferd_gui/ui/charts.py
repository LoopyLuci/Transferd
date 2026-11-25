"""A line chart drawn with QPainter: time on the x axis, one value axis, one line per series.

QPainter is used instead of a charting package because the dashboard needs three simple time series, and this keeps the
dependency list to PySide6 alone. With no points the chart says "no data yet" and draws no line.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from transferd_gui.model.fmt import NO_DATA
from transferd_gui.ui import theme

Point = Tuple[float, float]


class LineChart(QWidget):
    """Plots named series of `(epoch seconds, value)` over the last `window_s` seconds."""

    def __init__(self, title: str, unit: str, window_s: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._window_s = window_s
        self._series: List[Tuple[str, Sequence[Point]]] = []
        self._now: Optional[float] = None
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName(title)

    def set_window(self, window_s: int) -> None:
        self._window_s = window_s
        self.update()

    def set_series(self, series: Sequence[Tuple[str, Sequence[Point]]], now: Optional[float] = None) -> None:
        """Replace the lines. Series with no points are left out of the drawing and of the legend."""
        self._series = [(name, points) for name, points in series if points]
        self._now = now if now is not None else time.time()
        self.setToolTip("\n".join(f"{name}: {pts[-1][1]:.2f} {self._unit}" for name, pts in self._series))
        self.update()

    def series_names(self) -> List[str]:
        return [name for name, _points in self._series]

    def has_data(self) -> bool:
        return bool(self._series)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's name
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(str(tokens["card"])))
        muted = QColor(str(tokens["text_muted"]))
        font = painter.font()
        font.setPointSize(max(font.pointSize(), 9))
        painter.setFont(font)

        title_rect = QRectF(12, 8, self.width() - 24, 20)
        painter.setPen(QColor(str(tokens["text"])))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft, f"{self._title} ({self._unit})")

        plot = QRectF(52, 36, self.width() - 66, max(self.height() - 36 - 30, 20))
        if not self._series or self._now is None:
            painter.setPen(muted)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, NO_DATA)
            painter.end()
            return

        values = [v for _name, pts in self._series for _t, v in pts]
        top = max(max(values), 1e-9)
        top = top * 1.1
        start = self._now - self._window_s
        self._draw_axes(painter, plot, top, start, muted, tokens)
        palette = list(tokens["series"])
        for index, (_name, points) in enumerate(self._series):
            colour = QColor(palette[index % len(palette)])
            pen = QPen(colour, 2)
            painter.setPen(pen)
            path = [self._project(plot, t, v, start, top) for t, v in points if t >= start]
            for a, b in zip(path, path[1:]):
                painter.drawLine(a, b)
            if path:
                painter.drawEllipse(path[-1], 3, 3)
        self._draw_legend(painter, palette, plot)
        painter.end()

    def _project(self, plot: QRectF, when: float, value: float, start: float, top: float) -> QPointF:
        x = plot.left() + (when - start) / self._window_s * plot.width()
        y = plot.bottom() - (value / top) * plot.height()
        return QPointF(x, y)

    def _draw_axes(self, painter: QPainter, plot: QRectF, top: float, start: float, muted: QColor, tokens) -> None:
        grid = QColor(str(tokens["chart_grid"]))
        painter.setPen(QPen(grid, 1))
        for step in range(5):
            y = plot.top() + plot.height() * step / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(muted)
            label = top * (1 - step / 4)
            painter.drawText(QRectF(0, y - 8, plot.left() - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{label:.3g}")
            painter.setPen(QPen(grid, 1))
        painter.setPen(muted)
        painter.drawText(QRectF(plot.left(), plot.bottom() + 6, 120, 16), Qt.AlignmentFlag.AlignLeft, f"-{self._window_s} s")
        painter.drawText(QRectF(plot.right() - 120, plot.bottom() + 6, 120, 16), Qt.AlignmentFlag.AlignRight, "now")

    def _draw_legend(self, painter: QPainter, palette: List[object], plot: QRectF) -> None:
        x = plot.left()
        y = self.height() - 16
        font = QFont(painter.font())
        painter.setFont(font)
        for index, (name, _points) in enumerate(self._series):
            colour = QColor(str(palette[index % len(palette)]))
            painter.setPen(QPen(colour, 3))
            painter.drawLine(QPointF(x, y), QPointF(x + 14, y))
            painter.setPen(QColor(str(theme.current_tokens()["text"])))
            width = painter.fontMetrics().horizontalAdvance(name) + 12
            painter.drawText(QRectF(x + 18, y - 8, width + 10, 16), Qt.AlignmentFlag.AlignLeft, name)
            x += 18 + width + 18
            if x > self.width() - 60:
                break
