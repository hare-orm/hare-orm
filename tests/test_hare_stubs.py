"""``hare stubs``: each module of models as pyright reads it - the fields of a model typed, its queryset
taking the keys of its filters and the values of its writes - and ``--check`` finding outdated stubs."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hare.cli.hare_cli as cli_module
from hare.core.config import HareConfig
from hare.query.queryset.extensions import QuerySetExtensions
from hare.stubs.stub_writer import StubWriter
from hare.typing_info.bound_models import BoundModels

pytestmark = pytest.mark.database_independent

CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.factories_models"]}},
}
STUB_PATH = ("tests", "factories_models.pyi")
USAGE = """
from tests.factories_models import Member, Team


async def main() -> None:
    team = await Team.objects.get(name="core")
    await Member.objects.filter(team=team, name__icontains="a", team__name="core", team__in=[team, 1])
    names = await Member.objects.values_list("name", flat=True)
    reveal_type(names)
    reveal_type(team.name)
    await Member.objects.create(name="ann", email="a@b.c", team=team)
    await Member.objects.filter(nme="x")
    await Member.objects.filter(is_staff="yes")
    await Member.objects.create(name=1)
"""


def write_stubs(output_directory: Path) -> Path:
    StubWriter(BoundModels(HareConfig.load(CONFIG)), output_directory, 2).write()
    return output_directory.joinpath(*STUB_PATH)


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


def test_a_stub_types_the_fields_and_the_queryset(tmp_path):
    text = write_stubs(tmp_path).read_text(encoding="utf-8")
    assert "    objects: ClassVar[MemberQuerySet]" in text
    assert "    team: Field[Team]" in text
    assert "    name: Field[str]" in text
    assert "class MemberQuerySet(QuerySet[Member, Member, Any]):" in text
    assert "'team__name': str | Expression | Term | QuerySpecification," in text
    assert "'team__in': Iterable[int | Team] | Expression | Term | QuerySpecification," in text
    assert "'name__in': Iterable[str] | Expression | Term | QuerySpecification," in text
    assert "'team_id': int," in text
    assert "def values_list(self, field: Literal['name'], /, *, flat: Literal[True]" in text
    # Every other name of the module stays - pyright reads the stub instead of it.
    assert "class StoreItem(Model):" in text
    compile(text, "factories_models.pyi", "exec")


def test_the_relation_depth_bounds_the_keys(tmp_path):
    StubWriter(BoundModels(HareConfig.load(CONFIG)), tmp_path, 0).write()
    text = tmp_path.joinpath(*STUB_PATH).read_text(encoding="utf-8")
    assert "'team__name'" not in text
    assert "'team':" in text


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


@pytest.mark.skipif(
    importlib.util.find_spec("pyright") is None or shutil.which("node") is None,
    reason="pyright and node aren't installed",
)
def test_pyright_checks_queries_through_the_stubs(tmp_path):
    write_stubs(tmp_path / "typings")
    (tmp_path / "usage.py").write_text(USAGE, encoding="utf-8")
    repository = Path(__file__).resolve().parent.parent
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({"stubPath": "typings", "include": ["usage.py"], "extraPaths": [str(repository)]}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", "--pythonpath", sys.executable],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    diagnostics = json.loads(completed.stdout)["generalDiagnostics"]
    errors = sorted(
        (diagnostic["range"]["start"]["line"], diagnostic["rule"])
        for diagnostic in diagnostics
        if diagnostic["severity"] == "error"
    )
    revealed = [diagnostic["message"] for diagnostic in diagnostics if diagnostic["severity"] == "information"]
    # The unknown key, the wrong value of a filter, the wrong value of a write - nothing else.
    assert errors == [(11, "reportCallIssue"), (12, "reportArgumentType"), (13, "reportArgumentType")]
    assert any("list[str]" in message for message in revealed)
    assert sum('"str"' in message for message in revealed) == 1


def test_a_stub_declares_the_methods_of_the_dialect(tmp_path, monkeypatch):
    def first_rows(builder, name_below, /, limit=10, *, inclusive=False):
        return builder

    def first_matching(builder, extension_query, condition):
        return builder

    monkeypatch.setattr(QuerySetExtensions, "registered", {})
    QuerySetExtensions.register("first_rows", "sqlite", first_rows)
    QuerySetExtensions.register("first_matching", "sqlite", first_matching, reads_query=True, takes_condition=True)
    QuerySetExtensions.register("only_on_postgresql", "postgresql", first_rows)
    text = write_stubs(tmp_path).read_text(encoding="utf-8")
    assert (
        "    def first_rows(self, name_below: Any, /, limit: Any = ..., *, inclusive: Any = ...) -> Self: ..." in text
    )
    assert "    def first_matching(self, *args: Q | Exists, **kwargs: Unpack[MemberFilters]) -> Self: ..." in text
    assert "only_on_postgresql" not in text
    compile(text, "factories_models.pyi", "exec")
