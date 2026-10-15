"""``hare stubs``: each module of models as pyright reads it - the fields of a model typed, its queryset
taking the keys of its filters and the values of its writes - and ``--check`` finding outdated stubs."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import hare.cli.hare_cli as cli_module
from hare import fields
from hare.core.config import HareConfig
from hare.query.queryset.extensions import QuerySetExtensions
from hare.stubs.stub_imports import StubImports
from hare.stubs.stub_writer import StubWriter
from hare.stubs.value_type_text import ValueTypeText
from hare.typing_info.bound_models import BoundModels
from tests.contrib.mypy.hare_config import HARE_CONFIG

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


def test_a_stub_types_the_primary_key_of_an_instance(tmp_path):
    text = write_stubs(tmp_path).read_text(encoding="utf-8")
    assert "    def pk(self) -> int: ..." in text
    assert "    def pk(self, value: int | None) -> None: ..." in text
    # A composite key is a tuple of its fields' values - the CompositePrimaryKey of the class is gone.
    assert "    def pk(self) -> tuple[int, int]: ..." in text
    assert "    def pk(self, value: tuple[int, int] | None) -> None: ..." in text
    assert "CompositePrimaryKey" not in text


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


STUB_WRITING_SCRIPT = """
import sys
from pathlib import Path

from hare.core.config import HareConfig
from hare.stubs.stub_imports import StubImports
from hare.stubs.stub_writer import StubWriter
from hare.stubs.value_type_text import ValueTypeText
from hare.typing_info.bound_models import BoundModels
from tests.contrib.mypy.hare_config import HARE_CONFIG

config = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.testmodels"]}},
}
StubWriter(BoundModels(HareConfig.load(config)), Path(sys.argv[1]), 2).write()
"""


def test_a_stub_is_the_same_text_whatever_the_hash_seed(tmp_path):
    # A model's relations are a set - written in the order the model declares them, not the set's.
    script = tmp_path / "write_stubs.py"
    script.write_text(STUB_WRITING_SCRIPT, encoding="utf-8")
    repository = Path(__file__).resolve().parent.parent
    stub_texts = []
    for seed in ("1", "2", "3"):
        output_directory = tmp_path / f"typings_{seed}"
        subprocess.run(
            [sys.executable, str(script), str(output_directory)],
            cwd=repository,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(repository)},
            check=True,
        )
        stub_texts.append(output_directory.joinpath("tests", "testmodels.pyi").read_text(encoding="utf-8"))
    assert stub_texts[0] == stub_texts[1] == stub_texts[2]


def test_a_class_named_like_one_of_the_stubs_own_is_imported_under_an_alias(monkeypatch):
    shop_module = types.ModuleType("stub_clash_shop")
    stock_module = types.ModuleType("stub_clash_stock")
    other_module = types.ModuleType("stub_clash_other")
    for module in (shop_module, stock_module, other_module):
        module.Item = type("Item", (), {"__module__": module.__name__})
        monkeypatch.setitem(sys.modules, module.__name__, module)
    stock_module.Shared = shop_module.Item
    imports = StubImports("stub_clash_stock")

    assert imports.add_class(shop_module.Item) == "stub_clash_shop_Item"
    assert imports.add_class(stock_module.Item) == "Item"
    assert imports.add_class(other_module.Item) == "stub_clash_other_Item"
    assert imports.add("typing", "Any") == "Any"
    assert imports.render() == [
        "from stub_clash_other import Item as stub_clash_other_Item",
        "from stub_clash_shop import Item as stub_clash_shop_Item",
        "from typing import Any",
    ]


def test_a_declaration_named_like_a_class_of_the_module_takes_a_free_name(monkeypatch):
    """A model named BookFilters was shadowed by the TypedDict of Book's filter keys."""
    module = types.ModuleType("stub_declarations")
    module.BookFilters = type("BookFilters", (), {"__module__": module.__name__})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    imports = StubImports("stub_declarations")

    assert imports.declare("BookFilters") == "BookFilters_"
    assert imports.declare("BookFilters") == "BookFilters__"
    assert imports.declare("BookQuerySet") == "BookQuerySet"
    # An import named like a declaration is imported under an alias.
    assert imports.add("somewhere", "BookQuerySet") == "somewhere_BookQuerySet"


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


def test_a_stub_types_a_container_field_as_the_whole_container(tmp_path):
    StubWriter(BoundModels(HareConfig.load(HARE_CONFIG)), tmp_path, 1).write()
    text = tmp_path.joinpath("tests", "contrib", "mypy", "models.pyi").read_text(encoding="utf-8")
    assert "    labels: Field[list[str] | None]" in text
    assert "    grid: Field[list[list[int | None]]]" in text
    assert "    prices: Field[dict[str, int]]" in text
    assert "    point: Field[tuple[int, str]]" in text
    assert "def values_list(self, field: Literal['labels'], /, *, flat: Literal[True]" in text
    assert "QuerySet[Crate, list[str] | None, Any]" in text


def test_a_class_made_at_run_time_is_written_as_any():
    named_point = fields.TupleField({"x": fields.IntField(), "y": fields.IntField()})
    value_type_text = ValueTypeText(StubImports("tests.factories_models"))
    assert value_type_text.get_class_text(list[named_point.get_python_type()]) == "list[Any]"


MANAGERS_CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.stub_managers_models"]}},
}
MANAGERS_USAGE = """
from tests.stub_managers_models import Article


async def main(article: Article) -> None:
    reveal_type(Article.objects.published())
    reveal_type(Article.recent_ones.recent())
    reveal_type(article.note)
    await Article.objects.filter(titel="x")
"""


def write_manager_stubs(output_directory: Path) -> str:
    StubWriter(BoundModels(HareConfig.load(MANAGERS_CONFIG)), output_directory, 1).write()
    return output_directory.joinpath("tests", "stub_managers_models.pyi").read_text(encoding="utf-8")


def test_a_stub_declares_each_manager_once_with_the_queryset_class_of_the_project(tmp_path):
    text = write_manager_stubs(tmp_path)
    # The queryset class of the project's own is the stub queryset's base - its methods stay.
    assert "class ArticleQuerySet_(ArticleQuerySet):" in text
    assert text.count("    objects:") == 1
    assert "    objects: ClassVar[ArticleQuerySet_]" in text
    assert "    recent_ones: ClassVar[RecentQuerySet[Article]]" in text
    assert ": Incomplete" not in text


def test_a_stub_types_the_fields_of_an_abstract_base(tmp_path):
    text = write_manager_stubs(tmp_path)
    assert "    created: Field[datetime | None]" in text
    assert "    note: Field[str | None]" in text


@pytest.mark.skipif(
    importlib.util.find_spec("pyright") is None or shutil.which("node") is None,
    reason="pyright and node aren't installed",
)
def test_pyright_reads_the_managers_and_the_abstract_fields_through_the_stubs(tmp_path):
    write_manager_stubs(tmp_path / "typings")
    (tmp_path / "usage.py").write_text(MANAGERS_USAGE, encoding="utf-8")
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
    errors = [diagnostic["range"]["start"]["line"] for diagnostic in diagnostics if diagnostic["severity"] == "error"]
    revealed = [diagnostic["message"] for diagnostic in diagnostics if diagnostic["severity"] == "information"]
    # Only the unknown filter key.
    assert errors == [8]
    assert '"ArticleQuerySet"' in revealed[0]
    assert '"RecentQuerySet[Article]"' in revealed[1]
    assert '"str | None"' in revealed[2]


def test_a_stub_takes_the_keys_of_a_generic_foreign_key_and_the_primary_key(tmp_path):
    StubWriter(BoundModels(HareConfig.load(HARE_CONFIG)), tmp_path, 1).write()
    text = tmp_path.joinpath("tests", "contrib", "mypy", "models.pyi").read_text(encoding="utf-8")
    filters = text.partition("NoteFilters = TypedDict(")[2].partition(")")[0]
    assert "'subject': dict | Volume | Writer | Expression | Term | QuerySpecification," in filters
    assert "'subject__in': Iterable[dict | Volume | Writer] | Expression | Term | QuerySpecification," in filters
    assert "'subject__isnull': bool | Expression | Term | QuerySpecification," in filters
    assert "'subject__type': Literal['volume', 'writer'] | Expression | Term | QuerySpecification," in filters
    assert "'subject__type__not_isnull': bool | Expression | Term | QuerySpecification," in filters
    writes = text.partition("NoteWrites = TypedDict(")[2].partition(")")[0]
    assert "'subject': dict | Volume | Writer," in writes
    assert "'pk': int," in writes
    printing_writes = text.partition("PrintingWrites = TypedDict(")[2].partition(")")[0]
    assert "'pk': tuple[int, int]," in printing_writes
