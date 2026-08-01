"""Tkinter frontend for DSS PostLab."""

from __future__ import annotations

import argparse
from pathlib import Path
from threading import Event, Thread
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Sequence

try:
    from .postlab import (
        DEFAULT_ARGUMENTS,
        KNOWN_TOOLS,
        PostLabError,
        PostLabRun,
        PostRecipe,
        ToolInstallation,
        discover_tools,
        load_recipe,
        preview_command,
        run_recipe,
        save_recipe,
    )
except ImportError:  # PyInstaller/script entry point
    from postlab import (  # type: ignore[no-redef]
        DEFAULT_ARGUMENTS,
        KNOWN_TOOLS,
        PostLabError,
        PostLabRun,
        PostRecipe,
        ToolInstallation,
        discover_tools,
        load_recipe,
        preview_command,
        run_recipe,
        save_recipe,
    )


APP_NAME = "DSS PostLab"
APP_VERSION = "1.0.0"


class PostLabWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1080x760")
        self.minsize(840, 620)
        self.root_value = tk.StringVar()
        self.input_value = tk.StringVar()
        self.output_value = tk.StringVar()
        self.recipe_name = tk.StringVar(value="PartVTK 全粒子 VTK")
        self.status_value = tk.StringVar(
            value="选择 DualSPHysics 根目录并扫描官方后处理工具。"
        )
        self.installations: dict[str, ToolInstallation] = {}
        self.cancel_event: Event | None = None
        self._build()
        self.tool_combo.current(0)
        self._load_starter()

    def _build(self) -> None:
        form = ttk.Frame(self, padding=10)
        form.pack(fill=tk.X)
        self._path_row(
            form,
            0,
            "DualSPHysics 根目录",
            self.root_value,
            self._browse_root,
        )
        ttk.Button(form, text="扫描工具", command=self._scan).grid(
            row=0,
            column=3,
            padx=(8, 0),
        )
        self._path_row(
            form,
            1,
            "输入数据目录",
            self.input_value,
            self._browse_input,
        )
        self._path_row(
            form,
            2,
            "输出父目录",
            self.output_value,
            self._browse_output,
        )
        ttk.Label(form, text="官方工具").grid(
            row=3,
            column=0,
            sticky=tk.W,
            pady=(8, 0),
        )
        self.tool_combo = ttk.Combobox(
            form,
            state="readonly",
            values=[tool.label for tool in KNOWN_TOOLS],
        )
        self.tool_combo.grid(
            row=3,
            column=1,
            columnspan=2,
            sticky=tk.EW,
            padx=8,
            pady=(8, 0),
        )
        ttk.Button(
            form,
            text="载入起始模板",
            command=self._load_starter,
        ).grid(row=3, column=3, padx=(8, 0), pady=(8, 0))
        ttk.Label(form, text="配方名称").grid(
            row=4,
            column=0,
            sticky=tk.W,
            pady=(8, 0),
        )
        ttk.Entry(form, textvariable=self.recipe_name).grid(
            row=4,
            column=1,
            columnspan=3,
            sticky=tk.EW,
            padx=(8, 0),
            pady=(8, 0),
        )
        form.columnconfigure(1, weight=1)

        ttk.Label(
            self,
            text="参数数组（每行一个参数；只允许 {input}、{output} 占位符）",
            padding=(10, 4, 10, 2),
        ).pack(anchor=tk.W)
        self.arguments_text = tk.Text(self, height=9, wrap=tk.NONE)
        self.arguments_text.pack(fill=tk.X, padx=10)

        recipe_buttons = ttk.Frame(self, padding=(10, 8))
        recipe_buttons.pack(fill=tk.X)
        ttk.Button(
            recipe_buttons,
            text="预览命令",
            command=self._preview,
        ).pack(side=tk.LEFT)
        ttk.Button(
            recipe_buttons,
            text="保存配方…",
            command=self._save_recipe,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            recipe_buttons,
            text="加载配方…",
            command=self._load_recipe,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.run_button = ttk.Button(
            recipe_buttons,
            text="确认并运行",
            command=self._start,
        )
        self.run_button.pack(side=tk.RIGHT)
        self.cancel_button = ttk.Button(
            recipe_buttons,
            text="取消任务",
            command=self._cancel,
            state=tk.DISABLED,
        )
        self.cancel_button.pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Label(
            self,
            textvariable=self.status_value,
            padding=(10, 0, 10, 6),
        ).pack(fill=tk.X)
        self.log_text = tk.Text(self, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        value: tk.StringVar,
        command,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W)
        ttk.Entry(parent, textvariable=value).grid(
            row=row,
            column=1,
            columnspan=2,
            sticky=tk.EW,
            padx=8,
            pady=3,
        )
        ttk.Button(parent, text="浏览…", command=command).grid(
            row=row,
            column=3,
            padx=(8, 0),
        )

    def _browse_root(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="选择 DualSPHysics 根目录",
            initialdir=self.root_value.get() or None,
        )
        if selected:
            self.root_value.set(selected)
            self._scan()

    def _browse_input(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="选择官方后处理工具输入目录",
            initialdir=self.input_value.get() or None,
        )
        if selected:
            self.input_value.set(selected)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="选择 PostLab 输出父目录",
            initialdir=self.output_value.get() or None,
        )
        if selected:
            self.output_value.set(selected)

    def _scan(self) -> None:
        root = self.root_value.get().strip()
        if not root:
            return
        tools = discover_tools(root)
        self.installations = {tool.definition.key: tool for tool in tools}
        self.tool_combo.configure(
            values=[
                f"{'✓' if tool.available else '—'} {tool.definition.label}"
                for tool in tools
            ]
        )
        available = sum(tool.available for tool in tools)
        self.status_value.set(
            f"发现 {available}/{len(tools)} 个支持的官方后处理工具。"
        )

    def _selected_tool_key(self) -> str:
        index = self.tool_combo.current()
        if not 0 <= index < len(KNOWN_TOOLS):
            raise PostLabError("请先选择官方后处理工具。")
        return KNOWN_TOOLS[index].key

    def _recipe(self) -> PostRecipe:
        arguments = tuple(
            line.strip()
            for line in self.arguments_text.get("1.0", tk.END).splitlines()
            if line.strip()
        )
        recipe = PostRecipe(
            name=self.recipe_name.get(),
            tool=self._selected_tool_key(),
            arguments=arguments,
        )
        recipe.validate()
        return recipe

    def _load_starter(self) -> None:
        key = self._selected_tool_key()
        arguments = DEFAULT_ARGUMENTS.get(
            key,
            ("请删除本行并按官方说明填写独立参数",),
        )
        self.arguments_text.delete("1.0", tk.END)
        self.arguments_text.insert("1.0", "\n".join(arguments))
        if key == "partvtk":
            self.recipe_name.set("PartVTK 全粒子 VTK")
        else:
            self.recipe_name.set(f"{_tool_label(key)} 自定义配方")

    def _preview(self) -> None:
        try:
            command = preview_command(
                self._recipe(),
                self.root_value.get(),
                self.input_value.get(),
            )
        except PostLabError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        self._replace_log(_display_command(command))
        self.status_value.set("命令仅用于预览；尚未执行。")

    def _save_recipe(self) -> None:
        try:
            recipe = self._recipe()
        except PostLabError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="保存 PostLab 配方",
            defaultextension=".json",
            filetypes=(("PostLab JSON", "*.json"),),
        )
        if not selected:
            return
        try:
            save_recipe(recipe, selected)
        except (OSError, PostLabError) as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)

    def _load_recipe(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="加载 PostLab 配方",
            filetypes=(("PostLab JSON", "*.json"),),
        )
        if not selected:
            return
        try:
            recipe = load_recipe(selected)
        except PostLabError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        index = next(
            index
            for index, tool in enumerate(KNOWN_TOOLS)
            if tool.key == recipe.tool
        )
        self.tool_combo.current(index)
        self.recipe_name.set(recipe.name)
        self.arguments_text.delete("1.0", tk.END)
        self.arguments_text.insert("1.0", "\n".join(recipe.arguments))
        self.status_value.set("配方已加载；本机路径不会保存在配方中。")

    def _start(self) -> None:
        try:
            recipe = self._recipe()
            command = preview_command(
                recipe,
                self.root_value.get(),
                self.input_value.get(),
            )
            output_parent = Path(self.output_value.get()).resolve(strict=True)
            if not output_parent.is_dir():
                raise PostLabError("输出父路径不是文件夹。")
        except (OSError, PostLabError) as error:
            messagebox.showerror(APP_NAME, str(error), parent=self)
            return
        answer = messagebox.askyesno(
            APP_NAME,
            "PostLab 将直接执行以下官方 EXE，不经过 BAT 或 Shell，"
            "并在输出父目录中新建任务目录：\n\n"
            f"{_display_command(command)}\n\n"
            "请确认参数、输入数据和官方工具版本正确。",
            parent=self,
            default=messagebox.NO,
        )
        if not answer:
            return
        self.cancel_event = Event()
        self.run_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.status_value.set("官方后处理任务正在运行…")
        self._replace_log("")
        installation_root = self.root_value.get()
        input_directory = self.input_value.get()
        Thread(
            target=self._worker,
            args=(
                recipe,
                installation_root,
                input_directory,
                str(output_parent),
            ),
            daemon=True,
        ).start()

    def _worker(
        self,
        recipe: PostRecipe,
        installation_root: str,
        input_directory: str,
        output_parent: str,
    ) -> None:
        try:
            result = run_recipe(
                recipe,
                installation_root,
                input_directory,
                output_parent,
                cancel_event=self.cancel_event,
                on_log=lambda line: self.after(0, lambda: self._append_log(line)),
            )
        except PostLabError as error:
            message = str(error)
            self.after(0, lambda: self._failed(message))
            return
        self.after(0, lambda: self._finished(result))

    def _cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.status_value.set("正在请求取消；任务记录会保留。")

    def _failed(self, message: str) -> None:
        self.run_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.status_value.set("任务失败。")
        messagebox.showerror(APP_NAME, message, parent=self)

    def _finished(self, result: PostLabRun) -> None:
        self.run_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.status_value.set(
            f"任务 {result.status}；生成 {result.generated_files} 个文件。"
        )
        self._append_log(f"\n任务记录：{result.record_path}")
        messagebox.showinfo(
            APP_NAME,
            f"状态：{result.status}\n输出：{result.output_directory}",
            parent=self,
        )

    def _replace_log(self, value: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", value)
        self.log_text.configure(state=tk.DISABLED)

    def _append_log(self, value: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, value + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 5000:
            self.log_text.delete("1.0", f"{lines - 5000}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)


def _tool_label(key: str) -> str:
    return next(tool.label for tool in KNOWN_TOOLS if tool.key == key)


def _display_command(command: Sequence[str]) -> str:
    return "\n".join(f"[{index}] {argument}" for index, argument in enumerate(command))


def _smoke_test() -> int:
    with tempfile.TemporaryDirectory(prefix="dss-postlab-") as temporary:
        root = Path(temporary) / "DualSPHysics"
        executable = root / "bin" / "windows" / "PartVTK_win64.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"MZ inert smoke fixture")
        input_directory = Path(temporary) / "data"
        input_directory.mkdir()
        recipe = PostRecipe(
            "smoke",
            "partvtk",
            DEFAULT_ARGUMENTS["partvtk"],
        )
        target = Path(temporary) / "recipe.json"
        save_recipe(recipe, target)
        loaded = load_recipe(target)
        command = preview_command(loaded, root, input_directory)
        if command[0] != str(executable.resolve()) or "{input}" in " ".join(command):
            raise RuntimeError("PostLab smoke test failed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="DSSPostLab")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return _smoke_test()
    window = PostLabWindow()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
