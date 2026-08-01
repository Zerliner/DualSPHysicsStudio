from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from dlc_tools.postlab.app import main
from dlc_tools.postlab.postlab import (
    DEFAULT_ARGUMENTS,
    KNOWN_TOOLS,
    PostLabError,
    PostRecipe,
    discover_tools,
    load_recipe,
    preview_command,
    run_recipe,
    save_recipe,
)


def _installation(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "DualSPHysics"
    executable = root / "bin" / "windows" / "PartVTK_win64.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ inert fixture")
    return root, executable.resolve()


def _recipe() -> PostRecipe:
    return PostRecipe(
        name="PartVTK test",
        tool="partvtk",
        arguments=DEFAULT_ARGUMENTS["partvtk"],
    )


def test_discovery_is_limited_to_known_bin_windows_tools(tmp_path: Path) -> None:
    root, executable = _installation(tmp_path)
    outside = tmp_path / "MeasureTool_win64.exe"
    outside.write_bytes(b"MZ outside")

    tools = discover_tools(root)

    assert len(tools) == len(KNOWN_TOOLS)
    partvtk = next(tool for tool in tools if tool.definition.key == "partvtk")
    measure = next(tool for tool in tools if tool.definition.key == "measuretool")
    assert partvtk.available
    assert partvtk.path == executable
    assert not measure.available


def test_recipe_roundtrip_does_not_persist_local_paths(tmp_path: Path) -> None:
    target = save_recipe(_recipe(), tmp_path / "recipe.json")

    loaded = load_recipe(target)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert loaded == _recipe()
    assert set(payload) == {"schema_version", "name", "tool", "arguments"}
    assert str(tmp_path) not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "arguments",
    (
        ("-savevtk", r"C:\existing\PartAll"),
        ("-savevtk", r"C:existing\PartAll"),
        ("-savevtk", r"\existing\PartAll"),
        ("-savevtk", "//server/share/PartAll"),
        ("-savevtk", r"{output}\..\existing"),
        ("-savevtk", r"{output}-sibling"),
        ("-savevtk", r"prefix{output}\PartAll"),
        ("-savevtk", "{unknown}"),
        ("-dirdata", "{input}"),
    ),
)
def test_recipe_rejects_unsafe_or_outputless_arguments(
    arguments: tuple[str, ...],
) -> None:
    recipe = PostRecipe("unsafe", "partvtk", arguments)

    with pytest.raises(PostLabError):
        recipe.validate()


def test_preview_renders_argument_array_without_executing(tmp_path: Path) -> None:
    root, executable = _installation(tmp_path)
    input_directory = tmp_path / "data"
    input_directory.mkdir()

    command = preview_command(_recipe(), root, input_directory)

    assert command[0] == str(executable)
    assert str(input_directory.resolve()) in command
    assert command[-1].startswith("-vars:")
    assert all("{input}" not in argument for argument in command)


def test_run_creates_unique_directory_record_and_inventory(tmp_path: Path) -> None:
    root, executable = _installation(tmp_path)
    input_directory = tmp_path / "data"
    input_directory.mkdir()
    output_parent = tmp_path / "post"
    output_parent.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        def __init__(self, command: list[str], **kwargs) -> None:
            calls.append((command, kwargs))
            self.stdout = io.StringIO("processing\nfinished\n")
            self.returncode = 0
            Path(str(kwargs["cwd"]), "PartAll_0000.vtk").write_text(
                "# vtk",
                encoding="ascii",
            )

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    first = run_recipe(
        _recipe(),
        root,
        input_directory,
        output_parent,
        popen_factory=FakeProcess,
    )
    second = run_recipe(
        _recipe(),
        root,
        input_directory,
        output_parent,
        popen_factory=FakeProcess,
    )

    assert first.status == "succeeded"
    assert first.return_code == 0
    assert first.generated_files == 1
    assert first.output_directory != second.output_directory
    assert calls[0][0][0] == str(executable)
    assert calls[0][1]["shell"] is False
    assert isinstance(calls[0][0], list)
    record = json.loads(first.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "succeeded"
    assert record["tool"]["sha256"]
    assert record["artifacts"] == [
        {"path": "PartAll_0000.vtk", "bytes": 5}
    ]
    assert (first.output_directory / "stdout.log").read_text(
        encoding="utf-8"
    ) == "processing\nfinished\n"


def test_success_exit_without_output_is_reported_failed(tmp_path: Path) -> None:
    root, _executable = _installation(tmp_path)
    input_directory = tmp_path / "data"
    input_directory.mkdir()
    output_parent = tmp_path / "post"
    output_parent.mkdir()

    class NoOutputProcess:
        def __init__(self, _command, **_kwargs) -> None:
            self.stdout = io.StringIO("")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    result = run_recipe(
        _recipe(),
        root,
        input_directory,
        output_parent,
        popen_factory=NoOutputProcess,
    )

    assert result.status == "failed"
    assert result.generated_files == 0


def test_postlab_cli_smoke_test_does_not_open_gui() -> None:
    assert main(["--smoke-test"]) == 0
