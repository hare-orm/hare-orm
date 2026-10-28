"""An extension registered on the "clickhouse" dialect reaches the dialect of the JSON type too: a
clickhouse-connect connection renders in that one."""

from __future__ import annotations

import pytest

from hare import fields
from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT, CLICKHOUSE_NATIVE_JSON_DIALECT
from hare.dialects.dialect_registry import DialectRegistry

pytestmark = pytest.mark.database_independent


class MoneyField(fields.IntField):
    pass


def render_twice(function, sql_context):
    return "twice"


def within(*args):
    return None


def test_the_registered_dialect_repeats_its_registrations_on_the_json_type_dialect():
    """Registered on the "clickhouse" dialect, a type, a renderer and an operator didn't reach the dialect
    of clickhouse-connect - its column was another, its function unknown, its lookup unsupported."""
    assert DialectRegistry.get_dialect("clickhouse") is CLICKHOUSE_DIALECT
    assert CLICKHOUSE_DIALECT.variants == (CLICKHOUSE_NATIVE_JSON_DIALECT,)
    variant = ClickhouseDialect(stores_json_natively=True)
    dialect = ClickhouseDialect(variants=(variant,))
    mapping = TypeMapping(column_type="Int32")

    dialect.types.register(MoneyField, mapping)
    dialect.renderers.register_function("TWICE_X", render_twice)
    dialect.renderers.register(MoneyField, render_twice)
    dialect.renderers.register_name(MoneyField, render_twice)
    dialect.filter_operators.register_operator(within, within)

    for registered in (dialect, variant):
        assert registered.types.get_mappings(MoneyField)[0] is mapping
        assert registered.renderers.get_function_renderer("TWICE_X") is render_twice
        assert registered.renderers.get(MoneyField) is render_twice
        assert registered.filter_operators.get_overridden_operator(within, None) is within
    assert MoneyField().get_column_type(variant) == "Int32"


def test_a_registration_on_the_json_type_dialect_stays_on_it():
    variant = ClickhouseDialect(stores_json_natively=True)
    dialect = ClickhouseDialect(variants=(variant,))
    variant.renderers.register_function("TWICE_X", render_twice)
    assert dialect.renderers.get_function_renderer("TWICE_X") is None
    # Each keeps its own storage of JSON.
    assert dialect.types.get_mappings(fields.JSONField)[0] is not variant.types.get_mappings(fields.JSONField)[0]
