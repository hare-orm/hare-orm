from __future__ import annotations

import runpy
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.database_independent


@pytest.fixture
def wheel_builder(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[2] / ".github/scripts/build_wheel.py"))
    repository = tmp_path / "checkout"
    repository.mkdir()
    (repository / "hare").mkdir()
    (repository / "hare/__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (repository / "hare/__pycache__").mkdir()
    (repository / "hare/__pycache__/local.pyc").write_bytes(b"local bytecode")
    (repository / "pyproject.toml").write_text(
        '[project]\nreadme = "README.md"\n[tool.poetry]\npackages = [{ include = "hare" }]\n', encoding="utf-8"
    )
    (repository / "README.md").write_text("Readme\n", encoding="utf-8")
    (repository / "LICENSE").write_text("License\n", encoding="utf-8")
    (repository / "rust").mkdir()
    (repository / "rust/native.cp314-win_amd64.pyd").write_bytes(b"checkout extension")
    (repository / "hare_native.libs").mkdir()
    (repository / "hare_native.libs/existing.so").write_bytes(b"checkout library")
    return module["WheelBuilder"](repository, tmp_path / "dist")


def test_source_staging_keeps_metadata_but_excludes_local_artifacts(wheel_builder, tmp_path):
    destination = tmp_path / "stage"
    wheel_builder.stage_source(destination)
    assert {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()} == {
        "hare/__init__.py",
        "pyproject.toml",
        "README.md",
        "LICENSE",
    }
    assert (wheel_builder.repository / "rust/native.cp314-win_amd64.pyd").read_bytes() == b"checkout extension"
    assert (wheel_builder.repository / "hare_native.libs/existing.so").read_bytes() == b"checkout library"


@pytest.mark.parametrize("failure", ["build", "check", None])
def test_failed_or_successful_build_never_mutates_checkout(wheel_builder, monkeypatch, failure):
    original = {
        path.relative_to(wheel_builder.repository): path.read_bytes()
        for path in wheel_builder.repository.rglob("*")
        if path.is_file()
    }
    wheel_builder.out.mkdir()
    published = wheel_builder.out / "hare_orm-1-py3-none-any.whl"
    published.write_bytes(b"previous wheel")

    def build(spec, source, output):
        assert source != wheel_builder.repository
        assert not (source / "rust").exists()
        if failure == "build":
            raise RuntimeError("build failed")
        output.mkdir()
        wheel = output / published.name
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "hare_orm-1.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
            )
            archive.writestr("hare/__init__.py", "VALUE = 42")
            if failure == "check":
                archive.writestr("rust/native.cp314-win_amd64.pyd", b"unexpected")
        return wheel

    monkeypatch.setattr(wheel_builder, "build", build)
    if failure is None:
        wheel_builder.run(None)
        assert zipfile.is_zipfile(published)
    else:
        with pytest.raises((RuntimeError, SystemExit)):
            wheel_builder.run(None)
        assert published.read_bytes() == b"previous wheel"
    assert {
        path.relative_to(wheel_builder.repository): path.read_bytes()
        for path in wheel_builder.repository.rglob("*")
        if path.is_file()
    } == original
    assert not list(wheel_builder.out.glob("*.tmp"))


def test_each_abi_uses_only_its_selected_extension_and_libraries(wheel_builder, monkeypatch, tmp_path):
    archives = tmp_path / "archives"
    archives.mkdir()
    for version in ("312", "313"):
        with zipfile.ZipFile(archives / f"hare_native-1-cp{version}-cp{version}-win_amd64.whl", "w") as archive:
            archive.writestr(f"rust/native.cp{version}-win_amd64.pyd", version.encode())
            archive.writestr(f"hare_native.libs/helper-{version}.dll", b"helper")
    staged = []

    def build(spec, source, output):
        files = [
            path
            for directory in (source / "rust", source / "hare_native.libs")
            for path in directory.rglob("*")
            if path.is_file()
        ]
        staged.append({path.relative_to(source).as_posix() for path in files})
        output.mkdir()
        wheel = output / f"hare_orm-1-{spec.tag}.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "hare_orm-1.dist-info/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {spec.tag}\n"
            )
            for path in files:
                archive.write(path, path.relative_to(source).as_posix())
        return wheel

    monkeypatch.setattr(wheel_builder, "build", build)
    wheel_builder.run(archives)
    assert staged == [
        {f"rust/native.cp{version}-win_amd64.pyd", f"hare_native.libs/helper-{version}.dll"}
        for version in ("312", "313")
    ]
    assert len(list(wheel_builder.out.glob("*.whl"))) == 2


def test_archive_library_cannot_escape_staging(wheel_builder, tmp_path):
    archives = tmp_path / "archives"
    archives.mkdir()
    with zipfile.ZipFile(archives / "hare_native-1-cp314-cp314-win_amd64.whl", "w") as archive:
        archive.writestr("rust/native.cp314-win_amd64.pyd", b"extension")
        archive.writestr("hare_native.libs/../../escaped", b"invalid")
    stage = tmp_path / "stage"
    wheel_builder.stage_source(stage)
    [spec] = wheel_builder.get_specs(archives)
    with pytest.raises(SystemExit, match="invalid bundled library path"):
        wheel_builder.extract_extensions(spec, stage)
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize(
    ("purelib", "tag", "message"),
    [("false", "py3-none-any", "installation scheme"), ("true", "cp314-cp314-win_amd64", "metadata tags")],
)
def test_wheel_metadata_must_match_the_build_spec(wheel_builder, tmp_path, purelib, tag, message):
    wheel = tmp_path / "hare_orm-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hare_orm-1.dist-info/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: {purelib}\nTag: {tag}\n")
    with pytest.raises(SystemExit, match=message):
        wheel_builder.check(wheel, None, [])
