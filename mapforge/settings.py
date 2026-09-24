"""Runtime settings, all overridable through MAPFORGE_* environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _paths(value: str) -> list[Path]:
    return [Path(p).expanduser().resolve() for p in value.split(os.pathsep) if p.strip()]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("MAPFORGE_DATA", "./data")).expanduser().resolve())
    library_dirs: list[Path] = field(default_factory=list)
    # Folders a finished package may be copied into ("export to folder"), e.g. mounted shares.
    export_dirs: list[Path] = field(default_factory=list)
    max_pixels: int = int(os.environ.get("MAPFORGE_MAX_PIXELS", str(2_000_000_000)))
    workers: int = int(os.environ.get("MAPFORGE_WORKERS", "2"))
    user_agent: str = os.environ.get("MAPFORGE_USER_AGENT", "MapForge/0.1 (self-hosted map packager)")
    # Optional shared-secret token; when set every /api call must send it.
    token: str | None = os.environ.get("MAPFORGE_TOKEN") or None

    def __post_init__(self) -> None:
        if not self.library_dirs:
            self.library_dirs = _paths(os.environ.get("MAPFORGE_LIBRARY", "")) or [self.data_dir / "library"]
        if not self.export_dirs:
            self.export_dirs = _paths(os.environ.get("MAPFORGE_EXPORT_DIRS", "")) or [self.data_dir / "exports"]
        for d in (self.cache_dir, self.jobs_dir, self.config_dir, *self.library_dirs):
            d.mkdir(parents=True, exist_ok=True)
        for d in self.export_dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass  # e.g. a share that isn't mounted yet; exports there fail with a clear error

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    def load_json(self, name: str, default):
        p = self.config_dir / name
        if not p.exists():
            return default
        return json.loads(p.read_text())

    def save_json(self, name: str, value) -> None:
        p = self.config_dir / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2))
        tmp.replace(p)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    global _settings
    _settings = s
