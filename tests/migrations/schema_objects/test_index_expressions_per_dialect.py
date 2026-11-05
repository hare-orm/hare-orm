"""An index's key expressions are kept as declared and rendered by the database the DDL runs on.

Migration files used to keep an expression index's keys as SQL text rendered for SQLite, applied
as-is to any database."""

import sqlite3

import pytest

from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.ddl.indexes import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.declarations.import_manager import ImportManager
from hare.dialects.base.renderers import TermRenderers
from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import UnSupportedError
from hare.fields import CharField, DateField, FloatField, IntField, JSONField
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import AddIndex, AlterModelTable, CreateModel, RemoveIndex
from hare.migrations.state.state import State
from hare.query.expressions import F, Q, Value
from hare.query.functions import Cast, Concat, Lower, Upper
from hare.query.functions.datetime.extract import Extract
from hare.query.functions.datetime.trunc import Trunc
from hare.sql import functions
from tests.migrations.round_trips.test_round_trip_real_db import build_model
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent


class LowerCaseNamingDialect(SqlDialect):
    """A dialect of its own calling LOWER by another name."""

    name = "lower_case_naming"

    def build_renderers(self) -> TermRenderers:
        renderers = TermRenderers(self)
        renderers.register_name(functions.Lower, lambda function, ctx: "LCASE")
        return renderers


LOWER_CASE_NAMING_DIALECT = LowerCaseNamingDialect()
DialectRegistry.register_dialect(LOWER_CASE_NAMING_DIALECT)


def build_indexed_model():
    return build_model(
        "ExpressionIndexed",
        "expression_indexed",
        {"name": CharField(max_length=20)},
        {"indexes": [Index(Lower("name"), F("name").desc(nulls_first=True))]},
    )


def test_migration_file_keeps_the_declared_expressions():
    index = build_indexed_model()._meta.indexes[0]

    rendered = DeclarationValueSerializer.render_call(*index.deconstruct(), ImportManager())

    assert rendered == "Index(Lower('name'), F('name').desc(nulls_first=True))"


def test_each_dialect_renders_its_own_keys():
    model = build_indexed_model()
    index = model._meta.indexes[0]

    assert index.get_key_sqls(model, LOWER_CASE_NAMING_DIALECT) == [
        '(LCASE("name"))',
        '("name") DESC NULLS FIRST',
    ]
    assert index.get_key_sqls(model, SQLITE_DIALECT)[0] == '(LOWER("name"))'


def test_the_generated_name_is_the_same_on_every_dialect():
    model = build_indexed_model()
    index = model._meta.indexes[0]
    name_before = index.get_generated_name(model)

    index.get_key_sqls(model, LOWER_CASE_NAMING_DIALECT)

    assert index.get_generated_name(model) == name_before


def test_shared_index_keeps_its_declaration_across_model_bindings():
    index = Index(Lower("title"))
    equivalent = Index(Lower("title"))
    original = dict(vars(index))
    declarations = {index}
    names = []
    for column in ("first_title", "second_title"):
        state = State()
        CreateModel(
            "Book",
            [("title", CharField(max_length=40, source_field=column))],
            options={"table": "book", "indexes": [index]},
        ).state_forward("models", state)
        model = state.apps.get_model("models.Book")
        assert index.get_key_names(model) == [f'(LOWER("{column}"))']
        assert column in index.get_key_sqls(model, SQLITE_DIALECT)[0]
        names.append(index.get_generated_name(model))
        assert vars(index) == original
        assert equivalent in declarations
    assert names[0] != names[1]


@pytest.mark.parametrize("change_table", [False, True])
def test_expression_index_replacement_needs_no_model_rendering(change_table):
    state = State()
    CreateModel(
        "Book",
        [("title", CharField(max_length=40, source_field="book_title"))],
        options={"table": "books", "indexes": [Index(Lower("title"))]},
    ).state_forward("models", state)
    target = state.clone()
    target.models["models", "Book"].options["indexes"] = [Index(Upper("title"))]
    if change_table:
        AlterModelTable("Book", "library_books").state_forward("models", target)
    operations = OperationGenerator(state, target).generate()
    assert any(isinstance(operation, RemoveIndex) and operation.index is not None for operation in operations)
    assert any(isinstance(operation, AddIndex) for operation in operations)
    assert state.rendering.apps is None
    assert target.rendering.apps is None
    replay = state.clone()
    for operation in operations:
        operation.state_forward("models", replay)
    assert OperationGenerator(replay, target).generate() == []


def build_function_indexed_model():
    return build_model(
        "FunctionIndexed",
        "function_indexed",
        {"made": DateField(null=True), "ratio": FloatField(null=True), "data": JSONField(null=True)},
        {
            "indexes": [
                Index(Trunc("made", "month")),
                Index(Trunc("made", "day")),
                Index(Extract("made", "year")),
                Index(Cast("ratio", IntField())),
                Index(F("data__key")),
            ]
        },
    )


def test_keys_of_dialect_functions_are_named_apart():
    model = build_function_indexed_model()
    names = []
    for index in model._meta.indexes:
        names.append(index.get_generated_name(model))

    assert len(set(names)) == len(names)


def test_a_key_needing_a_function_of_hare_connections_is_refused_in_ddl():
    model = build_function_indexed_model()
    trunc_index, *_others, json_index = model._meta.indexes

    with pytest.raises(UnSupportedError, match="hare_trunc_date_part"):
        trunc_index.get_key_sqls(model, SQLITE_DIALECT)
    assert json_index.get_key_sqls(model, SQLITE_DIALECT)[0].startswith("(CASE json_type(")


def test_renaming_a_field_rewrites_only_what_an_expression_reads_as_a_field():
    renamed = ExpressionFieldRenaming.get_renamed(
        [Trunc("month", "month"), Lower("month").desc(), Concat(Lower("month"), Value("month")), F("month__key")],
        "month",
        "period",
    )

    assert repr(renamed) == (
        "[Trunc('period', 'month'), Ordering(Lower('period'), DESC), Concat(Lower('period'), Value('month')), "
        "F('period__key')]"
    )
    unread = Lower("name")
    assert ExpressionFieldRenaming.get_renamed(unread, "month", "period") is unread


@pytest.mark.parametrize("column", ["lower(title)", "a b", '"quoted"'])
def test_physical_index_keys_are_quoted_without_interpreting_their_text(column):
    model = build_model("Book", "book", {"title": CharField(max_length=40, source_field=column)})
    client = FakeClient("sqlite")
    editor = client.dialect.schema_editor_class(client)
    sql = editor.index_statements.get_index_create_sql(model, Index(fields=["title"], name="title_index"))
    database = sqlite3.connect(":memory:")
    try:
        declaration = editor.table_creation.get_definition(model)
        database.executescript(declaration.get_create_sql(editor.TABLE_CREATE_TEMPLATE))
        database.executescript(sql)
        assert database.execute('PRAGMA index_info("title_index")').fetchone()[2] == column
    finally:
        database.close()


def test_index_signatures_compare_declarations_without_python_rendering(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("signature rendered Python source")

    monkeypatch.setattr(DeclarationValueSerializer, "render_value", forbidden)
    first = PartialIndex(Lower("name").desc(), condition=Q(score__gt=F("threshold") + 1))
    same = PartialIndex(Lower("name").desc(), condition=Q(score__gt=F("threshold") + 1))
    reordered = PartialIndex(Lower("name").asc(), condition=Q(score__gt=F("threshold") + 1))
    changed_filter = PartialIndex(Lower("name").desc(), condition=Q(score__gt=F("threshold") + 2))
    signature = StateSignatures.get_index_signature(first)
    assert signature == StateSignatures.get_index_signature(same)
    assert signature != StateSignatures.get_index_signature(reordered)
    assert signature != StateSignatures.get_index_signature(changed_filter)
