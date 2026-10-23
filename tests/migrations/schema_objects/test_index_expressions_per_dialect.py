"""An index's key expressions are kept as declared and rendered by the database the DDL runs on.

Migration files used to keep an expression index's keys as SQL text rendered for SQLite, applied
as-is to any database."""

import pytest

from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.ddl.indexes import Index
from hare.dialects.base.renderers import TermRenderers
from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import UnSupportedError
from hare.fields import CharField, DateField, FloatField, IntField, JSONField
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.query.expressions import F, Value
from hare.query.functions import Cast, Concat, Lower
from hare.query.functions.datetime.extract import Extract
from hare.query.functions.datetime.trunc import Trunc
from hare.sql import functions
from tests.migrations.round_trips.test_round_trip_real_db import build_model

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
        index.get_expressions(model)
        names.append(index.get_generated_expression_name(model._meta.db_table))

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
