"""The ``hare stubs`` command: ``--check`` finding outdated stubs, ``--relation-depth`` checked."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import hare.cli.hare_cli as cli_module
from tests.stubs.stub_configs import CONFIG, STUB_PATH

pytestmark = pytest.mark.database_independent


async def run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())


def write_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    module_name = f"stub_settings_{tmp_path.name}"
    (tmp_path / f"{module_name}.py").write_text(f"HARE_ORM = {CONFIG!r}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return f"{module_name}.HARE_ORM"


@pytest.mark.asyncio
async def test_check_reports_an_outdated_stub(tmp_path, monkeypatch):
    config = write_settings(tmp_path, monkeypatch)
    output = (tmp_path / "typings").as_posix()

    missing = await run_cli(["-c", config, "stubs", "--output", output, "--check"])
    assert missing.exit_code == 1
    assert "Outdated stub" in missing.output

    written = await run_cli(["-c", config, "stubs", "--output", output])
    assert written.exit_code == 0
    assert (await run_cli(["-c", config, "stubs", "--output", output, "--check"])).exit_code == 0

    stub_path = tmp_path.joinpath("typings", *STUB_PATH)
    stub_path.write_text(stub_path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert (await run_cli(["-c", config, "stubs", "--output", output, "--check"])).exit_code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["-1", "6"])
async def test_the_relation_depth_is_checked(tmp_path, monkeypatch, depth):
    config = write_settings(tmp_path, monkeypatch)
    result = await run_cli(["-c", config, "stubs", "--relation-depth", depth])
    assert result.exit_code == 2
    assert "--relation-depth" in result.output
