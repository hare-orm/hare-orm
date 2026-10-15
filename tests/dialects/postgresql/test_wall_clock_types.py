"""PostgreSQL's types of a wall clock without a time zone, of an interval and of a fixed-width text -
``timestamp``, ``time``, ``interval``, ``character(n)`` - each kept by a field of its own."""

from __future__ import annotations

import datetime
import os

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import ValidationError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_wall_clock_types import WallClockEntry

MODULE = "tests.dialects.postgresql.models_wall_clock_types"


def wall_clock_context():
    skip_if_not_postgres()
    return hare_test_context(
        modules=[MODULE], db_url=os.environ["HARE_TEST_DB"], app_label="models", connection_label="models"
    )


@pytest.mark.asyncio
async def test_a_wall_clock_an_interval_and_a_fixed_width_text_are_kept_as_written():
    """A naive timestamp, a time, an interval and a character(n) column had no field."""
    async with wall_clock_context():
        at = datetime.datetime(2020, 1, 2, 3, 4, 5, 678901)
        await WallClockEntry.objects.create(
            id=1, at=at, opens=datetime.time(9, 30), lasts=datetime.timedelta(days=2, seconds=5), code="ab"
        )
        entry = await WallClockEntry.objects.get(id=1)
        assert (entry.at, entry.opens, entry.lasts, entry.code) == (
            at,
            datetime.time(9, 30),
            datetime.timedelta(days=2, seconds=5),
            "ab  ",
        )
        assert await WallClockEntry.objects.filter(at__year=2020, at__hour=3, opens__hour=9).count() == 1
        assert await WallClockEntry.objects.filter(lasts__gt=datetime.timedelta(days=1), code="ab").count() == 1
        columns = await Connections.get("models").execute_dicts(
            "SELECT column_name, data_type, character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'wallclockentry' AND column_name <> 'id' ORDER BY ordinal_position"
        )
        assert [(column["column_name"], column["data_type"]) for column in columns] == [
            ("at", "timestamp without time zone"),
            ("opens", "time without time zone"),
            ("lasts", "interval"),
            ("code", "character"),
        ]


@pytest.mark.asyncio
async def test_a_value_with_a_time_zone_is_refused():
    async with wall_clock_context():
        with pytest.raises(ValidationError, match="holds a time zone"):
            await WallClockEntry.objects.create(id=1, at=datetime.datetime(2020, 1, 2, tzinfo=datetime.UTC))
        with pytest.raises(ValidationError, match="holds a time zone"):
            await WallClockEntry.objects.create(id=1, opens=datetime.time(9, tzinfo=datetime.UTC))
        with pytest.raises(ValidationError):
            await WallClockEntry.objects.create(id=1, code="abcde")


@pytest.mark.asyncio
async def test_the_columns_map_back_onto_their_fields():
    """inspectdb read these columns as a TextField with a TODO."""
    async with wall_clock_context():
        connection = Connections.get("models")
        table = await DatabaseCatalog.inspect_table(connection, "wallclockentry")
        introspector = DatabaseCatalog.get_introspector_class(connection)
        mapped = {column.name: introspector.map_column_type(column) for column in table.columns if column.name != "id"}
        assert mapped == {
            "at": ("hare.dialects.postgresql.fields.NaiveDatetimeField", {}, False),
            "opens": ("hare.dialects.postgresql.fields.NaiveTimeField", {}, False),
            "lasts": ("hare.dialects.postgresql.fields.IntervalField", {}, False),
            "code": ("hare.dialects.postgresql.fields.FixedCharField", {"max_length": 4}, False),
        }
