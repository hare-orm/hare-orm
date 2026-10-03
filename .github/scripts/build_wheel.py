"""Builds a hare-orm wheel for a release.

With ``--extension-wheels`` it builds the wheel of one platform: the ``rust.native``
extension is taken out of the wheel maturin built for that platform, put into ``rust/`` and
packed into hare-orm's wheel, which gets the same tags (``cp314-cp314-manylinux_2_28_x86_64``).
Without it, it builds the pure wheel (``py3-none-any``) with no extension - for the platforms no
platform wheel is built for, where ``postgresql+asyncpg://`` and SQLite work and ``postgresql://``
says the Rust driver isn't there.

Usage:
    python .github/scripts/build_wheel.py --out dist [--extension-wheels extension-wheels]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


class WheelBuilder:
    """Builds hare-orm's wheel of one platform, or its pure wheel."""

    #: The extension modules a platform wheel carries.
    EXTENSION_MODULES = ("native",)
    #: The file suffixes of a compiled extension module.
    EXTENSION_SUFFIXES = (".so", ".pyd")

    def __init__(self, repository: Path, out: Path) -> None:
        """
        Args:
            repository: The repository root.
            out: The directory the wheel is written to.
        """
        self.repository = repository
        self.extension_directory = repository / "rust"
        self.out = out

    def get_extension_files(self) -> list[Path]:
        """The compiled extensions in ``rust/``."""
        return [
            path
            for path in self.extension_directory.iterdir()
            if path.is_file() and path.suffix in self.EXTENSION_SUFFIXES
        ]

    def get_wheel_tag(self, wheel: Path) -> str:
        """The ``python-abi-platform`` tag of a wheel, from its file name.

        Args:
            wheel: The wheel.

        Returns:
            The tag.
        """
        # name-version(-build)?-python-abi-platform.whl
        return "-".join(wheel.stem.split("-")[-3:])

    def extract_extensions(self, extension_wheels: Path) -> str:
        """Puts the extension modules of maturin's wheels into ``rust/``.

        Args:
            extension_wheels: The directory with one maturin wheel per extension module.

        Returns:
            The tag the extension wheels share.

        Raises:
            SystemExit: A module's wheel is missing, carries no extension, or the wheels are built
                for different platforms.
        """
        tags = set()
        for module in self.EXTENSION_MODULES:
            wheels = sorted(extension_wheels.glob(f"hare_{module}-*.whl"))
            if len(wheels) != 1:
                raise SystemExit(f"expected one hare_{module} wheel in {extension_wheels}, found {len(wheels)}")
            wheel = wheels[0]
            tags.add(self.get_wheel_tag(wheel))
            with zipfile.ZipFile(wheel) as archive:
                members = [
                    name
                    for name in archive.namelist()
                    if name.startswith(f"rust/{module}.") and name.endswith(self.EXTENSION_SUFFIXES)
                ]
                if len(members) != 1:
                    raise SystemExit(
                        f"{wheel.name} carries {len(members)} rust.{module} extension files, expected one"
                    )
                target = self.extension_directory / Path(members[0]).name
                with archive.open(members[0]) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                print(f"{wheel.name}: {members[0]}")
        if len(tags) != 1:
            raise SystemExit(f"the extension wheels are built for different platforms: {sorted(tags)}")
        return tags.pop()

    def build(self, tag: str | None) -> Path:
        """Builds the wheel with poetry and gives it ``tag``.

        Args:
            tag: The ``python-abi-platform`` tag, None for the pure wheel.

        Returns:
            The wheel.
        """
        with tempfile.TemporaryDirectory() as build_directory:
            subprocess.run(
                ["poetry", "build", "--format", "wheel", "--output", build_directory],
                cwd=self.repository,
                check=True,
            )
            (wheel,) = Path(build_directory).glob("*.whl")
            if tag is not None:
                python_tag, abi_tag, platform_tag = tag.split("-")
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "wheel",
                        "tags",
                        "--remove",
                        "--python-tag",
                        python_tag,
                        "--abi-tag",
                        abi_tag,
                        "--platform-tag",
                        platform_tag,
                        str(wheel),
                    ],
                    check=True,
                )
                (wheel,) = Path(build_directory).glob("*.whl")
            self.out.mkdir(parents=True, exist_ok=True)
            built = self.out / wheel.name
            shutil.copy2(wheel, built)
        return built

    def check(self, wheel: Path, tag: str | None) -> None:
        """Checks the wheel carries exactly the extension modules its tag promises.

        Args:
            wheel: The wheel.
            tag: Its tag, None for the pure wheel.

        Raises:
            SystemExit: A platform wheel misses an extension, or the pure wheel carries one.
        """
        with zipfile.ZipFile(wheel) as archive:
            extensions = [
                name
                for name in archive.namelist()
                if name.startswith("rust/") and name.endswith(self.EXTENSION_SUFFIXES)
            ]
        if tag is None and extensions:
            raise SystemExit(f"the pure wheel carries compiled extensions: {extensions}")
        if tag is not None:
            modules = sorted(Path(name).name.split(".")[0] for name in extensions)
            if modules != sorted(self.EXTENSION_MODULES):
                raise SystemExit(f"{wheel.name} carries {extensions}, expected one file per {self.EXTENSION_MODULES}")
        print(f"built {wheel.name}: {extensions or 'no extensions'}")

    def run(self, extension_wheels: Path | None) -> None:
        """Builds the wheel.

        Args:
            extension_wheels: The directory with maturin's wheels of one platform, None for the
                pure wheel.
        """
        # The extensions a checkout carries are built for other platforms: they are set aside
        # while the wheel is built and put back afterwards.
        with tempfile.TemporaryDirectory() as set_aside_directory:
            set_aside = []
            for path in self.get_extension_files():
                set_aside.append(Path(shutil.move(path, Path(set_aside_directory) / path.name)))
            try:
                tag = self.extract_extensions(extension_wheels) if extension_wheels is not None else None
                wheel = self.build(tag)
                self.check(wheel, tag)
            finally:
                for path in self.get_extension_files():
                    path.unlink()
                for path in set_aside:
                    shutil.move(path, self.extension_directory / path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Builds a hare-orm wheel for a release.")
    parser.add_argument("--out", type=Path, required=True, help="The directory the wheel is written to.")
    parser.add_argument(
        "--extension-wheels",
        type=Path,
        help="The directory with maturin's rust.native wheel of one platform; "
        "without it the pure wheel is built.",
    )
    arguments = parser.parse_args()
    WheelBuilder(Path(__file__).resolve().parents[2], arguments.out.resolve()).run(
        arguments.extension_wheels.resolve() if arguments.extension_wheels else None
    )
