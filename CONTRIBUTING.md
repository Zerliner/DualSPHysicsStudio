# Contributing to DualSPHysics Studio

Thank you for helping improve DualSPHysics Studio. Keep changes focused, reviewable, and compatible with the existing architecture.

## Development environment

The supported development baseline is 64-bit Python 3.13 on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip install pytest==9.1.1
```

Run the verification checks before opening a pull request:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -o addopts= -q
.\.venv\Scripts\python.exe -m dualsphysics_studio --smoke-test
```

## Pull requests

- Describe the problem, the chosen solution, and how the change was verified.
- Keep each pull request limited to one coherent change.
- Reuse existing modules, conventions, and extension points where they fit.
- Add or update tests for changed behavior.
- Avoid unrelated refactors, formatting changes, dependencies, or public API changes.
- Do not commit simulation cases, generated outputs, API keys, personal paths, third-party executables, or other private data.

DualSPHysics executables are not part of this repository. The independent OSGB-to-STL portable tool is also distributed separately and can be placed in the Studio `DLC` directory by users.

By submitting a contribution, you agree that it may be distributed under the repository's [MIT License](LICENSE).
