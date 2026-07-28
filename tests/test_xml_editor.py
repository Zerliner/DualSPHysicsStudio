from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QTextBlock, QTextCursor
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import delete

from dualsphysics_studio.ui.xml_editor import XmlCodeEditor


NESTED_XML = "\n".join(
    (
        "<case>",
        "    <group",
        '        name="中文">',
        "        <!-- <fake> 不应折叠 -->",
        "        <child>",
        "            <leaf/>",
        "        </child>",
        "    </group>",
        "    <sibling/>",
        "</case>",
    )
)


@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def editor(qapp: QApplication) -> Iterator[XmlCodeEditor]:
    widget = XmlCodeEditor()
    widget.resize(720, 420)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    delete(widget)
    qapp.processEvents()


def _block(editor: XmlCodeEditor, line: int) -> QTextBlock:
    block = editor.document().findBlockByNumber(line)
    assert block.isValid(), f"line {line} does not exist"
    return block


def _foreground_at(block: QTextBlock, position: int) -> QColor:
    for text_range in block.layout().formats():
        if text_range.start <= position < text_range.start + text_range.length:
            color = text_range.format.foreground().color()
            if color.isValid():
                return color
    raise AssertionError(
        f"no syntax foreground at position {position} in {block.text()!r}"
    )


def _colors_for(block: QTextBlock, text: str) -> list[QColor]:
    start = block.text().index(text)
    return [_foreground_at(block, position) for position in range(start, start + len(text))]


def _is_green(color: QColor) -> bool:
    return color.green() > color.red() and color.green() > color.blue()


def _is_blue(color: QColor) -> bool:
    return color.blue() > color.red() and color.blue() > color.green()


def _all_blocks(editor: XmlCodeEditor) -> list[QTextBlock]:
    blocks: list[QTextBlock] = []
    block = editor.document().firstBlock()
    while block.isValid():
        blocks.append(block)
        block = block.next()
    return blocks


def test_multiline_comments_are_green_and_override_fake_tags(
    editor: XmlCodeEditor, qapp: QApplication
) -> None:
    source = "\n".join(
        (
            '<case value="1">',
            "    <!-- 中文注释中的 <fake>",
            "         仍然属于注释 -->",
            "</case>",
        )
    )
    editor.setPlainText(source)
    qapp.processEvents()

    first_comment = _block(editor, 1)
    second_comment = _block(editor, 2)
    comment_colors = _colors_for(first_comment, "<!-- 中文注释中的 <fake>")
    comment_colors.extend(_colors_for(second_comment, "仍然属于注释 -->"))

    assert all(_is_green(color) for color in comment_colors)
    assert len({color.name(QColor.NameFormat.HexRgb) for color in comment_colors}) == 1
    assert all(_is_green(color) for color in _colors_for(first_comment, "<fake>"))

    opening_tag = _block(editor, 0)
    closing_tag = _block(editor, 3)
    assert all(_is_blue(color) for color in _colors_for(opening_tag, "<case"))
    assert _is_blue(_foreground_at(opening_tag, opening_tag.text().rindex(">")))
    assert all(_is_blue(color) for color in _colors_for(closing_tag, "</case>"))


def test_line_number_area_width_grows_at_digit_boundaries(
    editor: XmlCodeEditor,
) -> None:
    widths: dict[int, int] = {}
    for line_count in (9, 10, 99, 100):
        editor.setPlainText("\n".join("x" for _ in range(line_count)))
        assert editor.blockCount() == line_count
        widths[line_count] = editor.line_number_area_width()

    assert widths[10] > widths[9]
    assert widths[100] > widths[99]


def test_tabs_and_four_spaces_produce_the_same_indent_guides(
    editor: XmlCodeEditor,
) -> None:
    one_tab = editor.indent_guide_columns("\t<node/>")
    four_spaces = editor.indent_guide_columns("    <node/>")
    two_tabs = editor.indent_guide_columns("\t\t<node/>")
    eight_spaces = editor.indent_guide_columns("        <node/>")
    mixed = editor.indent_guide_columns("\t    <node/>")

    assert one_tab == four_spaces
    assert two_tabs == eight_spaces == mixed
    assert len(one_tab) == 1
    assert len(two_tabs) == 2


def test_nested_multiline_regions_fold_without_modifying_source(
    editor: XmlCodeEditor,
) -> None:
    editor.setPlainText(NESTED_XML)
    editor.document().setModified(False)
    changes: list[str] = []
    editor.textChanged.connect(lambda: changes.append(editor.toPlainText()))

    assert editor.fold_regions[0] == 9
    assert editor.fold_regions[1] == 7
    assert editor.fold_regions[4] == 6
    assert editor.is_foldable(0)
    assert editor.is_foldable(1)
    assert editor.is_foldable(4)
    assert not editor.is_foldable(3)  # XML comment containing a fake tag.
    assert not editor.is_foldable(5)  # Self-closing leaf.
    assert not editor.is_foldable(8)  # Self-closing sibling.

    original_blocks = _all_blocks(editor)
    editor.toggle_fold(1)

    assert editor.is_folded(1)
    assert original_blocks[1].isVisible()
    assert all(not original_blocks[line].isVisible() for line in range(2, 8))
    assert original_blocks[8].isVisible()
    assert original_blocks[8].blockNumber() == 8
    assert editor.toPlainText() == NESTED_XML
    assert not editor.document().isModified()
    assert changes == []

    editor.toggle_fold(1)

    assert not editor.is_folded(1)
    assert all(block.isVisible() for block in original_blocks)
    assert editor.toPlainText() == NESTED_XML
    assert not editor.document().isModified()
    assert changes == []


def test_replacing_or_editing_text_clears_folds_and_restores_blocks(
    editor: XmlCodeEditor,
) -> None:
    editor.setPlainText(NESTED_XML)
    editor.toggle_fold(1)
    assert editor.is_folded(1)

    replacement = "<root>\n    <item/>\n</root>"
    editor.setPlainText(replacement)

    assert editor.toPlainText() == replacement
    assert not editor.is_folded(0)
    assert all(block.isVisible() for block in _all_blocks(editor))
    assert editor.fold_regions[0] == 2

    editor.toggle_fold(0)
    assert editor.is_folded(0)
    assert any(not block.isVisible() for block in _all_blocks(editor)[1:])

    cursor = QTextCursor(editor.document())
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
    cursor.insertText(" ")

    assert not editor.is_folded(0)
    assert all(block.isVisible() for block in _all_blocks(editor))


def test_incomplete_xml_is_safe_to_display_and_query(
    editor: XmlCodeEditor, qapp: QApplication
) -> None:
    source = '<case>\n    <!-- 中文 <fake>\n    <broken attr="1"\n'
    editor.setPlainText(source)
    qapp.processEvents()

    for line in range(editor.blockCount()):
        editor.is_foldable(line)
        editor.is_folded(line)
        _block(editor, line).layout().formats()
    editor.toggle_fold(0)

    assert editor.toPlainText() == source


def test_true_blank_lines_inherit_only_the_common_neighbor_guides(
    editor: XmlCodeEditor,
) -> None:
    source = "\n".join(
        (
            "",
            "<root>",
            "    <group>",
            "        <first/>",
            "            ",
            "\t\t\t",
            "        <second/>",
            "",
            "    </group>",
            "</root>",
            "",
        )
    )
    editor.setPlainText(source)

    assert editor.indent_guide_columns_for_line(3) == (4, 8)
    assert editor.indent_guide_columns_for_line(4) == (4, 8)
    assert editor.indent_guide_columns_for_line(5) == (4, 8)
    assert editor.indent_guide_columns_for_line(6) == (4, 8)
    assert editor.indent_guide_columns_for_line(7) == (4,)
    assert editor.indent_guide_columns_for_line(0) == ()
    assert editor.indent_guide_columns_for_line(10) == ()
    assert editor.toPlainText() == source


def test_minimap_drag_respects_visible_range_after_large_fold(
    editor: XmlCodeEditor, qapp: QApplication
) -> None:
    source = "\n".join(
        (
            "<root>",
            "    <folded>",
            *(f'        <hidden id="{index}"/>' for index in range(100)),
            "    </folded>",
            *(f'    <visible id="{index}"/>' for index in range(900)),
            "</root>",
        )
    )
    editor.resize(720, 260)
    editor.setPlainText(source)
    editor.show()
    qapp.processEvents()
    editor.toggle_fold(1)
    qapp.processEvents()

    minimap = editor._minimap
    scroll_bar = editor.verticalScrollBar()
    effective_minimum, effective_maximum = editor._minimap_scroll_bounds()
    indicator = minimap.viewport_indicator_rect()
    start = indicator.center()
    end = QPointF(start.x(), minimap.height() - 1.0)

    _send_mouse_event(
        minimap,
        QEvent.Type.MouseButtonPress,
        start,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    _send_mouse_event(
        minimap,
        QEvent.Type.MouseMove,
        end,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
    )
    _send_mouse_event(
        minimap,
        QEvent.Type.MouseButtonRelease,
        end,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )
    qapp.processEvents()

    assert effective_minimum == scroll_bar.minimum()
    assert scroll_bar.value() == effective_maximum
    assert scroll_bar.value() <= scroll_bar.maximum()
    assert editor.firstVisibleBlock().isValid()
    assert editor.firstVisibleBlock().blockNumber() >= 900
    assert editor.toPlainText() == source


def _send_mouse_event(
    widget: QWidget,
    event_type: QEvent.Type,
    position: QPointF,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
) -> None:
    event = QMouseEvent(
        event_type,
        position,
        QPointF(widget.mapToGlobal(position.toPoint())),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def test_minimap_indicator_drag_scrolls_editor_without_editing_source(
    editor: XmlCodeEditor, qapp: QApplication
) -> None:
    source = "\n".join(
        ("<root>", *(f'    <item id="{index}"/>' for index in range(400)), "</root>")
    )
    editor.resize(720, 260)
    editor.setPlainText(source)
    editor.document().setModified(False)
    editor.show()
    qapp.processEvents()

    minimap = editor._minimap
    scroll_bar = editor.verticalScrollBar()
    scroll_bar.setValue(0)
    qapp.processEvents()
    changes: list[str] = []
    editor.textChanged.connect(lambda: changes.append(editor.toPlainText()))

    assert minimap.objectName() == "xml_minimap"
    assert minimap.isVisible()
    assert scroll_bar.maximum() > 0
    original_first_line = editor.firstVisibleBlock().blockNumber()
    original_indicator = minimap.viewport_indicator_rect()
    assert original_indicator.isValid()
    assert 0 < original_indicator.height() < minimap.height()

    start = original_indicator.center()
    end = QPointF(
        start.x(),
        min(
            minimap.height() - 1.0,
            minimap.height() - original_indicator.height() / 2.0,
        ),
    )
    assert end.y() > start.y()

    _send_mouse_event(
        minimap,
        QEvent.Type.MouseButtonPress,
        start,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    _send_mouse_event(
        minimap,
        QEvent.Type.MouseMove,
        end,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
    )
    _send_mouse_event(
        minimap,
        QEvent.Type.MouseButtonRelease,
        end,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )
    qapp.processEvents()

    moved_indicator = minimap.viewport_indicator_rect()
    assert scroll_bar.value() > scroll_bar.maximum() / 2
    assert editor.firstVisibleBlock().blockNumber() > original_first_line
    assert moved_indicator.top() > original_indicator.top()
    assert editor.toPlainText() == source
    assert not editor.document().isModified()
    assert changes == []
