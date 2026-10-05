"""``pk__in`` over a composite primary key builds its query without a node or a bound-value wrapper per
key - the key values are checked and converted column by column and bound as one parameter per column
(PostgreSQL) or one JSON parameter (SQLite) - while its SQL, its parameters and every special case stay
as they were: a None in a key, a query in place of the list, an expression as a key component, a value
the field refuses. ``bulk_create()`` in a transaction registers what a rollback puts back on all its
objects at once, each object still restored."""

from __future__ import annotations

import os
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import QueryError, ValidationError
from hare.query.expressions import F
from hare.sql.terms.array import Array
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.transactions import Transactions
from tests.composite_key_list_models import CompositeIntKeyEntry, CompositeTextKeyEntry

MODULES = ["tests.composite_key_list_models"]


@pytest_asyncio.fixture
async def composite_db():
    async with hare_test_context(
        MODULES,
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


def get_keys(key_count: int) -> list[tuple[str, str, str]]:
    return [(f"r{index}", f"a{index}", f"e{index}") for index in range(key_count)]


def get_statement(query: Any) -> tuple[str, list[Any]]:
    """The SQL and the bound values of a query, as running it builds them."""
    query._apply_connection(Connections.get("models"))
    return query._get_statements(False)[0]


def get_select_statement(keys: list[Any]) -> tuple[str, list[Any]]:
    return get_statement(CompositeTextKeyEntry.objects.filter(pk__in=keys)._get_compiler()._get_execution_query())


def count_built_terms(build: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """How many SQL terms and how many bound-value wrappers ``build()`` creates."""
    counts = {"terms": 0, "value_wrappers": 0}
    term_init = Term.__init__
    value_wrapper_init = ValueWrapper.__init__

    def counting_term_init(self: Term, *args: Any, **kwargs: Any) -> None:
        counts["terms"] += 1
        term_init(self, *args, **kwargs)

    def counting_value_wrapper_init(self: ValueWrapper, *args: Any, **kwargs: Any) -> None:
        counts["value_wrappers"] += 1
        value_wrapper_init(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Term, "__init__", counting_term_init)
        patch.setattr(ValueWrapper, "__init__", counting_value_wrapper_init)
        build()
    return counts["terms"], counts["value_wrappers"]


@pytest.mark.asyncio
async def test_a_long_key_list_builds_no_term_per_key(composite_db, monkeypatch):
    built_terms = {
        key_count: count_built_terms(
            lambda key_count=key_count: get_select_statement(get_keys(key_count)), monkeypatch
        )
        for key_count in (500, 5000)
    }
    assert built_terms[500] == built_terms[5000]
    delete_terms = {
        key_count: count_built_terms(
            lambda key_count=key_count: get_statement(
                CompositeTextKeyEntry.objects.filter(pk__in=get_keys(key_count)).delete()
            ),
            monkeypatch,
        )
        for key_count in (500, 5000)
    }
    assert delete_terms[500] == delete_terms[5000]


@pytest.mark.asyncio
async def test_the_sql_and_the_parameters_are_unchanged(composite_db):
    dialect_name = composite_db.get_connection().dialect.name
    if dialect_name not in ("postgresql", "sqlite"):
        pytest.skip("the SQL of the built-in dialects")
    keys = get_keys(25)
    sql, values = get_select_statement(keys)
    if dialect_name == "postgresql":
        assert sql == (
            'SELECT "region","account","entry_key","data" FROM "composite_text_key_entry" '
            'WHERE ("region","account","entry_key") IN (SELECT * FROM unnest(CAST($1 AS VARCHAR(64)[]), '
            "CAST($2 AS VARCHAR(64)[]), CAST($3 AS VARCHAR(128)[])))"
        )
        assert values == [[key[index] for key in keys] for index in range(3)]
    else:
        assert sql == (
            'SELECT "region","account","entry_key","data" FROM "composite_text_key_entry" '
            'WHERE ("region","account","entry_key") IN (SELECT json_extract(value, \'$[0]\'), '
            "json_extract(value, '$[1]'), json_extract(value, '$[2]') FROM json_each(?))"
        )
        assert values == [
            "[" + ",".join(f'["{region}","{account}","{entry}"]' for region, account, entry in keys) + "]"
        ]
    short_sql, short_values = get_select_statement(keys[:2])
    placeholders = "($1,$2,$3),($4,$5,$6)" if dialect_name == "postgresql" else "VALUES (?,?,?),(?,?,?)"
    assert short_sql.endswith(f'WHERE ("region","account","entry_key") IN ({placeholders})')
    assert short_values == ["r0", "a0", "e0", "r1", "a1", "e1"]


@pytest.mark.asyncio
async def test_rows_are_found_for_text_and_int_keys(composite_db):
    keys = get_keys(30)
    await CompositeTextKeyEntry.objects.bulk_create(
        [CompositeTextKeyEntry(region=region, account=account, entry_key=entry) for region, account, entry in keys]
    )
    await CompositeIntKeyEntry.objects.bulk_create(
        [CompositeIntKeyEntry(first=index, second=index * 2) for index in range(30)]
    )
    assert await CompositeTextKeyEntry.objects.filter(pk__in=keys[5:28]).count() == 23
    assert await CompositeIntKeyEntry.objects.filter(pk__in=[(index, index * 2) for index in range(25)]).count() == 25
    # A None never matches - the other rows still do.
    with_none = [*keys[:22], (None, "a0", "e0"), ("r1", None, "e1")]
    assert await CompositeTextKeyEntry.objects.filter(pk__in=with_none).count() == 22
    # A query in place of the list.
    subquery = CompositeTextKeyEntry.objects.filter(region__in=["r1", "r2"])
    assert await CompositeTextKeyEntry.objects.filter(pk__in=subquery).count() == 2
    # An expression as a key component - one equality group per row.
    expression_keys = [(F("region"), "a3", "e3"), *keys[10:12]]
    assert await CompositeTextKeyEntry.objects.filter(pk__in=expression_keys).count() == 3
    assert await CompositeTextKeyEntry.objects.filter(pk__in=keys[:22]).delete() == 22
    assert await CompositeTextKeyEntry.objects.count() == 8


@pytest.mark.asyncio
async def test_values_the_field_refuses_raise_the_first_error(composite_db):
    keys = get_keys(30)
    with pytest.raises(ValidationError, match="region") as too_long:
        await CompositeTextKeyEntry.objects.filter(pk__in=[*keys[:3], ("x" * 65, "a", "e"), ("y\x00", "a", "e")])
    assert "64" in str(too_long.value)
    with pytest.raises(ValidationError, match="region"):
        await CompositeTextKeyEntry.objects.filter(pk__in=[*keys[:25], ("y\x00", "a", "e")])
    with pytest.raises(ValidationError, match="first"):
        await CompositeIntKeyEntry.objects.filter(pk__in=[(index, index) for index in range(25)] + [(2**40, 1)])
    with pytest.raises(QueryError, match="3-tuple"):
        await CompositeTextKeyEntry.objects.filter(pk__in=[*keys[:25], ("r", "a")])
    with pytest.raises(QueryError, match="3-tuple"):
        await CompositeTextKeyEntry.objects.filter(pk__in=[*keys[:25], ["r", "a", "e"]])


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
async def test_bulk_create_in_a_rolled_back_transaction_restores_every_object(composite_db):
    objects = [CompositeIntKeyEntry(first=index, second=index) for index in range(40)]
    with pytest.raises(RuntimeError):
        async with Transactions.atomic("models"):
            await CompositeIntKeyEntry.objects.bulk_create(objects)
            assert all(obj._saved_in_db for obj in objects)
            raise RuntimeError("rolled back")
    assert not any(obj._saved_in_db for obj in objects)
    async with Transactions.atomic("models"):
        await CompositeIntKeyEntry.objects.bulk_create(objects[:20])
        with pytest.raises(RuntimeError):
            async with Transactions.atomic("models"):
                await CompositeIntKeyEntry.objects.bulk_create(objects[20:])
                raise RuntimeError("savepoint rolled back")
        assert all(obj._saved_in_db for obj in objects[:20])
        assert not any(obj._saved_in_db for obj in objects[20:])
    assert all(obj._saved_in_db for obj in objects[:20])
    assert await CompositeIntKeyEntry.objects.count() == 20


def test_an_array_walks_only_its_term_elements():
    plain = Array(*range(1000))
    assert list(plain.nodes_()) == [plain]
    assert plain.is_aggregate is None
    term_element = ValueWrapper(5)
    mixed = Array(1, term_element, "two")
    assert list(mixed.nodes_()) == [mixed, term_element]
    # A list element is an array of its own - with no node per value either.
    nested = Array([1, 2], 3)
    nested_nodes = list(nested.nodes_())
    assert nested_nodes[0] is nested
    assert [type(node) for node in nested_nodes[1:]] == [Array]
    # Elements read through ``values`` are walked as read.
    assert len(list(mixed.values)) == 3
    assert len(list(mixed.nodes_())) == 4
