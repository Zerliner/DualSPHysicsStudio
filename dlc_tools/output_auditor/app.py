"""Tkinter frontend for the independent DSS Output Auditor."""

from __future__ import annotations

import argparse
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Sequence

try:
    from .audit import AuditReport, audit_directory, write_report
except ImportError:  # PyInstaller/script entry point
    from audit import AuditReport, audit_directory, write_report


APP_NAME = "DSS 结果完整性审计器"
APP_VERSION = "1.0.0"


class AuditorWindow(tk.Tk):
    def __init__(self, initial_directory: str = "") -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("980x680")
        self.minsize(760, 520)
        self.report: AuditReport | None = None
        self.path_value = tk.StringVar(value=initial_directory)
        self.status_value = tk.StringVar(value="选择 DualSPHysics 输出目录后开始审计。")
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)
        ttk.Label(top, text="输出目录").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.path_value).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=8
        )
        ttk.Button(top, text="浏览…", command=self._browse).pack(side=tk.LEFT)
        self.audit_button = ttk.Button(top, text="开始审计", command=self._start_audit)
        self.audit_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            self,
            textvariable=self.status_value,
            padding=(10, 0, 10, 8),
        ).pack(fill=tk.X)

        panes = ttk.Panedwindow(self, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10)
        table_frame = ttk.Frame(panes)
        warning_frame = ttk.Frame(panes)
        panes.add(table_frame, weight=3)
        panes.add(warning_frame, weight=2)

        self.table = ttk.Treeview(
            table_frame,
            columns=("count", "size", "detail"),
            show="tree headings",
        )
        self.table.heading("#0", text="项目")
        self.table.heading("count", text="数量")
        self.table.heading("size", text="大小")
        self.table.heading("detail", text="完整性")
        self.table.column("#0", width=240, stretch=True)
        self.table.column("count", width=90, anchor=tk.E)
        self.table.column("size", width=130, anchor=tk.E)
        self.table.column("detail", width=360, stretch=True)
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self.table.yview,
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(warning_frame, text="警告与发现").pack(anchor=tk.W)
        self.warning_text = tk.Text(
            warning_frame,
            height=10,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        self.warning_text.pack(fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill=tk.X)
        self.export_button = ttk.Button(
            bottom,
            text="导出 JSON 报告…",
            command=self._export,
            state=tk.DISABLED,
        )
        self.export_button.pack(side=tk.LEFT)
        ttk.Label(
            bottom,
            text="只读工具：不会修改、删除或重命名任何结果文件。",
        ).pack(side=tk.RIGHT)

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="选择 DualSPHysics 输出目录",
            initialdir=self.path_value.get() or None,
        )
        if selected:
            self.path_value.set(selected)

    def _start_audit(self) -> None:
        directory = self.path_value.get().strip()
        if not directory:
            messagebox.showwarning(APP_NAME, "请先选择输出目录。", parent=self)
            return
        self.audit_button.configure(state=tk.DISABLED)
        self.export_button.configure(state=tk.DISABLED)
        self.status_value.set("正在只读扫描文件清单…")
        threading.Thread(
            target=self._audit_worker,
            args=(directory,),
            daemon=True,
        ).start()

    def _audit_worker(self, directory: str) -> None:
        try:
            report = audit_directory(directory)
        except Exception as error:
            self.after(0, lambda: self._audit_failed(str(error)))
            return
        self.after(0, lambda: self._audit_finished(report))

    def _audit_failed(self, message: str) -> None:
        self.audit_button.configure(state=tk.NORMAL)
        self.status_value.set("审计失败。")
        messagebox.showerror(APP_NAME, message, parent=self)

    def _audit_finished(self, report: AuditReport) -> None:
        self.report = report
        self.audit_button.configure(state=tk.NORMAL)
        self.export_button.configure(state=tk.NORMAL)
        self.status_value.set(
            f"完成：{report.total_files:,} 个文件，"
            f"{_format_bytes(report.total_bytes)}，"
            f"{len(report.warnings)} 项警告。"
        )
        for item in self.table.get_children():
            self.table.delete(item)
        category_root = self.table.insert(
            "",
            tk.END,
            text="文件类型",
            values=(report.total_files, _format_bytes(report.total_bytes), ""),
            open=True,
        )
        for category in report.categories:
            self.table.insert(
                category_root,
                tk.END,
                text=category.extension,
                values=(
                    f"{category.count:,}",
                    _format_bytes(category.bytes),
                    "",
                ),
            )
        sequence_root = self.table.insert(
            "",
            tk.END,
            text="Part/VTK 帧序列",
            values=(len(report.sequences), "", ""),
            open=True,
        )
        for sequence in report.sequences:
            detail = (
                f"{sequence.first_index}–{sequence.last_index}；"
                f"缺少 {sequence.missing_count}"
            )
            self.table.insert(
                sequence_root,
                tk.END,
                text=(
                    f"{sequence.directory or '.'}/"
                    f"{sequence.prefix}*{sequence.extension}"
                ),
                values=(sequence.frame_count, "", detail),
            )
        if report.session is not None:
            self.table.insert(
                "",
                tk.END,
                text="Studio 运行记录",
                values=(
                    "",
                    "",
                    f"{report.session['status']} · "
                    f"{report.session['backend'].upper()} · "
                    f"{report.session['session_id']}",
                ),
            )
        warning_text = (
            "\n".join(f"• {warning}" for warning in report.warnings)
            if report.warnings
            else "未发现文件级完整性警告。"
        )
        if report.zero_byte_files:
            warning_text += "\n\n零字节文件（最多显示 1000 项）：\n"
            warning_text += "\n".join(report.zero_byte_files)
        self.warning_text.configure(state=tk.NORMAL)
        self.warning_text.delete("1.0", tk.END)
        self.warning_text.insert("1.0", warning_text)
        self.warning_text.configure(state=tk.DISABLED)

    def _export(self) -> None:
        if self.report is None:
            return
        default = Path(self.report.root) / "dss-output-audit.json"
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="导出审计报告",
            initialdir=str(default.parent),
            initialfile=default.name,
            defaultextension=".json",
            filetypes=(("JSON 报告", "*.json"),),
        )
        if not selected:
            return
        try:
            target = write_report(self.report, selected)
        except OSError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        messagebox.showinfo(APP_NAME, f"报告已导出：\n{target}", parent=self)


def _format_bytes(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or suffix == "TiB":
            return f"{int(size)} B" if suffix == "B" else f"{size:.2f} {suffix}"
        size /= 1024
    return f"{value} B"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="DSSOutputAuditor")
    parser.add_argument("directory", nargs="?")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return 0
    window = AuditorWindow(args.directory or "")
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
