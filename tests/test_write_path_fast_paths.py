"""The write path's shortcuts give exactly what the general code gives: the INSERT values built by
the compiled per-model serializer, JSON encoded after one native check, asyncpg parameters bound
without adapting each plain value, and the per-model flags save() reads."""

import datetime
import math
import sqlite3
from decimal import Decimal

import pytest

from hare.core.caching.caches import Caches
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.exceptions import ValidationError
from hare.fields.data.json import JsonCodec
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.instance_writer import InstanceWriter
from tests import testmodels


@pytest.mark.asyncio
async def test_insert_values_match_the_field_by_field_conversion(db):
    instance = testmodels.DecimalFields(decimal=Decimal("1.23456"), decimal_nodec=Decimal("7"))
    connection = testmodels.DecimalFields.get_connection()
    executor = InstanceWriter(testmodels.DecimalFields, connection)
    for columns in (executor.regular_columns, executor.regular_columns_all):
        if "id" in columns:
            instance.id = 5
        expected = [
            connection.dialect.types.get_db_value(
                testmodels.DecimalFields._meta.fields_map[name], getattr(instance, name), instance
            )
            for name in columns
        ]
        if Decimal in connection.dialect.types.bound_as_text:
            # The native writer binds the text the sqlite3 adapter would make of a value bound as text.
            expected = [
                sqlite3.adapters.get((type(value), sqlite3.PrepareProtocol), lambda v: v)(value) for value in expected
            ]
        assert executor._get_insert_values(instance, columns) == expected
    created = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=Decimal("7"))
    fetched = await testmodels.DecimalFields.objects.get(pk=created.pk)
    assert fetched.decimal == Decimal("1.2346")
    assert fetched.decimal_nodec == Decimal("7")


def test_json_shortcut_encodes_like_the_full_path():
    field = testmodels.JSONFields._meta.fields_map["data"]
    for value in ({"a": [1, 2.5, "x"], "b": None}, [1, {"k": "v"}], "text", 12):
        assert field.encode_value(value) == JsonCodec.dumps(value)


def test_json_shortcut_leaves_problem_values_to_the_full_path():
    field = testmodels.JSONFields._meta.fields_map["data"]
    long_integer = 2**70
    assert field.encode_value({"big": long_integer}) == JsonCodec.dumps_exact({"big": long_integer})
    with pytest.raises(ValidationError):
        field.encode_value({"text": "a\x00b"})
    with pytest.raises(ValidationError):
        field.encode_value({"number": math.nan})
    with pytest.raises(ValidationError):
        field.encode_value({"object": object()})


def test_asyncpg_binds_a_row_of_plain_values_as_it_is():
    row = [1, "text", 2.5, None, True, b"bytes", Decimal("1.5"), datetime.date(2020, 1, 2)]
    assert AsyncpgClient._asyncpg_bind_values(row) is row
    assert AsyncpgClient._asyncpg_bind_values(tuple(row)) == row


def test_asyncpg_still_adapts_a_row_holding_a_value_that_needs_it():
    naive_time = datetime.time(10, 30)
    bound = AsyncpgClient._asyncpg_bind_values([1, naive_time, "x"])
    assert bound[0] == 1 and bound[2] == "x"
    assert bound[1] == naive_time.replace(tzinfo=datetime.UTC)


@pytest.mark.asyncio
async def test_tenant_scoped_relations_flag_is_kept_per_model_and_forgotten(db):
    cache = Tenancy.has_tenant_scoped_relations.cache
    assert Tenancy.has_tenant_scoped_relations(testmodels.Event) is False
    assert testmodels.Event in cache
    Caches.forget_model_caches([testmodels.Event])
    assert testmodels.Event not in cache


@pytest.mark.asyncio
async def test_create_and_save_still_mark_and_persist_the_instance(db):
    tournament = await testmodels.Tournament.objects.create(name="created")
    assert tournament._saved_in_db
    assert tournament.pk is not None
    assert not tournament._custom_generated_pk
    tournament.name = "renamed"
    await tournament.save(update_fields=["name"])
    assert (await testmodels.Tournament.objects.get(pk=tournament.pk)).name == "renamed"
