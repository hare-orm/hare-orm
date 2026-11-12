import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hare import Model, fields
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.migrations.state.state_apps import StateApps
from hare.query.expressions import ExpressionContext
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.lookup_info.bound_lookup_path import BoundLookupPath
from hare.query.lookup_info.bound_ordering import BoundOrdering
from hare.query.plans.statement.parameter_preparation import ParameterPreparation
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.statements.building.keyset_bounds import KeysetBounds
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.sql import Order, Query, Table
from hare.sql.constants import DEFAULT_SQL_CONTEXT
from hare.sql.functions.text.lower import Lower
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer

pytestmark = pytest.mark.database_independent


@pytest.fixture
def ordering_context():
    class Book(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField(null=True)

    apps = StateApps()
    apps.register_model("models", Book)
    apps.init_relations()
    return ExpressionContext(Book, Table("book"), {}, dialect=SqliteDialect(), connection=None)


@pytest.mark.parametrize("ascending", [True, False])
def test_cursor_ties_use_the_same_custom_key_as_ordering(ordering_context, monkeypatch, ascending):
    context = ordering_context
    title_field = context.model._meta.fields_map["title"]
    original = context.dialect.types.get_ordering_key
    monkeypatch.setattr(
        context.dialect.types,
        "get_ordering_key",
        lambda field, term: Lower(term) if field is title_field else original(field, term),
    )
    direction = Order.build(ascending, nulls_first=False)
    orderings = [("title", direction), ("id", Order.ASC)]
    query = SimpleNamespace(
        model=context.model,
        dialect=context.dialect,
        _connection=None,
        _visibility=RowVisibility.DEFAULT,
        _annotations={},
        _select_related_extra_conditions={},
        _effective_basetable=lambda: context.table,
        _apply_default_ordering=lambda items: items,
        query=Query.from_(context.table).select(context.table.id),
    )
    QueryOrdering.get_ordering(query, context.model, context.table, orderings, {})
    rows = [(1, "alpha"), (2, "ALPHA"), (3, "Beta"), (4, None)]
    nonnull = sorted(rows[:-1], key=lambda row: row[1].lower(), reverse=not ascending)
    expected = nonnull + [rows[-1]]
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.execute("CREATE TABLE book (id INTEGER, title TEXT)")
        connection.executemany("INSERT INTO book VALUES (?, ?)", rows)
        assert connection.execute(query.query.get_sql()).fetchall() == [(row[0],) for row in expected]
        for index, row in enumerate(expected):
            criterion = KeysetBounds.get_cursor_bound_criterion(
                query, orderings, (row[1], row[0]), "_cursor_values", None
            )
            statement = query.query.where(criterion)
            assert connection.execute(statement.get_sql()).fetchall() == [(item[0],) for item in expected[index + 1 :]]


def test_repeated_cursor_parameter_is_prepared_as_one_slot(ordering_context):
    context = ordering_context
    path = BoundLookupPath.bind(context, "title")
    ordering = BoundOrdering.bind(context, path.term, path.field, Order.ASC)
    bound_term, source = ordering.get_boundary_term(ordering.encode_value("alpha"))
    criterion = (ordering.term > bound_term) | ((ordering.term == bound_term) & (context.table.id > 1))
    parameterizer = RecordingParameterizer()
    criterion.get_sql(DEFAULT_SQL_CONTEXT.copy(parameterizer=parameterizer))
    prepared = ParameterPreparation(parameterizer, context.dialect.parameters, context.dialect.types).prepare(
        LiteralValueReference(source, context.dialect.types.get_read_column_encoder(path.field, context.model))
    )
    assert isinstance(prepared, LiteralValueReference)
    assert len(prepared.wrapper.positions) == 2
    parameters = list(parameterizer.values)
    assert prepared.bind(parameters, "beta", context.model, context.dialect)
    assert parameters == ["beta", "beta", 1]


def test_null_placement_uses_connection_features(ordering_context):
    context = ordering_context
    path = BoundLookupPath.bind(context, "title")
    features = context.dialect.features.replace(sorts_nulls_first=not context.dialect.features.sorts_nulls_first)
    bound_context = replace(context, connection=SimpleNamespace(features=features))
    ordering = BoundOrdering.bind(bound_context, path.term, path.field, Order.ASC)
    assert ordering.nulls_first == features.sorts_nulls_first
    explicit = BoundOrdering.bind(bound_context, path.term, path.field, Order.build(True, nulls_first=False))
    assert not explicit.nulls_first
