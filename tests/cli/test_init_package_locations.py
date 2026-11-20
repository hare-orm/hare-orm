"""Migration packages follow import resolution and the configured model source."""

import importlib.util
import json
import zipfile

import pytest

from tests.utils.cli_commands import CliCommands
from tests.utils.project_imports import ProjectImports

pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


@pytest.mark.parametrize("layout", ["plain", "package", "namespace-first", "namespace-second"])
@pytest.mark.parametrize("existing", ["missing", "package", "module"])
async def test_init_preserves_existing_imports_and_locates_inferred_packages(tmp_path, monkeypatch, layout, existing):
    first, second, launch = (tmp_path / name for name in ("first", "second", "launch"))
    for path in (first, second, launch):
        path.mkdir()
    if layout == "plain":
        model_source = first / "package_models.py"
        model_name, migrations_name = "package_models", "migrations"
    else:
        for root in (first, second):
            (root / "library").mkdir()
        if layout == "package":
            (first / "library/__init__.py").write_text("", encoding="utf-8")
        source_root = second if layout == "namespace-second" else first
        model_source = source_root / "library/models.py"
        model_name, migrations_name = "library.models", "library.migrations"
    model_source.write_text(
        "from hare import Model, fields\nclass Entry(Model):\n    title = fields.TextField()\n", encoding="utf-8"
    )
    expected = model_source.parent / "migrations"
    marker = b"marker = 'preserved'\n"
    if existing == "package":
        expected.mkdir()
        (expected / "__init__.py").write_bytes(marker)
        (expected / "0001_initial.py").write_bytes(marker)
    elif existing == "module":
        expected.with_suffix(".py").write_bytes(marker)
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"books": {"models": [model_name]}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(second))
    monkeypatch.syspath_prepend(str(first))
    monkeypatch.chdir(launch)
    with ProjectImports(first).isolate_modules(), ProjectImports(second).isolate_modules():
        before = importlib.util.find_spec(migrations_name)
        result = await CliCommands.run(["-c", str(config), "init", "books"])
        after = importlib.util.find_spec(migrations_name)
        if existing == "module":
            assert result.exit_code == 1
            assert "not a package" in result.stderr
            assert not expected.exists()
            assert expected.with_suffix(".py").read_bytes() == marker
            assert after.origin == before.origin
        else:
            assert result.exit_code == 0, result.stderr
            assert (expected / "__init__.py").is_file()
            assert after.origin == str(expected / "__init__.py")
            if existing == "package":
                assert after.origin == before.origin
                assert (expected / "__init__.py").read_bytes() == marker
                assert (expected / "0001_initial.py").read_bytes() == marker
        assert not (launch / "migrations").exists()
        if layout == "namespace-second":
            assert not (first / "library/migrations").exists()


@pytest.mark.parametrize("layout", ["file-target", "zip-parent"])
async def test_init_reports_unwritable_package_locations_without_raw_filesystem_errors(tmp_path, monkeypatch, layout):
    marker = b"preserved source\n"
    if layout == "file-target":
        (tmp_path / "package_models.py").write_text("", encoding="utf-8")
        target = tmp_path / "destination"
        target.write_bytes(marker)
        models = "package_models"
        migrations = "destination"
        monkeypatch.syspath_prepend(str(tmp_path))
    else:
        target = tmp_path / "source.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("library/__init__.py", "")
            archive.writestr("library/models.py", "")
        marker = target.read_bytes()
        models, migrations = "library.models", "library.migrations"
        monkeypatch.syspath_prepend(str(target))
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"books": {"models": [models], "migrations": migrations}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with ProjectImports(tmp_path).isolate_modules():
        result = await CliCommands.run(["-c", str(config), "init", "books"])
    assert result.exit_code == 1
    assert "Cannot create migrations package" in result.stderr
    assert "Traceback" not in result.stderr
    assert target.read_bytes() == marker
