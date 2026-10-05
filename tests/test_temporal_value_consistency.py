"""Date/time values compared, written and read the same way on SQLite, asyncpg and rust_pg: the
Postgres session zone, annotation filters, date/time literals, TimeField offsets, Now() defaults
of date/time columns, DST midnights, `timestamp` columns, migration rendering and JSON encoding."""

from __future__ import annotations

import datetime
import os
import warnings
from zoneinfo import ZoneInfo

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import ConfigurationError
from hare.fields.data.json import JsonCodec
from hare.fields.db_defaults import Now
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.models import Model
from hare.query.expressions import Case, F, Q, Value, When
from hare.query.functions import Coalesce, Max, Min
from hare.time import Timezone

APP_LABEL = "temporal_values"
TEST_DB_URL = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
UTC = datetime.UTC


class TemporalEvent(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, null=True)
    moment = fields.DatetimeField(null=True)
    day = fields.DateField(null=True)
    clock = fields.TimeField(null=True)
    duration = fields.TimeDeltaField(null=True)
    payload = fields.JSONField(null=True)

    class Meta:
        app = APP_LABEL
        table = "temporal_event"


class TemporalDefaults(Model):
    id = fields.IntField(primary_key=True)
    created_at = fields.DatetimeField(db_default=Now())
    created_on = fields.DateField(db_default=Now())
    created_at_time = fields.TimeField(db_default=Now())

    class Meta:
        app = APP_LABEL
        table = "temporal_defaults"


def temporal_context(use_timezone: bool = True, timezone: str = "UTC"):
    return hare_test_context(
        modules=[__name__],
        db_url=TEST_DB_URL,
        app_label=APP_LABEL,
        connection_label="models",
        use_timezone=use_timezone,
        timezone=timezone,
    )


def is_postgres() -> bool:
    return Connections.get("models").dialect.name == "postgresql"


ZONE_CONFIGS = [(True, "Asia/Kolkata"), (True, "UTC"), (False, "UTC")]


# Postgres session time zone


@pytest.mark.parametrize("configured_zone", ["Pacific/Chatham", "America/New_York"])
def test_server_settings_time_zone_other_than_utc_is_rejected(configured_zone):
    from hare.dialects.postgresql.client import PostgresqlClient

    with pytest.raises(ConfigurationError, match="TimeZone"):
        PostgresqlClient._get_server_settings_with_utc_session({"timezone": configured_zone})


@pytest.mark.parametrize("configured_zone", ["UTC", "Etc/UTC", "utc", "GMT"])
def test_server_settings_utc_time_zone_is_accepted(configured_zone):
    from hare.dialects.postgresql.client import PostgresqlClient

    settings = PostgresqlClient._get_server_settings_with_utc_session(
        {"TimeZone": configured_zone, "application_name": "app"}
    )
    assert settings == {"TimeZone": "UTC", "application_name": "app"}


@pytest.mark.asyncio
async def test_postgres_session_runs_in_utc_whatever_the_database_time_zone():
    async with temporal_context(timezone="Asia/Kolkata") as ctx:
        if not is_postgres():
            pytest.skip("the session time zone is a Postgres setting")
        connection = Connections.get("models")
        database_name = (await connection.execute_dicts("SELECT current_database() AS name"))[0]["name"]
        await connection.execute_script(f"ALTER DATABASE \"{database_name}\" SET TimeZone = 'Pacific/Chatham'")
        try:
            await ctx.connections.close_all(discard=False)
            rows = await connection.execute_dicts("SHOW TimeZone") + await connection.execute_dicts(
                "SELECT '2024-03-10'::date::timestamptz::text AS midnight"
            )
            assert rows[0]["TimeZone"] == "UTC"
            assert rows[1]["midnight"] == "2024-03-10 00:00:00+00"
            await TemporalEvent.objects.create(
                name="a", moment=datetime.datetime(2024, 3, 10, 2, 0, tzinfo=UTC), day=datetime.date(2024, 3, 10)
            )
            assert await TemporalEvent.objects.filter(day__gt=F("moment")).count() == 0
            assert await TemporalEvent.objects.filter(moment__gte=F("day")).count() == 1
        finally:
            await connection.execute_script(f'ALTER DATABASE "{database_name}" RESET TimeZone')


# Annotation filters


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_timezone", "timezone"), ZONE_CONFIGS)
async def test_annotation_filter_encodes_the_value_like_a_field_filter(use_timezone, timezone):
    async with temporal_context(use_timezone, timezone):
        stored_moment = datetime.datetime(2024, 6, 1, 7, 0, tzinfo=UTC)
        await TemporalEvent.objects.create(
            name="a", moment=stored_moment, day=datetime.date(2024, 1, 1), clock=datetime.time(12, 0)
        )
        compared_moment = Timezone.localtime(stored_moment).replace(tzinfo=None) if use_timezone else stored_moment
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            assert await TemporalEvent.objects.filter(moment=compared_moment).count() == 1
            assert await TemporalEvent.objects.annotate(x=F("moment")).filter(x=compared_moment).count() == 1
            assert await TemporalEvent.objects.annotate(x=F("moment")).filter(x__gte=compared_moment).count() == 1
            assert await TemporalEvent.objects.annotate(x=F("moment")).filter(x__in=[compared_moment]).count() == 1
            assert (
                await TemporalEvent.objects.annotate(x=F("moment") + datetime.timedelta(0))
                .filter(x=compared_moment)
                .count()
                == 1
            )
            latest_by_name = TemporalEvent.objects.annotate(x=Max("moment")).group_by("name")
            assert len(await latest_by_name.filter(x=compared_moment).values("name")) == 1
            assert await TemporalEvent.objects.filter(moment__gte=Value(compared_moment)).count() == 1
            assert await TemporalEvent.objects.annotate(x=F("clock")).filter(x=datetime.time(12, 0)).count() == 1
            assert await TemporalEvent.objects.annotate(x=F("day")).filter(x="2024-01-01").count() == 1
            assert (
                await TemporalEvent.objects.annotate(x=Min("day"))
                .group_by("name")
                .filter(x="2024-01-01")
                .values("name")
            ) == [{"name": "a"}]


# Date/time literals


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_timezone", "timezone"), ZONE_CONFIGS)
async def test_value_literal_is_written_like_a_plain_value(use_timezone, timezone):
    async with temporal_context(use_timezone, timezone):
        naive_moment = datetime.datetime(2024, 6, 1, 12, 0)
        aware_moment = datetime.datetime(2024, 6, 1, 7, 0, tzinfo=UTC)
        written_moment = naive_moment if use_timezone else aware_moment
        first = await TemporalEvent.objects.create(name="a", moment=aware_moment)
        second = await TemporalEvent.objects.create(name="b", moment=aware_moment)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            await TemporalEvent.objects.filter(id=first.id).update(moment=Value(written_moment))
            await TemporalEvent.objects.filter(id=second.id).update(moment=written_moment)
            await TemporalEvent.objects.filter(id=first.id).update(clock=Value(datetime.time(1, 2, 3)))
            await TemporalEvent.objects.filter(id=second.id).update(clock=datetime.time(1, 2, 3))
        first_read = await TemporalEvent.objects.get(id=first.id)
        second_read = await TemporalEvent.objects.get(id=second.id)
        assert first_read.moment == second_read.moment
        assert first_read.clock == second_read.clock
        assert await TemporalEvent.objects.filter(moment=second_read.moment).count() == 2
        assert await TemporalEvent.objects.filter(clock=second_read.clock).count() == 2
        assert await TemporalEvent.objects.filter(moment__hour=second_read.moment.hour).count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_timezone", "timezone"), ZONE_CONFIGS)
async def test_value_literal_in_coalesce_and_case_follows_the_configured_zone(use_timezone, timezone):
    async with temporal_context(use_timezone, timezone):
        await TemporalEvent.objects.create(name="empty")
        naive_moment = datetime.datetime(2030, 1, 1, 12, 0)
        expected_moment = Timezone.make_aware(naive_moment, Timezone.default()) if use_timezone else naive_moment
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            coalesced = await TemporalEvent.objects.annotate(x=Coalesce("moment", Value(naive_moment))).values_list(
                "x", flat=True
            )
            cased = await TemporalEvent.objects.annotate(
                x=Case(When(Q(name="empty"), then=Value(naive_moment)), default=F("moment"))
            ).values_list("x", flat=True)
            assert (
                await TemporalEvent.objects.annotate(x=Coalesce("moment", Value(naive_moment)))
                .filter(x=expected_moment)
                .count()
                == 1
            )
        assert coalesced == [expected_moment]
        assert cased == [expected_moment]


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_timezone", "timezone"), ZONE_CONFIGS)
async def test_bare_value_annotation_selects_and_filters_its_own_type(use_timezone, timezone):
    async with temporal_context(use_timezone, timezone):
        await TemporalEvent.objects.create(name="a")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            selected_clock = await TemporalEvent.objects.annotate(x=Value(datetime.time(1, 2, 3))).values_list(
                "x", flat=True
            )
            assert isinstance(selected_clock[0], datetime.time)
            assert selected_clock[0].replace(tzinfo=None) == datetime.time(1, 2, 3)
            for literal in (
                datetime.datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
                datetime.datetime(2024, 6, 1, 12, 0),
                datetime.date(2024, 1, 1),
                datetime.time(1, 2, 3),
                datetime.timedelta(days=1),
                5,
            ):
                assert await TemporalEvent.objects.annotate(x=Value(literal)).filter(x=literal).count() == 1, literal


# TimeField offsets


@pytest.mark.asyncio
async def test_time_field_with_offsets_compares_by_utc_time_on_every_backend():
    async with temporal_context():
        plus_three = datetime.timezone(datetime.timedelta(hours=3))
        minus_five = datetime.timezone(-datetime.timedelta(hours=5))
        await TemporalEvent.objects.create(name="a", clock=datetime.time(10, 0, tzinfo=plus_three))
        await TemporalEvent.objects.create(name="b", clock=datetime.time(8, 0, tzinfo=UTC))
        await TemporalEvent.objects.create(name="c", clock=datetime.time(7, 0, tzinfo=UTC))
        await TemporalEvent.objects.create(name="d", clock=datetime.time(1, 0, tzinfo=minus_five))

        assert await TemporalEvent.objects.all().order_by("clock").values_list("name", flat=True) == [
            "d",
            "a",
            "c",
            "b",
        ]
        assert sorted(
            await TemporalEvent.objects.filter(clock__lt=datetime.time(7, 30, tzinfo=UTC)).values_list(
                "name", flat=True
            )
        ) == ["a", "c", "d"]
        assert await TemporalEvent.objects.filter(clock=datetime.time(7, 0, tzinfo=UTC)).values_list(
            "name", flat=True
        ) == ["c"]
        extremes = await TemporalEvent.objects.all().aggregate(earliest=Min("clock"), latest=Max("clock"))
        assert extremes == {
            "earliest": datetime.time(1, 0, tzinfo=minus_five),
            "latest": datetime.time(8, 0, tzinfo=UTC),
        }
        assert [
            str(clock) for clock in await TemporalEvent.objects.all().order_by("id").values_list("clock", flat=True)
        ] == [
            "10:00:00+03:00",
            "08:00:00+00:00",
            "07:00:00+00:00",
            "01:00:00-05:00",
        ]


# Now() defaults of date/time columns


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_timezone", "timezone"), [(True, "Pacific/Chatham"), (True, "Asia/Kolkata"), (False, "UTC")]
)
async def test_now_default_of_date_and_time_columns_is_the_current_local_date_and_time(use_timezone, timezone):
    async with temporal_context(use_timezone, timezone):
        created = await TemporalDefaults.objects.create()
        await created.refresh_from_db()
        local_now = Timezone.localtime() if use_timezone else datetime.datetime.now()
        assert created.created_on == local_now.date()
        assert await TemporalDefaults.objects.filter(created_on=local_now.date()).count() == 1
        assert isinstance(created.created_at_time, datetime.time)
        if use_timezone:
            assert created.created_at_time.utcoffset() == Timezone.get_fixed_offset().utcoffset(None)
        else:
            assert created.created_at_time.tzinfo is None
        clock_seconds = (
            created.created_at_time.hour * 3600 + created.created_at_time.minute * 60 + created.created_at_time.second
        )
        expected_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
        assert min(abs(clock_seconds - expected_seconds), 86400 - abs(clock_seconds - expected_seconds)) < 60
        assert await TemporalDefaults.objects.filter(created_at_time=created.created_at_time).count() == 1


def test_now_default_follows_the_field_it_belongs_to():
    assert fields.DateField(db_default=Now()).db_default.value_type == "date"
    assert fields.TimeField(db_default=Now()).db_default.value_type == "time"
    assert fields.DatetimeField(db_default=Now()).db_default.value_type == "datetime"


# A day starting inside a DST gap


@pytest.mark.asyncio
async def test_date_compared_with_datetime_starts_at_the_first_moment_of_the_day():
    """America/Santiago jumps from 00:00 straight to 01:00 on 2024-09-08."""
    async with temporal_context(timezone="America/Santiago"):
        gap_day = datetime.date(2024, 9, 8)
        await TemporalEvent.objects.create(name="noon", moment=datetime.datetime(2024, 9, 8, 12, tzinfo=UTC))
        assert await TemporalEvent.objects.filter(moment__gte=gap_day).count() == 1
        assert await TemporalEvent.objects.filter(moment__range=(gap_day, datetime.date(2024, 9, 9))).count() == 1
        created = await TemporalEvent.objects.create(name="day", moment=gap_day)
        assert created.moment == datetime.datetime(2024, 9, 8, 1, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert (await TemporalEvent.objects.get(id=created.id)).moment.utcoffset() == datetime.timedelta(hours=-3)


def test_start_of_day_is_midnight_outside_a_gap():
    zone = ZoneInfo("America/Santiago")
    assert Timezone.get_start_of_day(datetime.date(2024, 9, 7), zone) == datetime.datetime(2024, 9, 7, tzinfo=zone)
    assert Timezone.get_start_of_day(datetime.date(2024, 9, 8), zone) == datetime.datetime(2024, 9, 8, 1, tzinfo=zone)


# Naive TIME read on Postgres


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["America/New_York", "UTC"])
async def test_naive_postgres_time_gets_the_fixed_offset_on_every_read_path(timezone):
    async with temporal_context(timezone=timezone):
        if not is_postgres():
            pytest.skip("a naive TIME column is a Postgres schema")
        connection = Connections.get("models")
        await connection.execute_script("ALTER TABLE temporal_event ALTER COLUMN clock TYPE time USING clock::time")
        await connection.execute_script("INSERT INTO temporal_event (id, clock) VALUES (1, '12:30:00')")
        expected_offset = Timezone.get_fixed_offset().utcoffset(None)
        fetched = await TemporalEvent.objects.get(id=1)
        listed = await TemporalEvent.objects.all()
        selected = await TemporalEvent.objects.all().values_list("clock", flat=True)
        for clock in (fetched.clock, listed[0].clock, selected[0]):
            assert clock == datetime.time(12, 30, tzinfo=datetime.timezone(expected_offset))
            assert clock.utcoffset() == expected_offset


# DatetimeField over a Postgres `timestamp` column


@pytest.mark.asyncio
@pytest.mark.parametrize(("use_timezone", "timezone"), [(True, "UTC"), (True, "Europe/Moscow"), (False, "UTC")])
async def test_datetime_field_over_postgres_timestamp_column_round_trips(use_timezone, timezone):
    async with temporal_context(use_timezone, timezone):
        if not is_postgres():
            pytest.skip("a timestamp without time zone column is a Postgres schema")
        connection = Connections.get("models")
        await connection.execute_script(
            "ALTER TABLE temporal_event ALTER COLUMN moment TYPE timestamp USING moment::timestamp"
        )
        aware_moment = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        written_moment = aware_moment if use_timezone else Timezone.get_system_local_naive(aware_moment)
        created = await TemporalEvent.objects.create(moment=written_moment)
        stored_text = (await connection.execute_dicts("SELECT moment::text AS text FROM temporal_event"))[0]
        fetched = await TemporalEvent.objects.get(id=created.id)
        assert stored_text["text"] == "2024-06-01 12:00:00"
        assert fetched.moment == written_moment
        assert (await TemporalEvent.objects.all().values_list("moment", flat=True)) == [written_moment]
        assert await TemporalEvent.objects.filter(moment=written_moment).count() == 1
        assert await TemporalEvent.objects.filter(moment__hour=fetched.moment.hour).count() == 1


# Migration rendering


class FakeDatetime(datetime.datetime):
    """Stands in for freezegun's FakeDatetime."""


@pytest.mark.parametrize(
    "value",
    [
        datetime.datetime(2024, 11, 3, 1, 30, fold=1),
        datetime.datetime(2024, 11, 3, 1, 30, fold=1, tzinfo=ZoneInfo("America/New_York")),
        datetime.time(1, 2, fold=1),
        datetime.time(1, 2, tzinfo=ZoneInfo("Europe/Moscow")),
        FakeDatetime(2024, 1, 1, 12, 30),
        datetime.datetime(1, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))),
        datetime.date.max,
    ],
)
def test_migration_writer_renders_dates_and_times_as_plain_datetime_classes(value):
    imports = ImportManager()
    rendered = MigrationWriter.render_value(value, imports)
    assert "FakeDatetime" not in rendered
    rebuilt = eval(rendered, {"datetime": datetime, "ZoneInfo": ZoneInfo})  # noqa: S307
    assert type(rebuilt) in (datetime.datetime, datetime.date, datetime.time)
    assert rebuilt == value
    assert getattr(rebuilt, "fold", 0) == getattr(value, "fold", 0)
    if isinstance(value, (datetime.datetime, datetime.time)):
        assert rebuilt.tzinfo == value.tzinfo or rebuilt.utcoffset() == value.utcoffset()


# JSON encoding


@pytest.mark.parametrize(
    "value",
    [
        {"at": datetime.datetime(2024, 1, 1, 12, 0, 0, 5)},
        {"at": datetime.datetime(2024, 1, 1, 12, tzinfo=ZoneInfo("America/New_York"))},
        {"on": datetime.date(2024, 1, 1)},
        {"clock": datetime.time(1, 2, 3, 4)},
        {"text": "привет"},
    ],
)
def test_json_standard_library_path_writes_what_orjson_writes(value):
    orjson = pytest.importorskip("orjson")
    assert JsonCodec.dumps_exact(value) == orjson.dumps(value).decode()


def test_json_encoding_without_orjson_serializes_dates(monkeypatch):
    monkeypatch.setattr(JsonCodec, "orjson", None)
    assert JsonCodec.dumps({"on": datetime.date(2024, 1, 1)}) == '{"on":"2024-01-01"}'


@pytest.mark.asyncio
async def test_json_field_with_big_integer_and_datetime_is_written():
    async with temporal_context():
        created = await TemporalEvent.objects.create(
            payload={"big": 2**70, "at": datetime.datetime(2024, 1, 1, tzinfo=UTC)}
        )
        assert (await TemporalEvent.objects.get(id=created.id)).payload == {
            "big": 2**70,
            "at": "2024-01-01T00:00:00+00:00",
        }


@pytest.mark.asyncio
async def test_json_filter_compares_a_date_with_a_stored_date_string():
    async with temporal_context():
        if not is_postgres():
            pytest.skip("JSONField's __filter lookup is Postgres-only")
        await TemporalEvent.objects.create(name="a", payload={"on": "2024-01-31"})
        await TemporalEvent.objects.create(name="b", payload={"on": "2024-11-03"})
        before_february = TemporalEvent.objects.filter(payload__filter={"on__lt": datetime.date(2024, 2, 1)})
        assert await before_february.values_list("name", flat=True) == ["a"]
