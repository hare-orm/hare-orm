"""The ``hare stubs`` command: ``--check`` finding outdated stubs, ``--relation-depth`` checked."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.stubs.stub_configs import CONFIG, STUB_PATH
from tests.utils.cli_commands import CliCommands

pytestmark = pytest.mark.database_independent


def write_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_name = f"stub_settings_{tmp_path.name}"
    (tmp_path / f"{module_name}.py").write_text(f"HARE_ORM = {CONFIG!r}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return f"{module_name}.HARE_ORM"


@pytest.mark.asyncio
async def test_check_reports_an_outdated_stub(tmp_path, monkeypatch):
    config = write_settings(tmp_path, monkeypatch)
    output = (tmp_path / "typings").as_posix()

    missing = await CliCommands.run(["-c", config, "stubs", "--output", output, "--check"], project=tmp_path)
    assert missing.exit_code == 1
    assert "Outdated stub" in missing.output

    written = await CliCommands.run(["-c", config, "stubs", "--output", output], project=tmp_path)
    assert written.exit_code == 0
    checked = await CliCommands.run(["-c", config, "stubs", "--output", output, "--check"], project=tmp_path)
    assert checked.exit_code == 0

    stub_path = tmp_path.joinpath("typings", *STUB_PATH)
    stub_path.write_text(stub_path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    checked = await CliCommands.run(["-c", config, "stubs", "--output", output, "--check"], project=tmp_path)
    assert checked.exit_code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["-1", "6"])
async def test_the_relation_depth_is_checked(tmp_path, monkeypatch, depth):
    config = write_settings(tmp_path, monkeypatch)
    result = await CliCommands.run(["-c", config, "stubs", "--relation-depth", depth], project=tmp_path)
    assert result.exit_code == 2
    assert "--relation-depth" in result.output
