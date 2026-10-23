"""WITHOUT OVERLAPS (PostgreSQL 18): a unique constraint or a composite primary key whose last field is a
range two rows with the other fields equal may not overlap in - enforced by the database, refused before
any DDL by an older server and by SQLite, carried through migrations."""

from __future__ import annotations

import datetime
import os
from contextlib import AbstractAsyncContextManager

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.ranges import Range
from hare.dialects.postgresql.server_versions import POSTGRESQL_WITHOUT_OVERLAPS_SERVER_VERSION
from hare.exceptions import ConfigurationError, IntegrityError, QueryError, UnSupportedError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.autodetection.diffs.state_model_diff import StateModelDiff
from hare.migrations.state.model_state import ModelState
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.schema.models_without_overlaps import OverlapPricePeriod, OverlapRoomBooking
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.model_source_generator import ModelSourceGenerator

MODULE = "tests.dialects.postgresql.schema.models_without_overlaps"


def day(number: int) -> datetime.date:
    return datetime.date(2024, 1, number)


def open_context() -> AbstractAsyncContextManager[HareContext]:
    return hare_test_context(
        modules=[MODULE],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    )


async def server_supports_without_overlaps() -> bool:
    """Whether the server under test has WITHOUT OVERLAPS - asked before a context of the module's
    models opens: an older server whose features are known already refuses the models as it opens."""
    return await DatabaseUnderTest.get_postgresql_server_version() >= POSTGRESQL_WITHOUT_OVERLAPS_SERVER_VERSION


async def assert_the_models_are_refused() -> None:
    """An older server refuses the models before any DDL - as the context opens when the server's
    features are known already, else as the schema is created."""
    with pytest.raises((ConfigurationError, UnSupportedError), match="without_overlaps"):
        async with open_context() as ctx:
            async with ctx.get_connection().acquire_connection():
                pass
            await ctx.generate_schemas(safe=False)


@pytest.mark.asyncio
async def test_overlapping_ranges_are_refused():
    skip_if_not_postgres()
    if not await server_supports_without_overlaps():
        await assert_the_models_are_refused()
        return
    async with open_context() as ctx:
        await ctx.generate_schemas(safe=False)
        await OverlapRoomBooking.objects.create(id=1, room=1, during=(day(1), day(5)))
        await OverlapRoomBooking.objects.create(id=2, room=1, during=(day(5), day(8)))
        await OverlapRoomBooking.objects.create(id=3, room=2, during=(day(1), day(5)))
        with pytest.raises(IntegrityError):
            async with Transactions.atomic("models"):
                await OverlapRoomBooking.objects.create(id=4, room=1, during=(day(4), day(6)))
        await OverlapPricePeriod.objects.create(product="tea", valid=(day(1), day(10)), price=5)
        await OverlapPricePeriod.objects.create(product="tea", valid=(day(10), day(20)), price=6)
        with pytest.raises(IntegrityError):
            async with Transactions.atomic("models"):
                await OverlapPricePeriod.objects.create(product="tea", valid=(day(15), day(25)), price=7)
        row = await OverlapPricePeriod.objects.get(product="tea", valid=Range(day(10), day(20)))
        assert row.price == 6


@pytest.mark.asyncio
async def test_an_older_server_refuses_both_keys(monkeypatch):
    skip_if_not_postgres()
    if not await server_supports_without_overlaps():
        await assert_the_models_are_refused()
        return
    async with open_context() as ctx:
        client = Connections.get("models")
        # Connected first: connecting sets the features of the server's version.
        async with client.acquire_connection():
            pass
        monkeypatch.setattr(client, "features", client.features.replace(supports_without_overlaps=False))
        with pytest.raises(UnSupportedError, match="without_overlaps"):
            await ctx.generate_schemas(safe=False)


@pytest.mark.database_independent
def test_the_keys_need_two_fields_and_no_condition():
    from hare.query.expressions import Q

    with pytest.raises(ConfigurationError, match="two fields or more"):
        UniqueConstraint(fields=("during",), without_overlaps=True)
    with pytest.raises(ConfigurationError, match="together with condition"):
        UniqueConstraint(fields=("room", "during"), without_overlaps=True, condition=Q(room__gt=1))


@pytest.mark.database_independent
def test_migrations_carry_the_keys_and_need_btree_gist():
    constraint = OverlapRoomBooking._meta.constraints[0]
    assert constraint.deconstruct()[2]["without_overlaps"] is True
    assert OverlapPricePeriod._meta.pk_without_overlaps is True
    model_state = ModelState.make_from_model("models", OverlapPricePeriod)
    assert model_state.options["pk_without_overlaps"] is True
    editor_class = DialectRegistry.get_dialect("postgresql").schema_editor_class
    assert editor_class.constraint_statements_class.get_without_overlaps_extension(
        ("room", "during"), OverlapRoomBooking._meta.fields_map
    ) == ("btree_gist")
    assert (
        editor_class.constraint_statements_class.get_without_overlaps_extension(
            ("during",), OverlapRoomBooking._meta.fields_map
        )
        is None
    )


@pytest.mark.database_independent
def test_changing_the_primary_key_flag_is_refused():
    old_state = ModelState.make_from_model("models", OverlapPricePeriod)
    new_state = ModelState.make_from_model("models", OverlapPricePeriod)
    new_state.options.pop("pk_without_overlaps")
    with pytest.raises(ConfigurationError, match="without_overlaps"):
        StateModelDiff(old_state, new_state).generate_operations()


@pytest.mark.asyncio
async def test_bulk_create_conflict_targets_of_a_key_without_overlaps():
    """PostgreSQL infers no conflict target from a key WITHOUT OVERLAPS and updates no row conflicting
    with one - both raised the server's raw error; the primary key's constraint name, a hash, couldn't
    be named."""
    skip_if_not_postgres()
    if not await server_supports_without_overlaps():
        pytest.skip("WITHOUT OVERLAPS needs PostgreSQL 18")
    async with open_context() as ctx:
        await ctx.generate_schemas(safe=False)
        await OverlapPricePeriod.objects.create(product="tea", valid=(day(1), day(10)), price=5)
        with pytest.raises(QueryError, match="infers no conflict target"):
            await OverlapPricePeriod.objects.bulk_create(
                [OverlapPricePeriod(product="tea", valid=(day(1), day(10)), price=6)],
                on_conflict=["product", "valid"],
                update_fields=["price"],
            )
        with pytest.raises(QueryError, match="updates none conflicting with an exclusion constraint"):
            await OverlapPricePeriod.objects.bulk_create(
                [OverlapPricePeriod(product="tea", valid=(day(1), day(10)), price=6)],
                on_conflict_constraint="pk",
                update_fields=["price"],
            )
        with pytest.raises(QueryError, match="infers no conflict target"):
            await OverlapRoomBooking.objects.bulk_create(
                [OverlapRoomBooking(id=9, room=1, during=(day(1), day(5)))],
                on_conflict=["room", "during"],
                ignore_conflicts=True,
            )
        await OverlapPricePeriod.objects.bulk_create(
            [OverlapPricePeriod(product="tea", valid=(day(2), day(4)), price=7)],
            on_conflict_constraint="pk",
            ignore_conflicts=True,
        )
        assert await OverlapPricePeriod.objects.values_list("price", flat=True) == [5]


@pytest.mark.asyncio
async def test_inspectdb_writes_the_keys_without_overlaps():
    skip_if_not_postgres()
    if not await server_supports_without_overlaps():
        pytest.skip("WITHOUT OVERLAPS needs PostgreSQL 18")
    async with open_context() as ctx:
        await ctx.generate_schemas(safe=False)
        connection = ctx.get_connection()
        booking_info, price_info = await DatabaseCatalog.inspect_tables(
            connection, ["overlap_room_booking", "overlap_price_period"]
        )

    # The constraints' GiST indexes are theirs - no GistIndex is written for them.
    assert booking_info.primary_key_without_overlaps is False
    assert price_info.primary_key_without_overlaps is True
    booking_source = ModelSourceGenerator.generate_model_source(booking_info, connection.dialect.name)
    assert "without_overlaps=True" in booking_source
    assert "UniqueConstraint(" in booking_source
    assert "GistIndex" not in booking_source
    price_source = ModelSourceGenerator.generate_model_source(price_info, connection.dialect.name)
    assert "CompositePrimaryKey('product', 'valid', without_overlaps=True)" in price_source
    assert "GistIndex" not in price_source
