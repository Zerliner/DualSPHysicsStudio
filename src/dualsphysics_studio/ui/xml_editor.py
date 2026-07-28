"""Lightweight XML source editor decorations for DualSPHysics Studio."""

from __future__ import annotations

from bisect import bisect_right

from PySide6.QtCore import (
    QEvent,
    QPointF,
    QRect,
    QRectF,
    QRegularExpression,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
    QResizeEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import QPlainTextEdit, QStyle, QStyleOption, QWidget

from ..core.source import SourceIndex


_COMMENT_STATE = 1


class XmlSyntaxHighlighter(QSyntaxHighlighter):
    """Apply a small, readable XML colour scheme without changing source text."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)

        self._comment_format = QTextCharFormat()
        self._comment_format.setForeground(QColor("#2e7d32"))

        self._tag_format = QTextCharFormat()
        self._tag_format.setForeground(QColor("#1565c0"))

        self._attribute_format = QTextCharFormat()
        self._attribute_format.setForeground(QColor("#007c91"))

        self._string_format = QTextCharFormat()
        self._string_format.setForeground(QColor("#9c5700"))

        name = r"[A-Za-z_:][A-Za-z0-9_.:\-]*"
        self._rules = (
            (QRegularExpression(r"[<>=/?!]"), 0, self._tag_format),
            (QRegularExpression(rf"</?\s*({name})"), 1, self._tag_format),
            (QRegularExpression(rf"<\?\s*({name})"), 1, self._tag_format),
            (
                QRegularExpression(rf"\b({name})(?=\s*=)"),
                1,
                self._attribute_format,
            ),
            (
                QRegularExpression(r'''"[^"]*"|'[^']*' '''.strip()),
                0,
                self._string_format,
            ),
        )
        self._comment_start = QRegularExpression(r"<!--")
        self._comment_end = QRegularExpression(r"-->")

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt virtual method
        for pattern, capture, text_format in self._rules:
            matches = pattern.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                start = match.capturedStart(capture)
                length = match.capturedLength(capture)
                if start >= 0 and length > 0:
                    self.setFormat(start, length, text_format)

        self.setCurrentBlockState(0)
        if self.previousBlockState() == _COMMENT_STATE:
            comment_start = 0
        else:
            match = self._comment_start.match(text)
            comment_start = match.capturedStart() if match.hasMatch() else -1

        text_length = _utf16_length(text)
        while comment_start >= 0:
            end_match = self._comment_end.match(text, comment_start)
            if not end_match.hasMatch():
                self.setFormat(
                    comment_start,
                    text_length - comment_start,
                    self._comment_format,
                )
                self.setCurrentBlockState(_COMMENT_STATE)
                break

            comment_end = end_match.capturedEnd()
            self.setFormat(
                comment_start,
                comment_end - comment_start,
                self._comment_format,
            )
            next_match = self._comment_start.match(text, comment_end)
            comment_start = next_match.capturedStart() if next_match.hasMatch() else -1


class _XmlEditorGutter(QWidget):
    def __init__(self, editor: "XmlCodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        self._editor._paint_gutter(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._editor._handle_gutter_click(event):
            event.accept()
            return
        super().mousePressEvent(event)


class _XmlMinimap(QWidget):
    """View-only XML overview with a draggable viewport indicator."""

    def __init__(self, editor: "XmlCodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor
        self._cache: QImage | None = None
        self._drag_offset: float | None = None
        self.setObjectName("xml_minimap")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(self._editor.minimap_width(), 0)

    def invalidate(self) -> None:
        self._cache = None
        self._editor._minimap_visible_line_count = None
        self.update()

    def viewport_indicator_rect(self) -> QRectF:
        return self._editor._minimap_viewport_rect()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._cache is None:
            self._cache = self._editor._create_minimap_cache(self.size())

        painter = QPainter(self)
        painter.drawImage(QPointF(0.0, 0.0), self._cache)
        indicator = self.viewport_indicator_rect()
        painter.fillRect(indicator, QColor(88, 96, 105, 72))
        painter.setPen(QPen(QColor(88, 96, 105, 132), 1))
        painter.drawRect(indicator)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.invalidate()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self.invalidate()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return

        indicator = self.viewport_indicator_rect()
        if indicator.height() >= self.height() or self.height() <= 0:
            event.accept()
            return
        if indicator.contains(event.position()):
            self._drag_offset = event.position().y() - indicator.top()
        else:
            self._drag_offset = indicator.height() / 2.0
            self._editor._scroll_from_minimap(
                event.position().y(), self._drag_offset
            )
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._drag_offset is None
            or not event.buttons() & Qt.MouseButton.LeftButton
        ):
            event.ignore()
            return
        self._editor._scroll_from_minimap(event.position().y(), self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._editor._scroll_from_minimap(event.position().y(), self._drag_offset)
            self._drag_offset = None
            event.accept()
            return
        event.ignore()


class XmlCodeEditor(QPlainTextEdit):
    """QPlainTextEdit with line numbers, XML highlighting and view-only folding.

    Public line arguments and fold-region keys are zero-based, matching
    ``QTextBlock.blockNumber()``.  The gutter presents those values as familiar
    one-based source line numbers.  Folding only changes ``QTextBlock``
    visibility; the document text, modified flag and undo stack remain
    authoritative and untouched.
    """

    _FOLD_COLUMN_WIDTH = 18
    _GUTTER_PADDING = 6
    _FOLD_RESCAN_DELAY_MS = 180
    _MINIMAP_WIDTH = 112
    _MINIMAP_TEXT_SCALE = 0.18
    _MINIMAP_MIN_INDICATOR_HEIGHT = 14.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fold_regions: dict[int, int] = {}
        self._folded_lines: set[int] = set()
        self._setting_plain_text = False
        self._minimap_visible_line_count: int | None = None

        self._gutter = _XmlEditorGutter(self)
        self._minimap = _XmlMinimap(self)
        self._highlighter = XmlSyntaxHighlighter(self.document())

        self._fold_timer = QTimer(self)
        self._fold_timer.setSingleShot(True)
        self._fold_timer.setInterval(self._FOLD_RESCAN_DELAY_MS)
        self._fold_timer.timeout.connect(self.rebuild_fold_regions)

        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.textChanged.connect(self._source_text_changed)
        self.verticalScrollBar().valueChanged.connect(
            lambda _value: self._minimap.update()
        )
        self.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum: self._minimap.update()
        )
        self._update_gutter_width()

    @property
    def fold_regions(self) -> dict[int, int]:
        """Return ``{start_line: end_line}`` for safely matched XML elements."""

        return dict(self._fold_regions)

    def line_number_area_width(self) -> int:
        """Return the width reserved for line numbers and fold arrows."""

        digits = len(str(max(1, self.blockCount())))
        number_width = self.fontMetrics().horizontalAdvance("9") * digits
        return self._GUTTER_PADDING * 2 + number_width + self._FOLD_COLUMN_WIDTH

    def minimap_width(self) -> int:
        """Return the overview width in device-independent pixels."""

        return self._MINIMAP_WIDTH

    @staticmethod
    def indent_guide_columns(text: str) -> tuple[int, ...]:
        """Return four-column visual indentation boundaries for ``text``."""

        visual_column = 0
        for character in text:
            if character == " ":
                visual_column += 1
            elif character == "\t":
                visual_column += 4 - (visual_column % 4)
            else:
                break
        return tuple(range(4, visual_column + 1, 4))

    def indent_guide_columns_for_line(self, line: int) -> tuple[int, ...]:
        """Return guide columns, continuing shared hierarchy over empty lines."""

        block = self.document().findBlockByNumber(line)
        if not block.isValid() or not block.isVisible():
            return ()

        direct = self.indent_guide_columns(block.text())
        if block.text().strip():
            return direct

        previous = block.previous()
        while previous.isValid() and (
            not previous.isVisible() or not previous.text().strip()
        ):
            previous = previous.previous()
        following = block.next()
        while following.isValid() and (
            not following.isVisible() or not following.text().strip()
        ):
            following = following.next()
        if not previous.isValid() or not following.isValid():
            return ()

        previous_columns = self.indent_guide_columns(previous.text())
        following_columns = set(self.indent_guide_columns(following.text()))
        return tuple(
            column for column in previous_columns if column in following_columns
        )

    def is_foldable(self, line: int) -> bool:
        return line in self._fold_regions

    def is_folded(self, line: int) -> bool:
        return line in self._folded_lines

    def toggle_fold(self, line: int) -> bool:
        """Toggle a zero-based source line and return its resulting fold state."""

        if line not in self._fold_regions:
            return False
        if line in self._folded_lines:
            self._folded_lines.remove(line)
        else:
            self._move_selection_out_of_fold(line, self._fold_regions[line])
            self._folded_lines.add(line)
        self._apply_fold_visibility()
        return line in self._folded_lines

    def rebuild_fold_regions(self) -> dict[int, int]:
        """Rescan the current text, clearing stale folds on invalid drafts."""

        self._fold_timer.stop()
        self._clear_fold_state()
        text = self.toPlainText()
        if not text:
            return self.fold_regions

        try:
            source_index = SourceIndex(text)
        except Exception:
            # Folding is optional decoration.  Incomplete XML remains fully
            # editable while CaseDocument keeps the last valid 3D scene.
            return self.fold_regions

        newline_offsets = [index for index, character in enumerate(text) if character == "\n"]
        block_count = self.blockCount()
        regions: dict[int, int] = {}
        for token in source_index.tokens.values():
            if token.self_closing or token.close_start is None:
                continue
            start_line = bisect_right(newline_offsets, token.start)
            end_line = bisect_right(newline_offsets, token.close_start)
            if start_line >= end_line or end_line >= block_count:
                continue
            # A gutter has one arrow per physical line.  If compact XML starts
            # nested elements on the same line, the outermost region is useful
            # and unambiguous for that single arrow.
            regions[start_line] = max(end_line, regions.get(start_line, 0))

        self._fold_regions = dict(sorted(regions.items()))
        self._gutter.update()
        return self.fold_regions

    def setPlainText(self, text: str) -> None:  # noqa: N802 - Qt API override
        """Replace text with standard QPlainTextEdit semantics, then rescan."""

        self._fold_timer.stop()
        self._clear_fold_state()
        self._setting_plain_text = True
        try:
            super().setPlainText(text)
        finally:
            self._setting_plain_text = False
        self.rebuild_fold_regions()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        viewport = self.viewport().geometry()
        self._gutter.setGeometry(
            QRect(
                self.contentsRect().left(),
                viewport.top(),
                self.line_number_area_width(),
                viewport.height(),
            )
        )
        self._minimap.setGeometry(
            QRect(
                viewport.right() + 1,
                viewport.top(),
                self.minimap_width(),
                viewport.height(),
            )
        )

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "_gutter"):
            self._update_gutter_width()
            self.viewport().update()
            self._minimap.invalidate()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#d8dee8"), 1))
        space_width = self.fontMetrics().horizontalAdvance(" ")
        content_x = self.contentOffset().x()

        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            height = round(self.blockBoundingRect(block).height())
            bottom = top + height
            if block.isVisible() and bottom >= event.rect().top():
                for column in self.indent_guide_columns_for_line(
                    block.blockNumber()
                ):
                    x = round(content_x + column * space_width)
                    painter.drawLine(x, top, x, bottom)
            block = block.next()
            top = bottom

    def _source_text_changed(self) -> None:
        # Cached line ranges are never retained across edits.  In particular,
        # a last-valid CaseDocument index must not hide lines in an invalid
        # editor draft with different positions.
        self._fold_timer.stop()
        self._clear_fold_state()
        self._minimap.invalidate()
        if not self._setting_plain_text:
            self._fold_timer.start()

    def _update_gutter_width(self, _block_count: int = 0) -> None:
        self.setViewportMargins(
            self.line_number_area_width(), 0, self.minimap_width(), 0
        )
        self._gutter.updateGeometry()
        self._minimap.updateGeometry()

    def _update_gutter(self, rect: QRect, vertical_delta: int) -> None:
        if vertical_delta:
            self._gutter.scroll(0, vertical_delta)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def _create_minimap_cache(self, size: QSize) -> QImage:
        device_ratio = max(1.0, self._minimap.devicePixelRatioF())
        image = QImage(
            QSize(
                max(1, round(size.width() * device_ratio)),
                max(1, round(size.height() * device_ratio)),
            ),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.setDevicePixelRatio(device_ratio)
        image.fill(QColor("#f8fafc"))

        painter = QPainter(image)
        painter.setClipRect(QRectF(1.0, 0.0, size.width() - 1.0, size.height()))
        painter.setPen(QPen(QColor("#d8dee8"), 1))
        painter.drawLine(0, 0, 0, size.height())

        visible_count = 0
        visible_line_count = 0
        block = self.document().firstBlock()
        while block.isValid():
            if block.isVisible():
                visible_count += 1
                visible_line_count += max(1, block.lineCount())
            block = block.next()
        self._minimap_visible_line_count = max(1, visible_line_count)
        if visible_count == 0 or size.height() <= 2:
            painter.end()
            return image

        line_height = max(1, self.fontMetrics().height())
        available_height = max(1.0, float(size.height() - 2))
        vertical_scale = min(
            self._MINIMAP_TEXT_SCALE,
            available_height / (visible_count * line_height),
        )
        if vertical_scale >= 0.045:
            painter.save()
            painter.translate(3.0, 1.0)
            painter.scale(0.14, vertical_scale)
            visual_line = 0
            block = self.document().firstBlock()
            while block.isValid():
                if block.isVisible():
                    self.document().documentLayout().blockBoundingRect(block)
                    block.layout().draw(
                        painter, QPointF(0.0, visual_line * line_height)
                    )
                    visual_line += 1
                block = block.next()
            painter.restore()
        else:
            # For very large XML files, a bounded number of coloured strokes
            # preserves the whole-document silhouette without repainting
            # hundreds of thousands of text layouts.
            maximum_strokes = max(1, int(available_height))
            stride = max(1, (visible_count + maximum_strokes - 1) // maximum_strokes)
            visual_line = 0
            block = self.document().firstBlock()
            while block.isValid():
                if block.isVisible():
                    if visual_line % stride == 0:
                        text = block.text()
                        stripped = text.strip()
                        if stripped:
                            if (
                                "<!--" in text
                                or block.userState() == _COMMENT_STATE
                                or (
                                    block.previous().isValid()
                                    and block.previous().userState() == _COMMENT_STATE
                                )
                            ):
                                painter.setPen(QPen(QColor("#2e7d32"), 1))
                            else:
                                painter.setPen(QPen(QColor("#1565c0"), 1))
                            if visible_count == 1:
                                y = 1.0
                            else:
                                y = 1.0 + (
                                    visual_line
                                    / (visible_count - 1)
                                    * (available_height - 1.0)
                                )
                            indent = len(text) - len(text.lstrip(" \t"))
                            x = min(size.width() * 0.32, 3.0 + indent * 0.42)
                            width = min(
                                size.width() - x - 3.0,
                                max(2.0, len(stripped) * 0.48),
                            )
                            painter.drawLine(QPointF(x, y), QPointF(x + width, y))
                    visual_line += 1
                block = block.next()
        painter.end()
        return image

    def _minimap_viewport_rect(self) -> QRectF:
        width = float(self._minimap.width())
        height = float(self._minimap.height())
        if width <= 0.0 or height <= 0.0:
            return QRectF()

        scroll_bar = self.verticalScrollBar()
        minimum, maximum = self._minimap_scroll_bounds()
        scroll_range = maximum - minimum
        if scroll_range <= 0:
            return QRectF(1.0, 0.0, max(0.0, width - 2.0), height)

        page_step = max(1, scroll_bar.pageStep())
        indicator_height = height * page_step / (scroll_range + page_step)
        indicator_height = min(
            height,
            max(self._MINIMAP_MIN_INDICATOR_HEIGHT, indicator_height),
        )
        track_height = max(0.0, height - indicator_height)
        ratio = (scroll_bar.value() - minimum) / scroll_range
        top = max(0.0, min(track_height, ratio * track_height))
        return QRectF(
            1.0,
            top,
            max(0.0, width - 2.0),
            indicator_height,
        )

    def _scroll_from_minimap(self, y: float, drag_offset: float) -> None:
        scroll_bar = self.verticalScrollBar()
        minimum, maximum = self._minimap_scroll_bounds()
        scroll_range = maximum - minimum
        indicator = self._minimap_viewport_rect()
        track_height = self._minimap.height() - indicator.height()
        if scroll_range <= 0 or track_height <= 0.0:
            return

        top = max(0.0, min(track_height, y - drag_offset))
        value = minimum + round(top / track_height * scroll_range)
        scroll_bar.setValue(value)

    def _minimap_scroll_bounds(self) -> tuple[int, int]:
        scroll_bar = self.verticalScrollBar()
        minimum = scroll_bar.minimum()
        if self._minimap_visible_line_count is None:
            visible_line_count = 0
            block = self.document().firstBlock()
            while block.isValid():
                if block.isVisible():
                    visible_line_count += max(1, block.lineCount())
                block = block.next()
            self._minimap_visible_line_count = max(1, visible_line_count)

        page_step = max(1, scroll_bar.pageStep())
        content_maximum = minimum + max(
            0, self._minimap_visible_line_count - page_step
        )
        return minimum, min(scroll_bar.maximum(), content_maximum)

    def _paint_gutter(self, event: QPaintEvent) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor("#f5f7fa"))
        painter.setPen(QColor("#687585"))

        width = self._gutter.width()
        number_width = width - self._FOLD_COLUMN_WIDTH - self._GUTTER_PADDING
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            height = round(self.blockBoundingRect(block).height())
            bottom = top + height
            if block.isVisible() and bottom >= event.rect().top():
                block_number = block.blockNumber()
                painter.drawText(
                    0,
                    top,
                    number_width,
                    height,
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    str(block_number + 1),
                )
                if block_number in self._fold_regions:
                    option = QStyleOption()
                    arrow_size = min(12, max(8, height - 4))
                    option.rect = QRect(
                        width - self._FOLD_COLUMN_WIDTH
                        + (self._FOLD_COLUMN_WIDTH - arrow_size) // 2,
                        top + (height - arrow_size) // 2,
                        arrow_size,
                        arrow_size,
                    )
                    option.state = QStyle.StateFlag.State_Enabled
                    primitive = (
                        QStyle.PrimitiveElement.PE_IndicatorArrowRight
                        if block_number in self._folded_lines
                        else QStyle.PrimitiveElement.PE_IndicatorArrowDown
                    )
                    self.style().drawPrimitive(primitive, option, painter, self._gutter)
            block = block.next()
            top = bottom

        painter.setPen(QColor("#d8dee8"))
        painter.drawLine(width - 1, event.rect().top(), width - 1, event.rect().bottom())

    def _handle_gutter_click(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if event.position().x() < self._gutter.width() - self._FOLD_COLUMN_WIDTH:
            return False

        line = self._visible_line_at_y(round(event.position().y()))
        if line is None or line not in self._fold_regions:
            return False
        self.toggle_fold(line)
        return True

    def _visible_line_at_y(self, y: int) -> int | None:
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= y:
            height = round(self.blockBoundingRect(block).height())
            bottom = top + height
            if block.isVisible() and top <= y < bottom:
                return block.blockNumber()
            block = block.next()
            top = bottom
        return None

    def _clear_fold_state(self) -> None:
        self._folded_lines.clear()
        self._fold_regions.clear()
        self._show_all_blocks()
        self._gutter.update()

    def _show_all_blocks(self) -> None:
        block = self.document().firstBlock()
        changed = False
        while block.isValid():
            if not block.isVisible() or block.lineCount() == 0:
                block.setVisible(True)
                block.setLineCount(1)
                changed = True
            block = block.next()
        if changed:
            self._refresh_fold_layout()

    def _apply_fold_visibility(self) -> None:
        self._show_all_blocks()
        changed = False
        for start_line in sorted(self._folded_lines):
            end_line = self._fold_regions.get(start_line)
            if end_line is None:
                continue
            block = self.document().findBlockByNumber(start_line + 1)
            while block.isValid() and block.blockNumber() <= end_line:
                if block.isVisible() or block.lineCount() != 0:
                    block.setVisible(False)
                    block.setLineCount(0)
                    changed = True
                block = block.next()
        if changed:
            self._refresh_fold_layout()
        self._gutter.update()

    def _refresh_fold_layout(self) -> None:
        document = self.document()
        document.markContentsDirty(0, document.characterCount())
        document.adjustSize()
        self.viewport().update()
        self._gutter.update()
        self._minimap.invalidate()

    def _move_selection_out_of_fold(self, start_line: int, end_line: int) -> None:
        first_hidden = self.document().findBlockByNumber(start_line + 1)
        final_hidden = self.document().findBlockByNumber(end_line)
        if not first_hidden.isValid() or not final_hidden.isValid():
            return

        cursor = self.textCursor()
        hidden_start = first_hidden.position()
        hidden_end = final_hidden.position() + final_hidden.length()
        selection_start = min(cursor.position(), cursor.anchor())
        selection_end = max(cursor.position(), cursor.anchor())
        if selection_end < hidden_start or selection_start >= hidden_end:
            return
        self.setTextCursor(QTextCursor(self.document().findBlockByNumber(start_line)))


def _utf16_length(text: str) -> int:
    """Return QString-compatible length for QSyntaxHighlighter ranges."""

    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


__all__ = ["XmlCodeEditor", "XmlSyntaxHighlighter"]
