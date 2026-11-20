"""Puts the ``rust.native`` extension of every maturin wheel in a directory into ``rust/``, where a
checkout imports it from - one file per Python version and platform the wheels are built for.

Usage:
    python .github/scripts/extract_native_extensions.py WHEEL_DIRECTORY
"""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path


class NativeExtensionExtractor:
    """Takes the compiled ``rust.native`` out of maturin's wheels."""

    #: The file suffixes of a compiled extension module.
    EXTENSION_SUFFIXES = (".so", ".pyd")

    def __init__(self, wheel_directory: Path, extension_directory: Path) -> None:
        """
        Args:
            wheel_directory: The directory with maturin's wheels of ``rust/native``.
            extension_directory: The directory the extensions are written to.
        """
        self.wheel_directory = wheel_directory
        self.extension_directory = extension_directory

    def run(self) -> None:
        """Extracts the extension of every wheel.


        Raises:
            SystemExit: The directory holds no wheel, or a wheel carries no single extension file.
        """
        wheels = sorted(self.wheel_directory.glob("*.whl"))
        if not wheels:
            raise SystemExit(f"no wheels in {self.wheel_directory}")
        for wheel in wheels:
            with zipfile.ZipFile(wheel) as archive:
                _, target_path = self.extract(wheel, archive, self.extension_directory)
            print(f"{wheel.name}: {target_path}")

    @classmethod
    def extract(cls, wheel: Path, archive: zipfile.ZipFile, directory: Path) -> tuple[str, Path]:
        """Read the wheel's single native module into the given directory."""
        members = [
            name
            for name in archive.namelist()
            if name.startswith("rust/native.") and name.endswith(cls.EXTENSION_SUFFIXES)
        ]
        if len(members) != 1:
            raise SystemExit(f"{wheel.name} carries {len(members)} rust.native extension files, expected one")
        [member] = members
        target = directory / Path(member).name
        with archive.open(member) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        return member, target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Puts the rust.native extension of maturin's wheels into rust/.")
    parser.add_argument("wheel_directory", type=Path, help="The directory with maturin's wheels.")
    arguments = parser.parse_args()
    NativeExtensionExtractor(arguments.wheel_directory, Path(__file__).resolve().parents[2] / "rust").run()
