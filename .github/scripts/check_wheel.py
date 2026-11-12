"""Checks an installed hare-orm wheel: its extension imports - with the PostgreSQL driver on Linux and macOS,
without it on Windows - hare works on SQLite - and on PostgreSQL through the Rust driver when
``--postgres-url`` names a server - and ``hare makemigrations`` and ``hare migrate`` work on a project of its
models, with none of the extras installed.

Run it with the interpreter the wheel is installed into, from outside the repository (so
``import rust`` finds the installed package, not the checkout):

    python check_wheel.py [--postgres-url postgresql://user:pass@host:5432/db]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import subprocess  # nosec B404 - build checks invoke trusted tooling without a shell.
import sys
import tempfile
from pathlib import Path

from hare import Hare, HareConfig, fields
from hare.models import Model


class WheelCheckNote(Model):
    """The table the check writes a row to and reads it back from."""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=40)

    class Meta:
        app = "wheel_check"
        table = "hare_wheel_check_note"


class WheelCheck:
    """The checks of one installed wheel."""

    #: The extension modules a wheel carries.
    EXTENSION_MODULES = ("rust.native",)
    #: The submodules of rust.native that make up the PostgreSQL driver - built on Unix alone.
    DRIVER_SUBMODULES = ("pg", "pool")

    def __init__(self, postgres_url: str | None) -> None:
        """
        Args:
            postgres_url: A PostgreSQL server to run a query on through the Rust driver.
        """
        self.postgres_url = postgres_url

    def check_extensions(self) -> None:
        """Checks the extensions import from the installed package, with the PostgreSQL driver where
        it is built.

        Raises:
            SystemExit: An extension doesn't import or imports from the checkout, or the driver is
                missing on Unix or present on Windows.
        """
        import hare  # noqa: PLC0415 - the installed package, located at run time

        package_directory = Path(hare.__file__).resolve().parent.parent
        for module_name in self.EXTENSION_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError as error:
                raise SystemExit(f"{module_name} doesn't import: {error}") from error
            module_file = getattr(module, "__file__", None)
            if module_file is None:
                # The bare `rust` namespace directory, with no compiled module in it.
                raise SystemExit(f"{module_name} is an empty namespace package")
            if Path(module_file).resolve().parent.parent != package_directory:
                raise SystemExit(f"{module_name} imports from {module_file}, not from the installed package")
            print(f"{module_name}: {module_file}")
        native = importlib.import_module("rust.native")
        driver_submodules = [name for name in self.DRIVER_SUBMODULES if hasattr(native, name)]
        expected_submodules = list(self.DRIVER_SUBMODULES) if os.name == "posix" else []
        if driver_submodules != expected_submodules:
            raise SystemExit(
                f"rust.native carries the driver submodules {driver_submodules} on {sys.platform}, "
                f"expected {expected_submodules}"
            )
        print(f"rust.native driver submodules: {driver_submodules or 'none, as on Windows'}")

    async def check_database(self, db_url: str) -> None:
        """Creates a table, writes and reads a row on the database of ``db_url``.

        Args:
            db_url: The database URL.

        Raises:
            SystemExit: The row doesn't come back.
        """
        await Hare.init(HareConfig.from_db_url(db_url, {"wheel_check": [__name__]}))
        try:
            await Hare.generate_schemas()
            await WheelCheckNote.objects.all().delete()
            await WheelCheckNote.objects.create(id=1, title="written by the wheel")
            titles = [row["title"] for row in await WheelCheckNote.objects.all().values("title")]
            scheme = db_url.split(":")[0]
            if list(titles) != ["written by the wheel"]:
                raise SystemExit(f"{scheme}: read back {titles!r}")
            client_class_name = type(WheelCheckNote._meta.connection).__name__
            if scheme == "postgresql" and not client_class_name.startswith("RustPg"):
                raise SystemExit(f"postgresql:// ran on {client_class_name}, not the Rust driver")
            print(f"{scheme}: a row written and read back through {client_class_name}")
            await WheelCheckNote.objects.all().delete()
        finally:
            await Hare.close_connections()

    @staticmethod
    def check_migrations() -> None:
        """Writes a project's first migration, applies it, and finds no changes afterwards - the CLI of the
        installed wheel, in a project of its own on a SQLite file.

        Raises:
            SystemExit: A command fails, or the second makemigrations writes a migration.
        """
        with tempfile.TemporaryDirectory() as project_directory:
            project = Path(project_directory)
            (project / "wheel_project").mkdir()
            (project / "wheel_project" / "__init__.py").write_text("", encoding="utf-8")
            (project / "wheel_project" / "models.py").write_text(
                "from hare import fields\n"
                "from hare.models import Model\n\n\n"
                "class Note(Model):\n"
                "    id = fields.IntField(primary_key=True)\n"
                "    title = fields.CharField(max_length=40)\n",
                encoding="utf-8",
            )
            (project / "wheel_settings.py").write_text(
                "HARE_ORM = {\n"
                f'    "connections": {{"default": "sqlite+aiosqlite://{(project / "project.sqlite3").as_posix()}"}},\n'
                '    "apps": {"wheel_project": {"models": ["wheel_project.models"], '
                '"default_connection": "default"}},\n'
                "}\n",
                encoding="utf-8",
            )
            outputs = []
            for command in ("makemigrations", "migrate", "makemigrations"):
                completed = subprocess.run(  # nosec B603 - explicit build-tool arguments, no shell.
                    [sys.executable, "-m", "hare.cli", "-c", "wheel_settings.HARE_ORM", command],
                    cwd=project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise SystemExit(f"hare {command} failed:\n{completed.stdout}{completed.stderr}")
                outputs.append(completed.stdout)
            if not list((project / "wheel_project" / "migrations").glob("0001_*.py")):
                raise SystemExit(f"makemigrations wrote no migration:\n{outputs[0]}")
            if "No changes detected" not in outputs[2]:
                raise SystemExit(f"makemigrations after migrate found changes:\n{outputs[2]}")
        print("makemigrations, migrate: a project's migration written, applied, no changes after it")

    def run(self) -> None:
        """Runs every check."""
        print(f"Python {sys.version.split()[0]} on {sys.platform}")
        self.check_extensions()
        asyncio.run(self.check_database("sqlite+aiosqlite://:memory:"))
        self.check_migrations()
        if self.postgres_url:
            asyncio.run(self.check_database(self.postgres_url))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checks an installed hare-orm wheel.")
    parser.add_argument("--postgres-url", help="A PostgreSQL server to check the Rust driver against.")
    arguments = parser.parse_args()
    WheelCheck(arguments.postgres_url).run()
