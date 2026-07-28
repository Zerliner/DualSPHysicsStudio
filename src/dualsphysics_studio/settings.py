from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from .version import APP_ID


def settings_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / APP_ID


def cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or settings_dir())
    return base / APP_ID / "cache"


@dataclass(slots=True)
class AppSettings:
    gencase_root: str = ""
    openai_model: str = "gpt-5.6-terra"
    recent_files: list[str] = field(default_factory=list)
    orthographic_view: bool = False
    window_geometry: str = ""
    window_state: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        target = path or settings_dir() / "settings.json"
        if not target.exists():
            return cls()
        try:
            raw: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
            allowed = {name for name in cls.__dataclass_fields__}
            return cls(**{key: value for key, value in raw.items() if key in allowed})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path | None = None) -> Path:
        target = path or settings_dir() / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(target)
        return target

    def remember_file(self, path: str, limit: int = 10) -> None:
        canonical = str(Path(path).resolve())
        self.recent_files = [item for item in self.recent_files if item != canonical]
        self.recent_files.insert(0, canonical)
        del self.recent_files[limit:]

