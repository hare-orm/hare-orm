"""Builds the hare-orm wheels of one platform of a release, one per Python version: for every tag
maturin built a ``rust.native`` wheel for (``cp312-cp312-manylinux_2_17_x86_64``, ``cp313-...``,
``cp314-...``), the extension is taken out of that wheel, put into ``rust/`` and packed into a
hare-orm wheel with the same tag, together with the shared libraries maturin's repair bundled with it
(``hare_native.libs/``, libgcc_s on musllinux). hare has no wheel without the extension.

Usage:
    python .github/scripts/build_wheel.py --out dist --extension-wheels extension-wheels
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess  # nosec B404 - build checks invoke trusted tooling without a shell.
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from itertools import product
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """One wheel's ABI/platform and the exact extension archives staged for it."""

    tag: str
    extension_wheels: tuple[tuple[str, Path], ...] = ()


class WheelBuilder:
    """Builds hare-orm's wheels of one platform."""

    #: The extension modules a wheel carries.
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
        self.repository = repository.absolute()
        self.out = out.absolute()

    def stage_source(self, destination: Path) -> None:
        """Copy the package sources and build metadata without checkout-native artifacts.

        Package declarations and the readme come from pyproject.toml. Bytecode and local
        compiled extensions are excluded; the selected archives supply native files later.
        The staging directory is fresh for each BuildSpec and is never the checkout.
        """
        destination.mkdir(parents=True)
        metadata = self.repository / "pyproject.toml"
        with metadata.open("rb") as source:
            project = tomllib.load(source)
        shutil.copy2(metadata, destination / metadata.name)
        for declaration in project["tool"]["poetry"]["packages"]:
            relative = Path(declaration.get("from", "")) / declaration["include"]
            source_path = self.repository / relative
            if source_path.is_dir():
                shutil.copytree(
                    source_path,
                    destination / relative,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.so", "*.pyd"),
                )
            else:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target)
        readme = project["project"]["readme"]
        if isinstance(readme, dict):
            readme = readme.get("file")
        if readme:
            target = destination / readme
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repository / readme, target)
        for license_file in self.repository.glob("LICENSE*"):
            if license_file.is_file():
                shutil.copy2(license_file, destination / license_file.name)

    @staticmethod
    def get_wheel_tag(wheel: Path) -> str:
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

    def extract_extensions(self, spec: BuildSpec, destination: Path) -> list[str]:
        """Puts the extension modules of maturin's wheels of one tag into ``rust/``, and the shared
        libraries bundled with them into ``hare_native.libs/``.

        Args:
            spec: The wheel of every extension module, by module.
            destination: This build's isolated source directory.

        Returns:
            The wheel paths of the bundled libraries.

        Raises:
            SystemExit: A wheel carries no single extension file.
        """
        bundled_libraries = []
        extension_directory = destination / "rust"
        extension_directory.mkdir(exist_ok=True)
        for module, wheel in spec.extension_wheels:
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
                target = extension_directory / Path(members[0]).name
                with archive.open(members[0]) as source, target.open("wb") as output_file:
                    shutil.copyfileobj(source, output_file)
                print(f"{wheel.name}: {members[0]}")
                for name in archive.namelist():
                    if not name.startswith(f"{self.BUNDLED_LIBRARIES_DIRECTORY_NAME}/") or name.endswith("/"):
                        continue
                    relative = PurePosixPath(name)
                    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                        raise SystemExit(f"invalid bundled library path in {wheel.name}: {name}")
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        if target.read_bytes() != archive.read(name):
                            raise SystemExit(f"conflicting bundled library in {wheel.name}: {name}")
                        continue
                    with archive.open(name) as source, target.open("wb") as output_file:
                        shutil.copyfileobj(source, output_file)
                    bundled_libraries.append(name)
                    print(f"{wheel.name}: {name}")
        return bundled_libraries

    @staticmethod
    def build(spec: BuildSpec, source_directory: Path, output_directory: Path) -> Path:
        """Build and tag a wheel inside staging; no output is published until check succeeds."""
        # Poetry is the trusted build tool on PATH.
        subprocess.run(  # nosec B603, B607
            ["poetry", "build", "--format", "wheel", "--output", str(output_directory)],
            cwd=source_directory,
            check=True,
        )
        (wheel,) = output_directory.glob("*.whl")
        python_tag, abi_tag, platform_tag = spec.tag.split("-")
        subprocess.run(  # nosec B603 - explicit build-tool arguments, no shell.
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
        (wheel,) = output_directory.glob("*.whl")
        # Poetry packages the supplied binaries as data. Retagging does not change
        # the installation scheme; set it explicitly and let wheel regenerate RECORD.
        unpacked = output_directory / "unpacked"
        subprocess.run(  # nosec B603 - the current interpreter runs the installed wheel tool.
            [sys.executable, "-m", "wheel", "unpack", "-d", str(unpacked), str(wheel)], check=True
        )
        (package,) = unpacked.iterdir()
        (metadata_path,) = package.glob("*.dist-info/WHEEL")
        metadata = BytesParser().parsebytes(metadata_path.read_bytes())
        metadata.replace_header("Root-Is-Purelib", "false")
        metadata_path.write_bytes(metadata.as_bytes())
        repacked = output_directory / "repacked"
        repacked.mkdir()
        subprocess.run(  # nosec B603 - the current interpreter runs the installed wheel tool.
            [sys.executable, "-m", "wheel", "pack", "-d", str(repacked), str(package)], check=True
        )
        (wheel,) = repacked.glob("*.whl")
        return wheel

    def check(self, wheel: Path, tag: str, bundled_libraries: list[str]) -> None:
        """Checks the wheel carries exactly the extension modules its tag promises, and the shared
        libraries bundled with them.

        Args:
            wheel: The wheel.
            tag: Its tag.
            bundled_libraries: The wheel paths of the libraries maturin bundled with the extensions.

        Raises:
            SystemExit: The wheel misses an extension or a bundled library.
        """
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_paths = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_paths) != 1:
                raise SystemExit(f"{wheel.name} must carry exactly one WHEEL metadata file")
            metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        expected_tags = {"-".join(parts) for parts in product(*(part.split(".") for part in tag.split("-")))}
        if set(metadata.get_all("Tag", [])) != expected_tags:
            raise SystemExit(f"{wheel.name} metadata tags differ from {tag}")
        if metadata.get("Root-Is-Purelib") != "false":
            raise SystemExit(f"{wheel.name} has the wrong Root-Is-Purelib installation scheme")
        extensions = [name for name in names if name.startswith("rust/") and name.endswith(self.EXTENSION_SUFFIXES)]
        libraries = sorted(name for name in names if name.startswith(f"{self.BUNDLED_LIBRARIES_DIRECTORY_NAME}/"))
        if libraries != sorted(bundled_libraries):
            raise SystemExit(f"{wheel.name} carries the libraries {libraries}, expected {sorted(bundled_libraries)}")
        modules = sorted(Path(name).name.split(".")[0] for name in extensions)
        if modules != sorted(self.EXTENSION_MODULES):
            raise SystemExit(f"{wheel.name} carries {extensions}, expected one file per {self.EXTENSION_MODULES}")
        # rust/native.cpython-312-x86_64-linux-gnu.so, rust/native.cpython-312-darwin.so,
        # rust/native.cp312-win_amd64.pyd
        python_version_digits = tag.split("-")[0].removeprefix("cp")
        other_version_extensions = [
            name for name in extensions if f"cp{python_version_digits}-" not in name.replace("cpython-", "cp")
        ]
        if other_version_extensions:
            raise SystemExit(f"{wheel.name} carries extensions of another Python: {other_version_extensions}")
        print(f"built {wheel.name}: {extensions + libraries}")

    def get_specs(self, extension_wheels: Path) -> tuple[BuildSpec, ...]:
        """Describe every requested build before staging or invoking build tools."""
        return tuple(
            BuildSpec(tag, tuple(module_wheels.items()))
            for tag, module_wheels in self.get_extension_wheels_by_tag(extension_wheels).items()
        )

    def publish(self, wheel: Path) -> None:
        """Atomically replace the output wheel after its contents have been verified."""
        self.out.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.out, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
        try:
            shutil.copy2(wheel, temporary)
            os.replace(temporary, self.out / wheel.name)
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, extension_wheels: Path) -> None:
        """Build each ABI in a fresh directory; failures leave the checkout untouched."""
        for spec in self.get_specs(extension_wheels):
            with tempfile.TemporaryDirectory(prefix="hare-wheel-") as directory:
                staging = Path(directory)
                source = staging / "source"
                self.stage_source(source)
                libraries = self.extract_extensions(spec, source)
                wheel = self.build(spec, source, staging / "wheels")
                self.check(wheel, spec.tag, libraries)
                self.publish(wheel)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Builds the hare-orm wheels of a release.")
    parser.add_argument("--out", type=Path, required=True, help="The directory the wheels are written to.")
    parser.add_argument(
        "--extension-wheels",
        type=Path,
        required=True,
        help="The directory with maturin's rust.native wheels of one platform, one per Python version.",
    )
    arguments = parser.parse_args()
    WheelBuilder(Path(__file__).resolve().parents[2], arguments.out.resolve()).run(
        arguments.extension_wheels.resolve()
    )
