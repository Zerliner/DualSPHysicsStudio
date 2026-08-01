"""Tkinter frontend for the standalone DSS Mesh Doctor."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Sequence

from dualsphysics_studio.core.mesh_guard import (
    MeshPreflightReport,
    inspect_mesh_file,
)


APP_NAME = "DSS Mesh Doctor"
APP_VERSION = "1.0.0"


def write_mesh_report(
    report: MeshPreflightReport,
    destination: str | os.PathLike[str],
) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


class MeshDoctorWindow(tk.Tk):
    def __init__(self, initial_path: str = "") -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("920x680")
        self.minsize(720, 500)
        self.report: MeshPreflightReport | None = None
        self.path_value = tk.StringVar(value=initial_path)
        self.status_value = tk.StringVar(
            value="选择 STL 或 legacy VTK 文件后执行只读预检。"
        )
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)
        ttk.Label(top, text="网格文件").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.path_value).pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=8,
        )
        ttk.Button(top, text="浏览…", command=self._browse).pack(side=tk.LEFT)
        self.inspect_button = ttk.Button(
            top,
            text="开始预检",
            command=self._start,
        )
        self.inspect_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            self,
            textvariable=self.status_value,
            padding=(10, 0, 10, 8),
        ).pack(fill=tk.X)
        self.output = tk.Text(self, wrap=tk.WORD, state=tk.DISABLED)
        self.output.pack(fill=tk.BOTH, expand=True, padx=10)

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
            text="只读诊断：不会修复、覆盖或重新保存源网格。",
        ).pack(side=tk.RIGHT)

    def _browse(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="选择 STL 或 legacy VTK",
            initialdir=str(Path(self.path_value.get()).parent)
            if self.path_value.get()
            else None,
            filetypes=(
                ("支持的网格", "*.stl *.vtk"),
                ("STL", "*.stl"),
                ("Legacy VTK", "*.vtk"),
            ),
        )
        if selected:
            self.path_value.set(selected)

    def _start(self) -> None:
        path = self.path_value.get().strip()
        if not path:
            messagebox.showwarning(APP_NAME, "请先选择网格文件。", parent=self)
            return
        self.inspect_button.configure(state=tk.DISABLED)
        self.export_button.configure(state=tk.DISABLED)
        self.status_value.set("正在执行有界只读预检…")
        threading.Thread(
            target=self._worker,
            args=(path,),
            daemon=True,
        ).start()

    def _worker(self, path: str) -> None:
        try:
            report = inspect_mesh_file(path)
        except Exception as error:
            message = str(error)
            self.after(0, lambda: self._failed(message))
            return
        self.after(0, lambda: self._finished(report))

    def _failed(self, message: str) -> None:
        self.inspect_button.configure(state=tk.NORMAL)
        self.status_value.set("预检失败。")
        messagebox.showerror(APP_NAME, message, parent=self)

    def _finished(self, report: MeshPreflightReport) -> None:
        self.report = report
        self.inspect_button.configure(state=tk.NORMAL)
        self.export_button.configure(state=tk.NORMAL)
        quality = (
            report.quality.short_status()
            if report.quality is not None
            else "无拓扑结论"
        )
        self.status_value.set(
            f"完成：资源风险 {report.risk.value}；{quality}。"
        )
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.insert(
            "1.0",
            f"{report.path}\n\n{report.summary()}",
        )
        self.output.configure(state=tk.DISABLED)

    def _export(self) -> None:
        if self.report is None:
            return
        default = self.report.path.with_suffix(".mesh-doctor.json")
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="导出 Mesh Doctor 报告",
            initialdir=str(default.parent),
            initialfile=default.name,
            defaultextension=".json",
            filetypes=(("JSON 报告", "*.json"),),
        )
        if not selected:
            return
        try:
            target = write_mesh_report(self.report, selected)
        except OSError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        messagebox.showinfo(APP_NAME, f"报告已导出：\n{target}", parent=self)


def _smoke_test() -> int:
    with tempfile.TemporaryDirectory(prefix="dss-mesh-doctor-") as temporary:
        path = Path(temporary) / "triangle.stl"
        path.write_text(
            "solid test\n"
            "facet normal 0 0 1\n"
            "outer loop\n"
            "vertex 0 0 0\n"
            "vertex 1 0 0\n"
            "vertex 0 1 0\n"
            "endloop\n"
            "endfacet\n"
            "endsolid test\n",
            encoding="ascii",
        )
        report = inspect_mesh_file(path)
        if (
            report.quality is None
            or not report.quality.analyzed
            or report.quality.boundary_edges != 3
        ):
            raise RuntimeError("Mesh Doctor smoke test failed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="DSSMeshDoctor")
    parser.add_argument("mesh", nargs="?")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return _smoke_test()
    window = MeshDoctorWindow(args.mesh or "")
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
