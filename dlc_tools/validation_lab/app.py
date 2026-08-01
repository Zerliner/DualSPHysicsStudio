"""Tkinter frontend for DSS Validation Lab."""

from __future__ import annotations

import argparse
from pathlib import Path
from threading import Thread
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Sequence

try:
    from .validation import (
        ValidationError,
        ValidationReport,
        align_series,
        create_report,
        load_csv_series,
        read_csv_headers,
        write_validation_bundle,
    )
except ImportError:  # PyInstaller/script entry point
    from validation import (  # type: ignore[no-redef]
        ValidationError,
        ValidationReport,
        align_series,
        create_report,
        load_csv_series,
        read_csv_headers,
        write_validation_bundle,
    )


APP_NAME = "DSS Validation Lab"
APP_VERSION = "1.0.0"


class DatasetPanel(ttk.LabelFrame):
    def __init__(self, parent, *, title: str) -> None:
        super().__init__(parent, text=title, padding=8)
        self.path_value = tk.StringVar()
        self.time_value = tk.StringVar()
        self.data_value = tk.StringVar()
        self.time_scale_value = tk.StringVar(value="1")
        self.value_scale_value = tk.StringVar(value="1")
        self.offset_value = tk.StringVar(value="0")

        ttk.Label(self, text="CSV 文件").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(self, textvariable=self.path_value).grid(
            row=0,
            column=1,
            columnspan=5,
            sticky=tk.EW,
            padx=8,
        )
        ttk.Button(self, text="浏览并读取列…", command=self._browse).grid(
            row=0,
            column=6,
        )
        ttk.Label(self, text="时间列").grid(
            row=1,
            column=0,
            sticky=tk.W,
            pady=(8, 0),
        )
        self.time_combo = ttk.Combobox(
            self,
            textvariable=self.time_value,
            state="readonly",
            width=18,
        )
        self.time_combo.grid(
            row=1,
            column=1,
            sticky=tk.EW,
            padx=(8, 16),
            pady=(8, 0),
        )
        ttk.Label(self, text="数值列").grid(
            row=1,
            column=2,
            sticky=tk.W,
            pady=(8, 0),
        )
        self.data_combo = ttk.Combobox(
            self,
            textvariable=self.data_value,
            state="readonly",
            width=18,
        )
        self.data_combo.grid(
            row=1,
            column=3,
            sticky=tk.EW,
            padx=(8, 16),
            pady=(8, 0),
        )
        ttk.Label(self, text="时间倍率").grid(
            row=2,
            column=0,
            sticky=tk.W,
            pady=(8, 0),
        )
        ttk.Entry(
            self,
            textvariable=self.time_scale_value,
            width=12,
        ).grid(row=2, column=1, sticky=tk.W, padx=(8, 16), pady=(8, 0))
        ttk.Label(self, text="数值倍率").grid(
            row=2,
            column=2,
            sticky=tk.W,
            pady=(8, 0),
        )
        ttk.Entry(
            self,
            textvariable=self.value_scale_value,
            width=12,
        ).grid(row=2, column=3, sticky=tk.W, padx=(8, 16), pady=(8, 0))
        ttk.Label(self, text="数值偏移").grid(
            row=2,
            column=4,
            sticky=tk.W,
            pady=(8, 0),
        )
        ttk.Entry(
            self,
            textvariable=self.offset_value,
            width=12,
        ).grid(row=2, column=5, sticky=tk.W, padx=(8, 0), pady=(8, 0))
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

    def _browse(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title=f"选择 {self.cget('text')} CSV",
            initialdir=str(Path(self.path_value.get()).parent)
            if self.path_value.get()
            else None,
            filetypes=(("CSV 数据", "*.csv"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        self.path_value.set(selected)
        try:
            headers, _delimiter, _encoding = read_csv_headers(selected)
        except ValidationError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        self.time_combo.configure(values=headers)
        self.data_combo.configure(values=headers)
        self.time_value.set(_default_time_column(headers))
        self.data_value.set(_default_value_column(headers))

    def options(self) -> dict[str, Any]:
        try:
            return {
                "source": self.path_value.get().strip(),
                "time_column": self.time_value.get().strip(),
                "value_column": self.data_value.get().strip(),
                "time_scale": float(self.time_scale_value.get().strip()),
                "value_scale": float(self.value_scale_value.get().strip()),
                "value_offset": float(self.offset_value.get().strip()),
            }
        except ValueError as error:
            raise ValidationError(
                f"{self.cget('text')} 的倍率和偏移必须是数字。"
            ) from error


class ValidationLabWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1120x820")
        self.minsize(900, 700)
        self.report_name = tk.StringVar(value="DualSPHysics 仿真验证")
        self.unit_value = tk.StringVar()
        self.start_value = tk.StringVar()
        self.end_value = tk.StringVar()
        self.output_value = tk.StringVar()
        self.status_value = tk.StringVar(
            value="选择参考 CSV 与仿真 CSV，并明确各自的时间列和数值列。"
        )
        self.last_output: Path | None = None
        self._build()

    def _build(self) -> None:
        container = ttk.Frame(self, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        self.reference_panel = DatasetPanel(container, title="参考 / 实测数据")
        self.reference_panel.pack(fill=tk.X)
        self.simulation_panel = DatasetPanel(container, title="仿真数据")
        self.simulation_panel.pack(fill=tk.X, pady=(8, 0))

        settings = ttk.LabelFrame(container, text="对比设置", padding=8)
        settings.pack(fill=tk.X, pady=(8, 0))
        labels = ("报告名称", "单位", "起始时间（可空）", "结束时间（可空）")
        variables = (
            self.report_name,
            self.unit_value,
            self.start_value,
            self.end_value,
        )
        for index, (label, variable) in enumerate(zip(labels, variables)):
            ttk.Label(settings, text=label).grid(
                row=0,
                column=index * 2,
                sticky=tk.W,
            )
            ttk.Entry(settings, textvariable=variable).grid(
                row=0,
                column=index * 2 + 1,
                sticky=tk.EW,
                padx=(6, 16 if index < 3 else 0),
            )
            settings.columnconfigure(index * 2 + 1, weight=1)
        ttk.Label(settings, text="输出父目录").grid(
            row=1,
            column=0,
            sticky=tk.W,
            pady=(8, 0),
        )
        ttk.Entry(settings, textvariable=self.output_value).grid(
            row=1,
            column=1,
            columnspan=6,
            sticky=tk.EW,
            padx=(6, 16),
            pady=(8, 0),
        )
        ttk.Button(
            settings,
            text="浏览…",
            command=self._browse_output,
        ).grid(row=1, column=7, pady=(8, 0))

        actions = ttk.Frame(container)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(actions, textvariable=self.status_value).pack(side=tk.LEFT)
        self.open_button = ttk.Button(
            actions,
            text="打开报告目录",
            command=self._open_output,
            state=tk.DISABLED,
        )
        self.open_button.pack(side=tk.RIGHT)
        self.run_button = ttk.Button(
            actions,
            text="生成验证报告",
            command=self._start,
        )
        self.run_button.pack(side=tk.RIGHT, padx=(0, 8))

        results = ttk.Panedwindow(container, orient=tk.HORIZONTAL)
        results.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        metric_frame = ttk.LabelFrame(results, text="指标", padding=6)
        warning_frame = ttk.LabelFrame(
            results,
            text="数据质量与方法",
            padding=6,
        )
        results.add(metric_frame, weight=3)
        results.add(warning_frame, weight=2)
        self.metric_table = ttk.Treeview(
            metric_frame,
            columns=("value",),
            show="tree headings",
        )
        self.metric_table.heading("#0", text="指标")
        self.metric_table.heading("value", text="结果")
        self.metric_table.column("#0", width=210, stretch=True)
        self.metric_table.column("value", width=220, stretch=True)
        self.metric_table.pack(fill=tk.BOTH, expand=True)
        self.warning_text = tk.Text(
            warning_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        self.warning_text.pack(fill=tk.BOTH, expand=True)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="选择 Validation Lab 输出父目录",
            initialdir=self.output_value.get() or None,
        )
        if selected:
            self.output_value.set(selected)

    def _start(self) -> None:
        try:
            reference_options = self.reference_panel.options()
            simulation_options = self.simulation_panel.options()
            start = _optional_float(self.start_value.get(), "起始时间")
            end = _optional_float(self.end_value.get(), "结束时间")
            output = Path(self.output_value.get()).expanduser().resolve(
                strict=True
            )
            if not output.is_dir():
                raise ValidationError("输出父路径不是文件夹。")
            if not self.report_name.get().strip():
                raise ValidationError("请填写报告名称。")
        except (OSError, ValidationError) as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        self.run_button.configure(state=tk.DISABLED)
        self.open_button.configure(state=tk.DISABLED)
        self.status_value.set("正在读取、对齐并分析数据…")
        name = self.report_name.get()
        unit = self.unit_value.get()
        Thread(
            target=self._worker,
            args=(
                reference_options,
                simulation_options,
                start,
                end,
                name,
                unit,
                str(output),
            ),
            daemon=True,
        ).start()

    def _worker(
        self,
        reference_options: dict[str, Any],
        simulation_options: dict[str, Any],
        start: float | None,
        end: float | None,
        name: str,
        unit: str,
        output: str,
    ) -> None:
        try:
            reference = load_csv_series(
                **reference_options,
                role="reference",
            )
            simulation = load_csv_series(
                **simulation_options,
                role="simulation",
            )
            aligned = align_series(
                reference,
                simulation,
                start_time=start,
                end_time=end,
            )
            report = create_report(
                name,
                reference,
                simulation,
                aligned,
                unit=unit,
            )
            folder = write_validation_bundle(report, aligned, output)
        except (OSError, ValidationError) as error:
            message = str(error)
            self.after(0, lambda: self._failed(message))
            return
        self.after(0, lambda: self._finished(report, folder))

    def _failed(self, message: str) -> None:
        self.run_button.configure(state=tk.NORMAL)
        self.status_value.set("验证报告生成失败。")
        messagebox.showerror(APP_NAME, message, parent=self)

    def _finished(self, report: ValidationReport, folder: Path) -> None:
        self.run_button.configure(state=tk.NORMAL)
        self.open_button.configure(state=tk.NORMAL)
        self.last_output = folder
        self.status_value.set(f"已生成可复核验证报告：{folder.name}")
        self._show_report(report)
        messagebox.showinfo(
            APP_NAME,
            "验证报告已生成。\n\n"
            f"{folder}\n\n"
            "目录包含 JSON 指标、对齐 CSV 和离线 HTML 报告。",
            parent=self,
        )

    def _show_report(self, report: ValidationReport) -> None:
        for item in self.metric_table.get_children():
            self.metric_table.delete(item)
        metrics = report.metrics
        values = (
            ("样本数", f"{metrics.samples:,}"),
            ("时间窗口", f"{metrics.start_time:.8g} — {metrics.end_time:.8g}"),
            ("MAE", _metric(metrics.mae, report.unit)),
            ("RMSE", _metric(metrics.rmse, report.unit)),
            ("Bias", _metric(metrics.bias, report.unit)),
            ("最大绝对误差", _metric(metrics.max_abs_error, report.unit)),
            (
                "相关系数",
                f"{metrics.correlation:.6g}"
                if metrics.correlation is not None
                else "不可用",
            ),
            (
                "归一化 RMSE",
                f"{metrics.normalized_rmse:.4%}"
                if metrics.normalized_rmse is not None
                else "不可用",
            ),
            (
                "参考绝对峰值",
                f"{_metric(metrics.reference_abs_peak, report.unit)}"
                f" @ {metrics.reference_abs_peak_time:.8g}",
            ),
            (
                "仿真绝对峰值",
                f"{_metric(metrics.simulation_abs_peak, report.unit)}"
                f" @ {metrics.simulation_abs_peak_time:.8g}",
            ),
            (
                "绝对峰值数值误差",
                _metric(metrics.abs_peak_value_error, report.unit),
            ),
            (
                "绝对峰值时间误差",
                f"{metrics.abs_peak_time_error:.8g}",
            ),
        )
        for label, value in values:
            self.metric_table.insert("", tk.END, text=label, values=(value,))
        quality = "\n".join(
            (
                report.interpolation,
                "",
                *(f"• {warning}" for warning in report.warnings),
            )
        )
        if not report.warnings:
            quality += "\n• 未产生数据质量警告。"
        self.warning_text.configure(state=tk.NORMAL)
        self.warning_text.delete("1.0", tk.END)
        self.warning_text.insert("1.0", quality)
        self.warning_text.configure(state=tk.DISABLED)

    def _open_output(self) -> None:
        if self.last_output is None:
            return
        try:
            import os

            os.startfile(self.last_output)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)


def _default_time_column(headers: tuple[str, ...]) -> str:
    for header in headers:
        if header.casefold() in {"time", "t", "timestamp", "时间", "时刻"}:
            return header
    return headers[0]


def _default_value_column(headers: tuple[str, ...]) -> str:
    time_column = _default_time_column(headers)
    for header in headers:
        if header != time_column:
            return header
    return headers[0]


def _optional_float(value: str, label: str) -> float | None:
    if not value.strip():
        return None
    try:
        return float(value)
    except ValueError as error:
        raise ValidationError(f"{label}必须是数字或留空。") from error


def _metric(value: float, unit: str) -> str:
    return f"{value:.8g}{f' {unit}' if unit else ''}"


def _smoke_test() -> int:
    with tempfile.TemporaryDirectory(prefix="dss-validation-") as temporary:
        root = Path(temporary)
        reference_path = root / "reference.csv"
        simulation_path = root / "simulation.csv"
        reference_path.write_text(
            "time,value\n0,0\n1,1\n2,4\n",
            encoding="utf-8",
        )
        simulation_path.write_text(
            "time,value\n0,0\n2,4\n",
            encoding="utf-8",
        )
        reference = load_csv_series(
            reference_path,
            role="reference",
            time_column="time",
            value_column="value",
        )
        simulation = load_csv_series(
            simulation_path,
            role="simulation",
            time_column="time",
            value_column="value",
        )
        aligned = align_series(reference, simulation)
        report = create_report("smoke", reference, simulation, aligned)
        folder = write_validation_bundle(report, aligned, root)
        expected = {
            "comparison.csv",
            "report.html",
            "validation.json",
        }
        if {path.name for path in folder.iterdir()} != expected:
            raise RuntimeError("Validation Lab smoke test failed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="DSSValidationLab")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return _smoke_test()
    window = ValidationLabWindow()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
