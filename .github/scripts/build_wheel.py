"""Builds the hare-orm wheels of a release.

With ``--extension-wheels`` it builds the wheels of one platform, one per Python version: for every
tag maturin built a ``rust.native`` wheel for (``cp312-cp312-manylinux_2_17_x86_64``, ``cp313-...``,
``cp314-...``), the extension is taken out of that wheel, put into ``rust/`` and packed into a
hare-orm wheel with the same tag, together with the shared libraries maturin's repair bundled with it
(``hare_native.libs/``, libgcc_s on musllinux). Without it, it builds the pure wheel (``py3-none-any``) with no
extension - for the platforms no platform wheel is built for, where ``postgresql+asyncpg://`` and
SQLite work and ``postgresql://`` says the Rust driver isn't there.

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
    #: The directory of the shared libraries maturin's repair bundles with the extension - at the
    #: wheel's root, where the extension's run path ($ORIGIN/../hare_native.libs) finds them.
    BUNDLED_LIBRARIES_DIRECTORY_NAME = "hare_native.libs"

    def __init__(self, repository: Path, out: Path) -> None:
        """
        Args:
            repository: The repository root.
            out: The directory the wheel is written to.
        """
        self.repository = repository
        self.extension_directory = repository / "rust"
        self.bundled_libraries_directory = repository / self.BUNDLED_LIBRARIES_DIRECTORY_NAME
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

    def get_extension_wheels_by_tag(self, extension_wheels: Path) -> dict[str, dict[str, Path]]:
        """Maturin's wheels, by their tag and then by the extension module they carry.

        Args:
            extension_wheels: The directory with maturin's wheels - for every tag, one per extension
                module.

        Returns:
            The wheels.

        Raises:
            SystemExit: The directory holds no wheel, or a tag misses a module's wheel or has two.
        """
        wheels_by_tag: dict[str, dict[str, Path]] = {}
        for module in self.EXTENSION_MODULES:
            for wheel in sorted(extension_wheels.glob(f"hare_{module}-*.whl")):
                module_wheels = wheels_by_tag.setdefault(self.get_wheel_tag(wheel), {})
                if module in module_wheels:
                    raise SystemExit(
                        f"two hare_{module} wheels for one tag: {module_wheels[module].name}, {wheel.name}"
                    )
                module_wheels[module] = wheel
        if not wheels_by_tag:
            raise SystemExit(f"no extension wheels in {extension_wheels}")
        for tag, module_wheels in wheels_by_tag.items():
            missing_modules = sorted(set(self.EXTENSION_MODULES) - set(module_wheels))
            if missing_modules:
                raise SystemExit(f"no wheel of {', '.join(missing_modules)} for {tag}")
        return wheels_by_tag

    def extract_extensions(self, module_wheels: dict[str, Path]) -> list[str]:
        """Puts the extension modules of maturin's wheels of one tag into ``rust/``, and the shared
        libraries bundled with them into ``hare_native.libs/``.

        Args:
            module_wheels: The wheel of every extension module, by module.

        Returns:
            The wheel paths of the bundled libraries.

        Raises:
            SystemExit: A wheel carries no single extension file.
        """
        bundled_libraries = []
        for module, wheel in module_wheels.items():
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
                for name in archive.namelist():
                    if not name.startswith(f"{self.BUNDLED_LIBRARIES_DIRECTORY_NAME}/") or name.endswith("/"):
                        continue
                    target = self.repository / name
                    target.parent.mkdir(exist_ok=True)
                    with archive.open(name) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    bundled_libraries.append(name)
                    print(f"{wheel.name}: {name}")
        return bundled_libraries

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

    def check(self, wheel: Path, tag: str | None, bundled_libraries: list[str]) -> None:
        """Checks the wheel carries exactly the extension modules its tag promises, and the shared
        libraries bundled with them.

        Args:
            wheel: The wheel.
            tag: Its tag, None for the pure wheel.
            bundled_libraries: The wheel paths of the libraries maturin bundled with the extensions.

        Raises:
            SystemExit: A platform wheel misses an extension or a bundled library, or the pure wheel
                carries one.
        """
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        extensions = [name for name in names if name.startswith("rust/") and name.endswith(self.EXTENSION_SUFFIXES)]
        libraries = sorted(name for name in names if name.startswith(f"{self.BUNDLED_LIBRARIES_DIRECTORY_NAME}/"))
        if tag is None and (extensions or libraries):
            raise SystemExit(f"the pure wheel carries compiled extensions: {extensions + libraries}")
        if libraries != sorted(bundled_libraries):
            raise SystemExit(f"{wheel.name} carries the libraries {libraries}, expected {sorted(bundled_libraries)}")
        if tag is not None:
            modules = sorted(Path(name).name.split(".")[0] for name in extensions)
            if modules != sorted(self.EXTENSION_MODULES):
                raise SystemExit(f"{wheel.name} carries {extensions}, expected one file per {self.EXTENSION_MODULES}")
            # rust/native.cpython-312-x86_64-linux-gnu.so, rust/native.cp312-win_amd64.pyd
            python_version_digits = tag.split("-")[0].removeprefix("cp")
            other_version_extensions = [
                name for name in extensions if f"cp{python_version_digits}-" not in name.replace("cpython-", "cp")
            ]
            if other_version_extensions:
                raise SystemExit(f"{wheel.name} carries extensions of another Python: {other_version_extensions}")
        print(f"built {wheel.name}: {extensions + libraries or 'no extensions'}")

    def run(self, extension_wheels: Path | None) -> None:
        """Builds the wheels - one per tag of maturin's wheels, or the pure one.

        Args:
            extension_wheels: The directory with maturin's wheels of one platform, None for the
                pure wheel.
        """
        # The extensions a checkout carries are built for other platforms: they are set aside
        # while the wheels are built and put back afterwards.
        # Left by an interrupted build, they would go into the next wheel.
        shutil.rmtree(self.bundled_libraries_directory, ignore_errors=True)
        with tempfile.TemporaryDirectory() as set_aside_directory:
            set_aside = []
            for path in self.get_extension_files():
                set_aside.append(Path(shutil.move(path, Path(set_aside_directory) / path.name)))
            try:
                if extension_wheels is None:
                    self.check(self.build(None), None, [])
                else:
                    for tag, module_wheels in self.get_extension_wheels_by_tag(extension_wheels).items():
                        bundled_libraries = self.extract_extensions(module_wheels)
                        self.check(self.build(tag), tag, bundled_libraries)
                        for path in self.get_extension_files():
                            path.unlink()
                        shutil.rmtree(self.bundled_libraries_directory, ignore_errors=True)
            finally:
                for path in self.get_extension_files():
                    path.unlink()
                shutil.rmtree(self.bundled_libraries_directory, ignore_errors=True)
                for path in set_aside:
                    shutil.move(path, self.extension_directory / path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Builds the hare-orm wheels of a release.")
    parser.add_argument("--out", type=Path, required=True, help="The directory the wheels are written to.")
    parser.add_argument(
        "--extension-wheels",
        type=Path,
        help=(
            "The directory with maturin's rust.native wheels of one platform, one per Python version; "
            "without it the pure wheel is built."
        ),
    )
    arguments = parser.parse_args()
    WheelBuilder(Path(__file__).resolve().parents[2], arguments.out.resolve()).run(
        arguments.extension_wheels.resolve() if arguments.extension_wheels else None
    )
