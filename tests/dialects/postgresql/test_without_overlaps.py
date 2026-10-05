"""WITHOUT OVERLAPS (PostgreSQL 18): a unique constraint or a composite primary key whose last field is a
range two rows with the other fields equal may not overlap in - enforced by the database, refused before
any DDL by an older server and by SQLite, carried through migrations."""

from __future__ import annotations

import datetime
import os

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.ranges import Range
from hare.exceptions import ConfigurationError, IntegrityError, UnSupportedError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.migrations.autodetection.diffs.state_model_diff import StateModelDiff
from hare.migrations.state.model_state import ModelState
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_without_overlaps import OverlapPricePeriod, OverlapRoomBooking

MODULE = "tests.dialects.postgresql.models_without_overlaps"


def day(number: int) -> datetime.date:
    return datetime.date(2024, 1, number)


@pytest.mark.asyncio
async def test_overlapping_ranges_are_refused():
    skip_if_not_postgres()
    async with hare_test_context(
        modules=[MODULE],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        # Connected first, so the features follow the server's own version.
        async with ctx.get_connection().acquire_connection():
            pass
        if not ctx.get_connection().features.supports_without_overlaps:
            with pytest.raises(UnSupportedError, match="without_overlaps"):
                await ctx.generate_schemas(safe=False)
            return
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
    async with hare_test_context(
        modules=[MODULE],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        client = Connections.get("models")
        # Connected first: connecting sets the features of the server's version.
        async with client.acquire_connection():
            pass
        monkeypatch.setattr(client, "features", client.features.replace(supports_without_overlaps=False))
        with pytest.raises(UnSupportedError, match="without_overlaps"):
            await ctx.generate_schemas(safe=False)


def test_the_keys_need_two_fields_and_no_condition():
    from hare.query.expressions import Q

    with pytest.raises(ConfigurationError, match="two fields or more"):
        UniqueConstraint(fields=("during",), without_overlaps=True)
    with pytest.raises(ConfigurationError, match="together with condition"):
        UniqueConstraint(fields=("room", "during"), without_overlaps=True, condition=Q(room__gt=1))
    with pytest.raises(ConfigurationError, match="must be a bool"):
        CompositePrimaryKey("product", "valid", without_overlaps="yes")


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


def test_changing_the_primary_key_flag_is_refused():
    old_state = ModelState.make_from_model("models", OverlapPricePeriod)
    new_state = ModelState.make_from_model("models", OverlapPricePeriod)
    new_state.options.pop("pk_without_overlaps")
    with pytest.raises(ConfigurationError, match="without_overlaps"):
        StateModelDiff(old_state, new_state).generate_operations()
