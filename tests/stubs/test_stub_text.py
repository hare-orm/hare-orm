"""``hare stubs``: the text of a module's stub - the fields of a model typed, its queryset taking the keys
of its filters and the values of its writes, its managers and the methods of its dialect declared."""

from __future__ import annotations

import pytest

from hare import fields
from hare.core.config import HareConfig
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.query.queryset.extensions import QuerySetExtensions
from hare.stubs.stub_imports import StubImports
from hare.stubs.stub_writer import StubWriter
from hare.stubs.value_type_text import ValueTypeText
from hare.typing_info.bound_models import BoundModels
from tests.stubs.stub_configs import CONFIG, MANAGERS_STUB_PATH, STUB_PATH, write_stubs

pytestmark = pytest.mark.database_independent


def test_a_stub_types_the_fields_and_the_queryset(stubs_path):
    text = stubs_path.joinpath(*STUB_PATH).read_text(encoding="utf-8")
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


def test_a_stub_types_the_primary_key_of_an_instance(stubs_path):
    text = stubs_path.joinpath(*STUB_PATH).read_text(encoding="utf-8")
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


def test_a_stub_declares_the_methods_of_the_dialect(tmp_path, monkeypatch):
    def first_rows(builder, name_below, /, limit=10, *, inclusive=False):
        return builder

    def first_matching(builder, extension_query, condition):
        return builder

    monkeypatch.setattr(QuerySetExtensions, "registered", {})
    QuerySetExtensions.register("first_rows", SqliteDialect, first_rows)
    QuerySetExtensions.register(
        "first_matching", SqliteDialect, first_matching, reads_query=True, takes_condition=True
    )
    QuerySetExtensions.register("only_on_postgresql", PostgresqlDialect, first_rows)
    text = write_stubs(tmp_path).read_text(encoding="utf-8")
    assert (
        "    def first_rows(self, name_below: Any, /, limit: Any = ..., *, inclusive: Any = ...) -> Self: ..." in text
    )
    assert "    def first_matching(self, *args: Q | Exists, **kwargs: Unpack[MemberFilters]) -> Self: ..." in text
    assert "only_on_postgresql" not in text
    compile(text, "factories_models.pyi", "exec")


def test_a_stub_types_a_container_field_as_the_whole_container(mypy_models_stub_text):
    text = mypy_models_stub_text
    assert "    labels: Field[list[str] | None]" in text
    assert "    grid: Field[list[list[int | None]]]" in text
    assert "    prices: Field[dict[str, int]]" in text
    assert "    point: Field[tuple[int, str]]" in text
    assert "def values_list(self, field: Literal['labels'], /, *, flat: Literal[True]" in text
    assert "QuerySet[Crate, list[str] | None, Any]" in text


def test_a_class_made_at_run_time_is_written_as_any():
    named_point = fields.TupleField({"x": fields.IntField(), "y": fields.IntField()})
    value_type_text = ValueTypeText(StubImports("tests.contrib.factories_models"))
    assert value_type_text.get_class_text(list[named_point.get_python_type()]) == "list[Any]"


def test_a_stub_declares_each_manager_once_with_the_queryset_class_of_the_project(manager_stubs_path):
    text = manager_stubs_path.joinpath(*MANAGERS_STUB_PATH).read_text(encoding="utf-8")
    # The queryset class of the project's own is the stub queryset's base - its methods stay.
    assert "class ArticleQuerySet_(ArticleQuerySet):" in text
    assert text.count("    objects:") == 1
    assert "    objects: ClassVar[ArticleQuerySet_]" in text
    assert "    recent_ones: ClassVar[RecentQuerySet[Article]]" in text
    assert ": Incomplete" not in text


def test_a_stub_types_the_fields_of_an_abstract_base(manager_stubs_path):
    text = manager_stubs_path.joinpath(*MANAGERS_STUB_PATH).read_text(encoding="utf-8")
    assert "    created: Field[datetime | None]" in text
    assert "    note: Field[str | None]" in text


def test_a_stub_takes_the_keys_of_a_generic_foreign_key_and_the_primary_key(mypy_models_stub_text):
    text = mypy_models_stub_text
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
