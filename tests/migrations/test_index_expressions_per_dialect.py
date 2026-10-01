"""An index's key expressions are kept as declared and rendered by the database the DDL runs on.

Migration files used to keep an expression index's keys as SQL text rendered for SQLite, applied
as-is to any database."""

from hare.ddl.indexes import Index
from hare.dialects.base.renderers import TermRenderers
from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.registry import DialectRegistry
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.fields import CharField
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.query.expressions import F
from hare.query.functions import Lower
from hare.sql import functions
from tests.migrations.test_round_trip_real_db import build_model


class LowerCaseNamingDialect(SqlDialect):
    """A dialect of its own calling LOWER by another name."""

    name = "lower_case_naming"

    def build_renderers(self) -> TermRenderers:
        renderers = TermRenderers()
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

    rendered = MigrationWriter.render_call(*index.deconstruct(), ImportManager())

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
    index.get_expressions(model)
    name_before = index.get_generated_expression_name(model._meta.db_table)

    index.get_key_sqls(model, LOWER_CASE_NAMING_DIALECT)

    assert index.get_generated_expression_name(model._meta.db_table) == name_before
