from dataclasses import replace
from itertools import product

import pytest

from hare.ddl.constraints import CheckConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector
from hare.fields.enums import OnDelete
from hare.inspectdb.generation.many_to_many_tables import ManyToManyTables
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.table_info import TableInfo
from hare.inspectdb.schema_inspector import SchemaInspector
from hare.query.expressions import Q

pytestmark = pytest.mark.database_independent


@pytest.fixture(params=[None, "main"])
def junction_tables(request):
    parents = [
        TableInfo(name, columns=[ColumnInfo("id", "INTEGER", False, True, False, generates_key=True)])
        for name in ("author", "book")
    ]
    through = TableInfo(
        "author_books",
        columns=[ColumnInfo(name, "INT", False, False, False) for name in ("author_id", "book_id")],
        foreign_keys={
            name: ForeignKeyInfo(name, target, "id", on_delete=OnDelete.RESTRICT)
            for name, target in (("author_id", "author"), ("book_id", "book"))
        },
    )
    tables = [*parents, through]
    for table in tables:
        table.schema = request.param
    return tables


@pytest.mark.parametrize(("unique", "db_index"), list(product((False, True), repeat=2)))
def test_automatic_relation_preserves_delete_action_and_complete_index_set(junction_tables, unique, db_index):
    through = junction_tables[-1]
    keys = ("author_id", "book_id")
    if unique:
        through.indexes.append(
            IndexInfo(
                list(keys), True, GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_INDEX, through.name, keys)
            )
        )
    if db_index:
        for key in keys[1:] if unique else keys:
            through.column_indexes.append(
                IndexInfo([key], False, GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, through.name, [key]))
            )
    through.table_description = "Author links"
    classified = ManyToManyTables(
        junction_tables,
        {table.name: f"models.{table.name.title()}" for table in junction_tables},
        SqliteIntrospector,
    )
    field = classified.fields_by_table["author"][0][1]
    assert field.on_delete is OnDelete.RESTRICT
    assert field.unique is unique
    assert field.index is db_index
    assert field.description == "Author links"
    source = SchemaInspector.render_module(junction_tables, "sqlite")
    assert "class AuthorBooks(" not in source
    assert "ManyToManyField(" in source


@pytest.mark.parametrize(
    "difference",
    [
        "schema",
        "delete_action",
        "update_action",
        "deferral",
        "nullable",
        "type",
        "integer_width",
        "default",
        "check",
        "partial_index",
        "renamed_index",
        "missing_target",
    ],
)
def test_junction_with_unrepresented_properties_remains_an_explicit_model(junction_tables, difference):
    through = junction_tables[-1]
    foreign_key = through.foreign_keys["book_id"]
    if difference == "schema":
        foreign_key.target_schema = "archive"
    elif difference == "delete_action":
        foreign_key.on_delete = OnDelete.CASCADE
    elif difference == "update_action":
        foreign_key.on_update = "CASCADE"
    elif difference == "deferral":
        foreign_key.deferral = "DEFERRABLE INITIALLY DEFERRED"
    elif difference in {"nullable", "type", "integer_width", "default"}:
        changes = {
            "nullable": {"nullable": True},
            "type": {"db_type": "TEXT"},
            "integer_width": {"db_type": "BIGINT"},
            "default": {"db_default": 1},
        }
        through.columns[1] = replace(through.columns[1], **changes[difference])
    elif difference == "check":
        through.check_constraints.append(CheckConstraint(name="positive_book", check=Q(book_id__gt=0)))
    elif difference in {"partial_index", "renamed_index"}:
        through.indexes.append(
            IndexInfo(
                ["author_id", "book_id"],
                True,
                "custom_links",
                condition_sql="book_id > 0" if difference == "partial_index" else None,
            )
        )
    else:
        junction_tables.pop(1)
    source = SchemaInspector.render_module(junction_tables, "sqlite")
    assert "class AuthorBooks(" in source
    assert "ManyToManyField(" not in source
    if difference == "integer_width":
        assert "ForeignKeyField('models.Book'" in source
        assert "the foreign key column has type 'BIGINT'" in source
        assert "book.id supplies 'INT'" in source
        assert "recreating this relation changes its column type" in source
        assert through.columns[1].todo_reason is None
