from __future__ import annotations

import textwrap
from pathlib import Path


class ProjectSources:
    """Write the Python sources of a temporary test project beneath its fixture directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write_module(self, name: str, source: str) -> Path:
        path = self.root / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        return path

    def write_package(self, name: str, models_source: str = "") -> Path:
        self.write_module(f"{name}/__init__", "")
        self.write_module(f"{name}/models", models_source)
        return self.root / name

    def prepare_migrations(self, package: Path) -> Path:
        """Create the package and migration initializers while keeping existing sources."""
        relative = package.relative_to(self.root)
        migrations = relative / "migrations"
        for directory in (relative, migrations):
            if not (self.root / directory / "__init__.py").exists():
                self.write_module((directory / "__init__").as_posix(), "")
        return self.root / migrations

    def write_migration(self, package: Path, name: str, source: str) -> Path:
        migrations = self.prepare_migrations(package).relative_to(self.root)
        return self.write_module((migrations / name).as_posix(), source)
