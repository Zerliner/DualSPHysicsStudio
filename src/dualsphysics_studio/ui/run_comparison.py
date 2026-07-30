"""Run-history comparison dialog."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..pipeline.compare import RunComparison, compare_sessions
from ..pipeline.session import RunSession


class RunComparisonDialog(QDialog):
    def __init__(
        self,
        sessions: Iterable[RunSession],
        *,
        selected_record: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行记录对比")
        self.resize(1080, 620)
        self.sessions = tuple(sessions)
        self.comparison: RunComparison | None = None

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("基线", self))
        self.baseline_combo = QComboBox(self)
        self.baseline_combo.setObjectName("comparison_baseline_combo")
        for index, session in enumerate(self.sessions):
            created = session.created_at[:19].replace("T", " ")
            self.baseline_combo.addItem(
                f"{created} · {session.backend.upper()} · {session.session_id[-8:]}",
                index,
            )
            if selected_record and str(session.record_path) == selected_record:
                self.baseline_combo.setCurrentIndex(index)
        controls.addWidget(self.baseline_combo, 1)
        self.compatible_only = QCheckBox("仅显示相同 XML SHA 与相同后端", self)
        self.compatible_only.setObjectName("comparison_compatible_only")
        self.compatible_only.setChecked(True)
        controls.addWidget(self.compatible_only)
        layout.addLayout(controls)

        self.notice = QLabel(self)
        self.notice.setObjectName("comparison_notice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        self.table = QTableWidget(self)
        self.table.setObjectName("run_comparison_table")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.export_button = QPushButton("导出 CSV…", self)
        self.export_button.setObjectName("export_comparison_csv")
        buttons.addWidget(self.export_button)
        buttons.addStretch(1)
        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.baseline_combo.currentIndexChanged.connect(self._refresh)
        self.compatible_only.toggled.connect(self._refresh)
        self.export_button.clicked.connect(self._export_csv)
        self._refresh()

    def _refresh(self) -> None:
        index = self.baseline_combo.currentIndex()
        if not 0 <= index < len(self.sessions):
            self.comparison = None
            self.table.clear()
            self.export_button.setEnabled(False)
            return
        baseline = self.sessions[index]
        comparison = compare_sessions(
            baseline,
            self.sessions,
            compatible_only=self.compatible_only.isChecked(),
        )
        self.comparison = comparison
        self.export_button.setEnabled(True)
        if comparison.excluded_count:
            self.notice.setText(
                f"已排除 {comparison.excluded_count} 条 XML SHA 或求解后端不同的记录。"
                "取消筛选可查看，但这些列不会被视为可直接比较。"
            )
        elif len(comparison.columns) == 1:
            self.notice.setText("没有其他兼容记录；可取消筛选查看不可直接比较的会话。")
        else:
            self.notice.setText(
                "括号中的百分比为相对基线变化；正值表示数值增加，负值表示减少。"
            )

        self.table.clear()
        self.table.setRowCount(len(comparison.rows))
        self.table.setColumnCount(1 + len(comparison.columns))
        self.table.setHorizontalHeaderLabels(
            ["指标", *(column.label for column in comparison.columns)]
        )
        for row_index, row in enumerate(comparison.rows):
            label = QTableWidgetItem(row.label)
            label.setToolTip(row.unit)
            self.table.setItem(row_index, 0, label)
            for column_index, column in enumerate(comparison.columns):
                item = QTableWidgetItem(
                    comparison.formatted_value(row, column_index)
                )
                if not column.compatible:
                    item.setToolTip(column.compatibility_note)
                    item.setForeground(Qt.GlobalColor.darkYellow)
                self.table.setItem(row_index, column_index + 1, item)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

    def _export_csv(self) -> None:
        if self.comparison is None:
            return
        default_path = (
            self.comparison.baseline.output_root
            / f"run-comparison-{self.comparison.baseline.session_id}.csv"
        )
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "导出运行对比",
            str(default_path),
            "CSV 表格 (*.csv)",
        )
        if not selected:
            return
        target = Path(selected)
        if target.suffix.casefold() != ".csv":
            target = target.with_suffix(".csv")
        try:
            target.write_text(self.comparison.to_csv(), encoding="utf-8-sig")
        except OSError as error:
            QMessageBox.critical(self, "导出失败", str(error))
            return
        QMessageBox.information(self, "导出完成", f"运行对比已导出：\n{target}")


__all__ = ["RunComparisonDialog"]
