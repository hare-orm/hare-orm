"""Checks an installed hare-orm wheel: the extensions its tag promises import, and hare works on
SQLite - and on PostgreSQL through the Rust driver when ``--postgres-url`` names a server.

Run it with the interpreter the wheel is installed into, from outside the repository (so
``import rust`` finds the installed package, not the checkout):

    python check_wheel.py --platform-wheel [--postgres-url postgresql://user:pass@host:5432/db]
    python check_wheel.py --pure-wheel
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path

from hare import Hare, fields
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

    #: The extension modules a platform wheel carries.
    EXTENSION_MODULES = ("rust.native",)

    def __init__(self, platform_wheel: bool, postgres_url: str | None) -> None:
        """
        Args:
            platform_wheel: Whether the wheel is a platform wheel, with the extensions.
            postgres_url: A PostgreSQL server to run a query on through the Rust driver.
        """
        self.platform_wheel = platform_wheel
        self.postgres_url = postgres_url

    def check_extensions(self) -> None:
        """Checks the extensions import from the installed package - or are absent from the pure
        wheel.

        Raises:
            SystemExit: An extension doesn't import, imports from the checkout, or the pure wheel
                carries one.
        """
        import hare  # noqa: PLC0415 - the installed package, located at run time

        package_directory = Path(hare.__file__).resolve().parent.parent
        for module_name in self.EXTENSION_MODULES:
            try:
                module = importlib.import_module(module_name)
            except ImportError as error:
                if self.platform_wheel:
                    raise SystemExit(f"{module_name} doesn't import: {error}") from error
                print(f"{module_name}: absent, as in the pure wheel")
                continue
            module_file = getattr(module, "__file__", None)
            if module_file is None:
                # The bare `rust` namespace directory, with no compiled module in it.
                if self.platform_wheel:
                    raise SystemExit(f"{module_name} is an empty namespace package")
                print(f"{module_name}: absent, as in the pure wheel")
                continue
            if not self.platform_wheel:
                raise SystemExit(f"the pure wheel carries {module_name}: {module_file}")
            if Path(module_file).resolve().parent.parent != package_directory:
                raise SystemExit(f"{module_name} imports from {module_file}, not from the installed package")
            print(f"{module_name}: {module_file}")

    async def check_database(self, db_url: str) -> None:
        """Creates a table, writes and reads a row on the database of ``db_url``.

        Args:
            db_url: The database URL.

        Raises:
            SystemExit: The row doesn't come back.
        """
        await Hare.init(db_url=db_url, modules={"wheel_check": [__name__]})
        try:
            await Hare.generate_schemas()
            await WheelCheckNote.all().delete()
            await WheelCheckNote.create(id=1, title="written by the wheel")
            titles = [row["title"] for row in await WheelCheckNote.all().values("title")]
            scheme = db_url.split(":")[0]
            if list(titles) != ["written by the wheel"]:
                raise SystemExit(f"{scheme}: read back {titles!r}")
            client_class_name = type(WheelCheckNote._meta.db).__name__
            if scheme == "postgresql" and not client_class_name.startswith("RustPg"):
                raise SystemExit(f"postgresql:// ran on {client_class_name}, not the Rust driver")
            print(f"{scheme}: a row written and read back through {client_class_name}")
            await WheelCheckNote.all().delete()
        finally:
            await Hare.close_connections()

    def run(self) -> None:
        """Runs every check."""
        print(f"Python {sys.version.split()[0]} on {sys.platform}")
        self.check_extensions()
        asyncio.run(self.check_database("sqlite://:memory:"))
        if self.postgres_url:
            if not self.platform_wheel:
                raise SystemExit("--postgres-url checks the Rust driver, which only a platform wheel carries")
            asyncio.run(self.check_database(self.postgres_url))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checks an installed hare-orm wheel.")
    kind = parser.add_mutually_exclusive_group(required=True)
    kind.add_argument("--platform-wheel", action="store_true", help="The wheel carries the extensions.")
    kind.add_argument("--pure-wheel", action="store_true", help="The wheel carries no extension.")
    parser.add_argument("--postgres-url", help="A PostgreSQL server to check the Rust driver against.")
    arguments = parser.parse_args()
    WheelCheck(arguments.platform_wheel, arguments.postgres_url).run()
