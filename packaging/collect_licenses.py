"""Build one license appendix for the portable distribution."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path, PurePosixPath


RUNTIME_DISTRIBUTIONS = (
    "PyInstaller",
    "vtk",
    "openai",
    "anyio",
    "certifi",
    "distro",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "jiter",
    "packaging",
    "pydantic",
    "pydantic-core",
    "sniffio",
    "typing-extensions",
    "typing-inspection",
    "annotated-types",
    "keyring",
    "jaraco.classes",
    "jaraco.context",
    "jaraco.functools",
    "more-itertools",
    "pywin32-ctypes",
)

QT_DISTRIBUTIONS = (
    "PySide6",
    "PySide6_Essentials",
    "PySide6_Addons",
    "shiboken6",
)


def _license_relative_path(distribution_file: PurePosixPath) -> Path | None:
    parts = distribution_file.parts
    info_index = next(
        (index for index, part in enumerate(parts) if part.endswith(".dist-info")),
        None,
    )
    if info_index is None or info_index + 1 >= len(parts):
        return None

    relative_parts = parts[info_index + 1 :]
    filename = relative_parts[-1].lower()
    if relative_parts[0].lower() == "licenses":
        relative_parts = relative_parts[1:]
    elif not filename.startswith(
        ("license", "licence", "copying", "notice", "authors")
    ):
        return None

    if not relative_parts or any(part in ("", ".", "..") for part in relative_parts):
        raise RuntimeError(f"Unsafe license path: {distribution_file}")
    return Path("--".join(relative_parts))


def _append_section(output: bytearray, title: str, source_name: str, body: bytes) -> None:
    separator = "=" * 79
    output.extend(f"\n\n{separator}\nCOMPONENT: {title}\nSOURCE FILE: {source_name}\n{separator}\n".encode("utf-8"))
    output.extend(body)
    if not body.endswith((b"\n", b"\r")):
        output.extend(b"\n")


def collect_licenses(
    output_file: Path,
    summary_file: Path,
    python_license: Path,
    qt_lgpl: Path,
    qt_gpl: Path,
    qt_third_party: Path,
    vtk_third_party: Path,
    rust_third_party: Path,
) -> None:
    if output_file.exists():
        raise FileExistsError(f"Output already exists: {output_file}")

    output = bytearray(summary_file.read_bytes())
    if output and not output.endswith((b"\n", b"\r")):
        output.extend(b"\n")

    _append_section(
        output,
        "Python runtime 3.13",
        python_license.name,
        python_license.read_bytes(),
    )
    _append_section(
        output,
        "GNU General Public License version 3 (required by LGPLv3)",
        qt_gpl.name,
        qt_gpl.read_bytes(),
    )
    _append_section(
        output,
        "Qt 6.11.1 / PySide6 6.11.1 / Shiboken6 6.11.1",
        qt_lgpl.name,
        qt_lgpl.read_bytes(),
    )
    _append_section(
        output,
        "Qt 6.11.1 third-party components",
        qt_third_party.name,
        qt_third_party.read_bytes(),
    )
    _append_section(
        output,
        "VTK 9.6.2 module and third-party components",
        vtk_third_party.name,
        vtk_third_party.read_bytes(),
    )
    _append_section(
        output,
        "pydantic-core and jiter Rust crate components",
        rust_third_party.name,
        rust_third_party.read_bytes(),
    )

    for requested_name in QT_DISTRIBUTIONS:
        distribution = metadata.distribution(requested_name)
        display_name = distribution.metadata["Name"] or requested_name
        license_expression = distribution.metadata.get("License", "")
        notice = (
            f"{display_name} {distribution.version}\n"
            f"License declared by wheel metadata: {license_expression}\n"
            "Project: https://code.qt.io/cgit/pyside/pyside-setup.git/\n"
        ).encode("utf-8")
        _append_section(output, f"{display_name} {distribution.version}", "wheel METADATA summary", notice)

    for requested_name in RUNTIME_DISTRIBUTIONS:
        distribution = metadata.distribution(requested_name)
        display_name = distribution.metadata["Name"] or requested_name
        collected = 0

        for distribution_file in sorted(distribution.files or (), key=str):
            pure_path = PurePosixPath(str(distribution_file).replace("\\", "/"))
            relative_path = _license_relative_path(pure_path)
            if relative_path is None:
                continue

            source = Path(distribution.locate_file(distribution_file))
            if not source.is_file():
                raise RuntimeError(f"License file is missing: {source}")
            _append_section(
                output,
                f"{display_name} {distribution.version}",
                str(relative_path).replace("\\", "/"),
                source.read_bytes(),
            )
            collected += 1

        if not collected:
            raise RuntimeError(f"No license files found for {display_name}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(bytes(output))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--python-license", required=True, type=Path)
    parser.add_argument("--qt-lgpl", required=True, type=Path)
    parser.add_argument("--qt-gpl", required=True, type=Path)
    parser.add_argument("--qt-third-party", required=True, type=Path)
    parser.add_argument("--vtk-third-party", required=True, type=Path)
    parser.add_argument("--rust-third-party", required=True, type=Path)
    arguments = parser.parse_args()
    collect_licenses(
        arguments.output.resolve(),
        arguments.summary.resolve(),
        arguments.python_license.resolve(),
        arguments.qt_lgpl.resolve(),
        arguments.qt_gpl.resolve(),
        arguments.qt_third_party.resolve(),
        arguments.vtk_third_party.resolve(),
        arguments.rust_third_party.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
