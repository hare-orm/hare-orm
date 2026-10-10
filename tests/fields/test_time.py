import contextlib
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from time import sleep
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import pytest

from hare import fields
from hare.contrib import test
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ConfigurationError, IntegrityError, NonExistentTimeError, ValidationError
from hare.query.expressions import F
from hare.query.filters import FieldLookups
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT
from hare.sql.terms import Field as SqlField
from hare.time import UTC, Timezone, ZoneInfo
from tests import testmodels
from tests.utils.timezone_context import override_timezone

# ============================================================================
# TestEmpty -> test_empty_*
# ============================================================================


@pytest.mark.asyncio
async def test_empty_datetime_fields(db):
    """Test that creating DatetimeFields without required field raises IntegrityError."""
    with pytest.raises(IntegrityError):
        await testmodels.DatetimeFields.objects.create()


# ============================================================================
# TestDatetimeFields -> test_datetime_*
# ============================================================================


def test_datetime_both_auto_bad(db):
    """Test that setting both auto_now and auto_now_add raises ConfigurationError."""
    with pytest.raises(ConfigurationError, match="You can choose only 'auto_now' or 'auto_now_add'"):
        fields.DatetimeField(auto_now=True, auto_now_add=True)


@pytest.mark.asyncio
async def test_datetime_create(db):
    """Test creating datetime fields and auto_now/auto_now_add behavior."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    obj0 = await model.objects.create(datetime=now)
    obj = await model.objects.get(id=obj0.id)
    assert obj.datetime == now
    assert obj.datetime_null is None
    assert obj.datetime_auto - now < timedelta(seconds=1)
    assert obj.datetime_add - now < timedelta(seconds=1)
    datetime_auto = obj.datetime_auto
    sleep(0.012)
    await obj.save()
    obj2 = await model.objects.get(id=obj.id)
    assert obj2.datetime == now
    assert obj2.datetime_null is None
    assert obj2.datetime_auto == obj.datetime_auto
    assert obj2.datetime_auto != datetime_auto
    assert obj2.datetime_auto - now > timedelta(microseconds=10000)
    assert obj2.datetime_auto - now < timedelta(seconds=1)
    assert obj2.datetime_add == obj.datetime_add


@pytest.mark.asyncio
async def test_datetime_auto_now_bumped_by_partial_save(db):
    """auto_now's own docstring promises "Always set to datetime.utcnow() on save" -
    unconditionally, not "only on a full save()". save(update_fields=[...]) that doesn't name the
    auto_now field itself must still bump it, mirroring how optimistic_lock_field is already bumped
    unconditionally on every update() regardless of what's in update_fields."""
    model = testmodels.DatetimeFields
    obj = await model.objects.create(datetime=Timezone.now())
    original_auto = obj.datetime_auto
    sleep(0.012)

    obj.datetime_null = Timezone.now()
    await obj.save(update_fields=["datetime_null"])

    assert obj.datetime_auto != original_auto
    refreshed = await model.objects.get(id=obj.id)
    assert refreshed.datetime_auto == obj.datetime_auto
    assert refreshed.datetime_auto != original_auto


@pytest.mark.asyncio
async def test_datetime_update(db):
    """Test updating datetime fields via filter().update() with use_timezone=True."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True):
        obj0 = await model.objects.create(datetime=datetime(2019, 9, 1, 0, 0, 0, tzinfo=Timezone.default()))
        await model.objects.filter(id=obj0.id).update(
            datetime=datetime(2019, 9, 1, 6, 0, 8, tzinfo=Timezone.default())
        )
        obj = await model.objects.get(id=obj0.id)
        assert obj.datetime == datetime(2019, 9, 1, 6, 0, 8, tzinfo=Timezone.default())
        assert obj.datetime_null is None


@pytest.mark.asyncio
async def test_datetime_filter(db):
    """Test filtering by datetime field."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    obj = await model.objects.create(datetime=now)
    assert await model.objects.filter(datetime=now).first() == obj
    assert await model.objects.annotate(d=F("datetime")).filter(d=now).first() == obj


@pytest.mark.asyncio
async def test_datetime_cast(db):
    """Test datetime field accepts ISO format string."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    obj0 = await model.objects.create(datetime=now.isoformat())
    obj = await model.objects.get(id=obj0.id)
    assert obj.datetime == now


@pytest.mark.asyncio
async def test_datetime_values(db):
    """Test datetime field in values() query."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    obj0 = await model.objects.create(datetime=now)
    values = await model.objects.get(id=obj0.id).values("datetime")
    assert values["datetime"] == now


@pytest.mark.asyncio
async def test_datetime_values_list(db):
    """Test datetime field in values_list() query."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    obj0 = await model.objects.create(datetime=now)
    values = await model.objects.get(id=obj0.id).values_list("datetime", flat=True)
    assert values == now


@pytest.mark.asyncio
async def test_datetime_get_utcnow(db):
    """Test getting datetime using UTC now with use_timezone=True."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True):
        now = datetime.now(dt_timezone.utc).replace(tzinfo=Timezone.default())
        await model.objects.create(datetime=now)
        obj = await model.objects.get(datetime=now)
        assert obj.datetime == now


@pytest.mark.asyncio
async def test_datetime_get_now(db):
    """Test getting datetime using Timezone.now()."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    await model.objects.create(datetime=now)
    obj = await model.objects.get(datetime=now)
    assert obj.datetime == now


@pytest.mark.asyncio
async def test_datetime_count(db):
    """Test count queries with datetime fields."""
    model = testmodels.DatetimeFields
    now = Timezone.now()
    obj = await model.objects.create(datetime=now)
    assert await model.objects.filter(datetime=obj.datetime).count() == 1
    assert await model.objects.filter(datetime_auto=obj.datetime_auto).count() == 1
    assert await model.objects.filter(datetime_add=obj.datetime_add).count() == 1


@pytest.mark.asyncio
async def test_datetime_default_timezone(db):
    """Test default timezone is UTC when use_timezone=True."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True):
        now = Timezone.now()
        obj = await model.objects.create(datetime=now)
        assert obj.datetime.tzinfo.zone == "UTC"

        obj_get = await model.objects.get(pk=obj.pk)
        assert obj_get.datetime.tzinfo.zone == "UTC"
        assert obj_get.datetime == now


@pytest.mark.asyncio
async def test_datetime_set_timezone(db):
    """Test setting a custom timezone with use_timezone=True."""
    model = testmodels.DatetimeFields
    tz = "Asia/Shanghai"
    with override_timezone(use_timezone=True, timezone=tz):
        now = datetime.now(Timezone.parse(tz))
        obj = await model.objects.create(datetime=now)
        assert obj.datetime.tzinfo.zone == tz

        obj_get = await model.objects.get(pk=obj.pk)
        assert obj_get.datetime.tzinfo.zone == tz
        assert obj_get.datetime == now


@pytest.fixture
def force_pure_python_hydration():
    """Forces Model._init_from_db_positional()'s pure-Python path (with its tz_ctx handling) by
    disabling the Rust hydration accelerator - a dev environment with rust/hydrate/ built would
    otherwise take the rust.hydrate accelerator's entirely separate code path
    for every one of these tests, silently never exercising the code being tested here."""
    from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

    with patch.object(HydrateAccelerator, "module", None):
        yield


@pytest.mark.asyncio
async def test_positional_hydration_tz_ctx_aware_value_matches_to_python_value(db, force_pure_python_hydration):
    """Model._init_from_db_positional()'s tz_ctx fast path (DatetimeField.get_python_value_with_timezone,
    called with use_timezone/tz resolved once per query) must read back the exact same value
    DatetimeField.from_db_value() (resolving Timezone itself, per-row) would - both an
    already-aware stored value (astimezone branch) and, separately, USE_TZ=False (naive branch)."""
    model = testmodels.DatetimeFields
    tz = "Asia/Shanghai"
    with override_timezone(use_timezone=True, timezone=tz):
        now = datetime.now(Timezone.parse(tz))
        obj = await model.objects.create(datetime=now)

        fetched = await model.objects.all()
        assert len(fetched) == 1
        assert fetched[0].datetime.tzinfo.zone == tz
        assert fetched[0].datetime == now
        assert Timezone.is_aware(fetched[0].datetime)

        obj_get = await model.objects.get(pk=obj.pk)
        assert obj_get.datetime == now


@pytest.mark.asyncio
async def test_positional_hydration_tz_ctx_naive_roundtrip_with_use_tz_false(db, force_pure_python_hydration):
    """Same tz_ctx fast path, USE_TZ=False - must produce a naive datetime, not silently apply a
    tzinfo the aware branch's shortcut (`tz` resolved once regardless of use_timezone) might otherwise
    leak in."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        naive_dt = datetime(2021, 2, 2, 12, 30, 0)
        await model.objects.create(datetime=naive_dt)

        fetched = await model.objects.all()
        assert len(fetched) == 1
        assert Timezone.is_naive(fetched[0].datetime)
        assert fetched[0].datetime == naive_dt


@pytest.mark.asyncio
async def test_datetime_timezone(db):
    """Test timezone handling with USE_TZ enabled."""
    model = testmodels.DatetimeFields
    tz = "Asia/Shanghai"
    with override_timezone(use_timezone=True, timezone=tz):
        now = datetime.now(Timezone.parse(tz))
        obj = await model.objects.create(datetime=now)
        assert obj.datetime.tzinfo.zone == tz
        obj_get = await model.objects.get(pk=obj.pk)
        assert obj.datetime.tzinfo.zone == tz
        assert obj_get.datetime == now


def test_timezone_now_returns_naive_when_use_tz_false():
    """Test Timezone.now() returns naive datetime when use_timezone=False."""
    with override_timezone(use_timezone=False):
        now = Timezone.now()
        assert Timezone.is_naive(now), f"Expected naive datetime, got {now} with tzinfo={now.tzinfo}"
        assert now.tzinfo is None


def test_timezone_now_returns_aware_when_use_tz_true():
    """Test Timezone.now() returns aware datetime in UTC when use_timezone=True."""
    with override_timezone(use_timezone=True):
        now = Timezone.now()
        assert Timezone.is_aware(now), f"Expected aware datetime, got {now} with tzinfo={now.tzinfo}"
        assert now.tzinfo == UTC


@pytest.mark.asyncio
async def test_datetime_naive_roundtrip_with_use_tz_false(db):
    """Test naive datetime survives round-trip with use_timezone=False."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        naive_dt = datetime(2021, 2, 2, 12, 30, 0)
        assert Timezone.is_naive(naive_dt)

        obj = await model.objects.create(datetime=naive_dt)
        assert Timezone.is_naive(obj.datetime), f"Expected naive after create, got {obj.datetime}"
        assert obj.datetime == naive_dt

        obj_get = await model.objects.get(pk=obj.pk)
        assert Timezone.is_naive(obj_get.datetime), f"Expected naive after retrieve, got {obj_get.datetime}"
        assert obj_get.datetime == naive_dt


@pytest.mark.asyncio
async def test_datetime_field_to_db_value_normalizes_aware_value_when_use_tz_false(db):
    """to_db_value()'s naive/aware handling only covered ONE direction (use_timezone=True + naive
    value -> made aware) - from_db_value (read path) handles both directions symmetrically
    (use_timezone=False -> always ensure naive, converting an aware value via astimezone() first), but
    to_db_value (write path) had no mirror case for use_timezone=False + an aware value: called
    directly, it returned the value completely unconverted, still aware.

    Called directly (not through .create()/round-trip) because .create(datetime=...) doesn't
    reach this gap at all - _set_kwargs() already routes the value through from_db_value
    (which IS symmetric) before to_db_value ever sees it, and a full round-trip through sqlite's
    own serialization masks the gap too (a plain attribute assignment - unlike construction,
    which routes through from_db_value - is the one path that reaches to_db_value with the
    value completely unconverted, but by the time it comes back out through from_db_value on
    read, that method's own symmetric handling normalizes it anyway, so the DB round-trip alone
    can't tell a fixed to_db_value from a broken one)."""
    with override_timezone(use_timezone=False):
        field = testmodels.DatetimeFields._meta.fields_map["datetime"]
        types = testmodels.DatetimeFields._meta.connection.dialect.types
        aware_dt = datetime(2021, 2, 2, 12, 30, 0, tzinfo=dt_timezone.utc)
        result = types.get_db_value(field, aware_dt, testmodels.DatetimeFields)
        if types.get_db_converter(type(field)) is not None:
            # A Postgres TIMESTAMPTZ column is bound the instant itself.
            assert result == aware_dt
        else:
            assert Timezone.is_naive(result), f"Expected naive, got {result} with tzinfo={result.tzinfo}"
            assert result == Timezone.get_system_local_naive(aware_dt)


def test_datetime_field_to_python_value_epoch_int_preserves_the_instant_when_use_tz_true():
    """from_db_value()'s int branch used to interpret the epoch in the SYSTEM's local
    timezone via a naive fromtimestamp(value) call, then just relabel it as tz-aware instead of
    converting - silently shifting the represented instant by the system/configured-timezone
    offset whenever they differ. Reachable via a plain attribute assignment (obj.field = <int>),
    which - unlike construction - doesn't route through from_db_value at write time, so a raw
    epoch int can genuinely land in the column and get read back through this exact path."""
    with override_timezone(use_timezone=True, timezone="America/New_York"):
        field = testmodels.DatetimeFields._meta.fields_map["datetime"]
        epoch = 1700000000
        result = field.from_db_value(epoch)
        assert result is not None
        assert result.timestamp() == epoch


def test_time_field_to_db_value_normalizes_aware_value_when_use_tz_false():
    """Same gap as DatetimeField above, for TimeField.to_db_value."""
    with override_timezone(use_timezone=False):
        field = testmodels.TimeFields._meta.fields_map["time"]
        aware_t = time(12, 30, 0, tzinfo=dt_timezone.utc)
        result = field.to_db_value(aware_t, testmodels.TimeFields)
        assert Timezone.is_naive(result), f"Expected naive, got {result} with tzinfo={result.tzinfo}"


def test_time_field_to_db_value_parses_raw_string():
    """A raw ISO time string (e.g. a plain attribute assignment, which - unlike construction/
    _init_from_db - doesn't route through from_db_value) used to crash to_db_value() with an
    unrelated AttributeError ('str' object has no attribute 'utcoffset', raised deep inside the
    Timezone.is_naive() call) instead of parsing it - DateField/DatetimeField.to_db_value both
    already have an isinstance(value, str) branch for exactly this case, TimeField was the one
    field missing it. from_db_value already parses a raw string via
    datetime.time.fromisoformat() (see its own isinstance(value, str) branch just above this
    field's to_db_value) - to_db_value now mirrors that same conversion.

    use_timezone=False (deterministic, decoupled from the separate aware/naive dispatch logic already
    covered by the sibling test above) - just confirms the string gets parsed instead of
    crashing before any aware/naive branch is even reached."""
    with override_timezone(use_timezone=False):
        field = testmodels.TimeFields._meta.fields_map["time"]
        result = field.to_db_value("12:30:00", testmodels.TimeFields)
        assert result == time(12, 30, 0)


def test_time_field_to_db_value_wraps_malformed_string():
    """The str-parsing branch added just above (datetime.time.fromisoformat) raises a bare
    ValueError for a malformed (but still string-shaped) input, same unwrapped-exception bug
    class as Date/DatetimeField's own parse_datetime() calls and the base Field's
    field_type() coercion - all fixed the same way in this session."""
    with override_timezone(use_timezone=False):
        field = testmodels.TimeFields._meta.fields_map["time"]
        with pytest.raises(ValidationError):
            field.to_db_value("not-a-time-string", testmodels.TimeFields)


def test_time_field_auto_now_stamps_local_wall_clock_not_utc():
    """TimeField(auto_now=True)'s factory used to do `Timezone.now().time()` -
    Timezone.now() returns an AWARE UTC datetime when use_timezone is on, and `.time()` strips
    tzinfo, leaving UTC wall-clock digits with no zone marker. from_db_value's naive-handling
    branch then tagged those UTC digits with the configured LOCAL zone via
    `.replace(tzinfo=...)` instead of converting - off by exactly the UTC offset (verified live:
    3 hours for Europe/Moscow, which has no DST). This only shows up with a non-UTC configured
    timezone.

    Exercises to_db_value directly against a bare stand-in instance (mirroring
    test_time_field_to_db_value_normalizes_aware_value_when_use_tz_false above) rather than a
    full create() round-trip - sqlite has no native support for an aware datetime.time value
    (see the postgres-only capability guards on TestTimeFields above), so a real round-trip of
    this auto_now field isn't possible there.
    """

    def seconds_since_midnight(t: time) -> float:
        return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6

    def circular_diff(a: time, b: time) -> float:
        diff = abs(seconds_since_midnight(a) - seconds_since_midnight(b))
        return min(diff, 86400 - diff)

    class FakeInstance:
        _saved_in_db = False

    field = testmodels.TimeFields._meta.fields_map["time_auto"]
    instance = FakeInstance()
    tz = "Europe/Moscow"
    with override_timezone(use_timezone=True, timezone=tz):
        utc_now = Timezone.now()
        field.to_db_value(None, instance)

        stamped = instance.time_auto
        # A fixed offset carrying the zone's offset, NOT the named zone itself: a bare time has
        # no date, so a named zone can't report an offset for it (utcoffset() -> None), which
        # leaves the value naive as far as Python and both Postgres drivers are concerned - see
        # test_time_field_named_zone_is_stored_as_a_fixed_offset below.
        assert stamped.utcoffset() == timedelta(hours=3)

        expected_local = Timezone.localtime(utc_now, tz).time()
        assert circular_diff(stamped.replace(tzinfo=None), expected_local) < 5, (
            f"expected {stamped} to be within 5s of local time {expected_local}"
        )

        # The pre-fix behavior stamped raw UTC wall-clock digits mislabeled as Europe/Moscow -
        # confirm the fix didn't just move the bug, by checking it's NOT close to bare UTC.
        assert circular_diff(stamped.replace(tzinfo=None), utc_now.timetz().replace(tzinfo=None)) > 60


def test_date_field_to_db_value_wraps_malformed_string():
    """DateField.to_db_value's own parse_datetime() call, same unwrapped-exception bug class."""
    field = testmodels.DateFields._meta.fields_map["date"]
    with pytest.raises(ValidationError):
        field.to_db_value("not-a-date-string-longer-than-4-chars", testmodels.DateFields)


def test_datetime_field_to_db_value_wraps_malformed_string():
    """DatetimeField.to_db_value's own parse_datetime() call, same unwrapped-exception bug class."""
    field = testmodels.DatetimeFields._meta.fields_map["datetime"]
    with pytest.raises(ValidationError):
        field.to_db_value("not-a-datetime-string", testmodels.DatetimeFields)


@pytest.mark.asyncio
async def test_datetime_two_fields_naive_no_comparison_error(db):
    """Test two DatetimeFields with naive datetimes (issue #631 reproduction)."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        dt1 = datetime(2021, 1, 1, 10, 0, 0)
        dt2 = datetime(2021, 2, 2, 14, 0, 0)

        # Create with first datetime
        obj = await model.objects.create(datetime=dt1)
        assert Timezone.is_naive(obj.datetime)

        # Update with second datetime - this should not cause "can't compare aware and naive" error
        obj.datetime = dt2
        await obj.save()

        # Verify both are naive and correct
        obj_get = await model.objects.get(pk=obj.pk)
        assert Timezone.is_naive(obj_get.datetime)
        assert obj_get.datetime == dt2


@pytest.mark.asyncio
async def test_datetime_aware_behavior_unchanged_with_use_tz_true(db):
    """Test aware datetime behavior is unchanged with use_timezone=True."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone="UTC"):
        aware_dt = Timezone.now()
        assert Timezone.is_aware(aware_dt)

        obj = await model.objects.create(datetime=aware_dt)
        assert Timezone.is_aware(obj.datetime)

        obj_get = await model.objects.get(pk=obj.pk)
        assert Timezone.is_aware(obj_get.datetime)
        assert obj_get.datetime == aware_dt


@pytest.mark.asyncio
async def test_datetime_auto_now_add_naive_with_use_tz_false(db):
    """Test auto_now_add produces naive datetime with use_timezone=False."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        # Create object with auto_now_add field
        obj = await model.objects.create(datetime=datetime(2021, 1, 1))

        # Check that auto_now_add field is naive
        assert Timezone.is_naive(obj.datetime_add), f"Expected naive auto_now_add, got {obj.datetime_add}"

        # Retrieve and verify still naive
        obj_get = await model.objects.get(pk=obj.pk)
        assert Timezone.is_naive(obj_get.datetime_add)


@pytest.mark.asyncio
async def test_datetime_auto_now_naive_with_use_tz_false(db):
    """Test auto_now produces naive datetime with use_timezone=False."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        # Create and save object
        obj = await model.objects.create(datetime=datetime(2021, 1, 1))
        original_auto_now = obj.datetime_auto

        # Update object to trigger auto_now
        sleep(0.01)  # Ensure time difference
        obj.datetime = datetime(2021, 2, 2)
        await obj.save()

        # Check that auto_now field is naive
        assert Timezone.is_naive(obj.datetime_auto), f"Expected naive auto_now, got {obj.datetime_auto}"
        assert obj.datetime_auto > original_auto_now

        # Retrieve and verify still naive
        obj_get = await model.objects.get(pk=obj.pk)
        assert Timezone.is_naive(obj_get.datetime_auto)


@pytest.mark.asyncio
async def test_datetime_auto_now_add_matches_db_on_create(db):
    """Test auto_now_add value on instance after create() matches what DB returns.

    Note: MSSQL (DATETIME2) and MySQL (DATETIME) use timezone-naive columns.
    Python drivers strip timezone info before sending, causing UTC wall-clock time
    to be stored and misinterpreted as local time on read with custom timezones.
    """
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone="Asia/Shanghai"):
        obj = await model.objects.create(datetime=datetime(2021, 1, 1, tzinfo=Timezone.default()))
        obj_get = await model.objects.get(pk=obj.pk)

        # Instance from create() should match instance from get() — no refresh needed
        assert obj.datetime_add == obj_get.datetime_add
        assert obj.datetime_add.tzinfo is not None
        assert obj.datetime_add.tzinfo.key == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_datetime_auto_now_matches_db_on_save(db):
    """Test auto_now value on instance after save() matches what DB returns.

    Note: MSSQL (DATETIME2) and MySQL (DATETIME) use timezone-naive columns.
    Python drivers strip timezone info before sending, causing UTC wall-clock time
    to be stored and misinterpreted as local time on read with custom timezones.
    """
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone="Asia/Shanghai"):
        obj = await model.objects.create(datetime=datetime(2021, 1, 1, tzinfo=Timezone.default()))
        sleep(0.01)
        obj.datetime = datetime(2021, 2, 2, tzinfo=Timezone.default())
        await obj.save()
        obj_get = await model.objects.get(pk=obj.pk)

        # Instance from save() should match instance from get() — no refresh needed
        assert obj.datetime_auto == obj_get.datetime_auto
        assert obj.datetime_auto.tzinfo is not None
        assert obj.datetime_auto.tzinfo.key == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_datetime_auto_fields_match_db_with_use_tz_false(db):
    """Test auto_now/auto_now_add on instance match DB when use_timezone=False."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        obj = await model.objects.create(datetime=datetime(2021, 1, 1))
        obj_get = await model.objects.get(pk=obj.pk)

        assert obj.datetime_add == obj_get.datetime_add
        assert Timezone.is_naive(obj.datetime_add)

        sleep(0.01)
        obj.datetime = datetime(2021, 2, 2)
        await obj.save()
        obj_get = await model.objects.get(pk=obj.pk)

        assert obj.datetime_auto == obj_get.datetime_auto
        assert Timezone.is_naive(obj.datetime_auto)


@pytest.mark.asyncio
async def test_datetime_filter_by_year_month_day(db):
    """Test filtering datetime by year, month, and day.

    No longer skipped on sqlite - field__year/__month/__day/etc used to raise
    OperationalError there (SQLite has no EXTRACT()); both dialects are exercised now that
    SQLite has its own hare_extract_date_part() UDF backing the same lookups (see
    hare.sql.functions.Extract / hare.dialects.sqlite.functions.datetime).
    """
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True):
        obj = await model.objects.create(datetime=datetime(2024, 1, 2))
        same_year_objs = await model.objects.filter(datetime__year=2024)
        filtered_obj = await model.objects.filter(datetime__year=2024, datetime__month=1, datetime__day=2).first()
        assert obj == filtered_obj
        assert obj.id in [i.id for i in same_year_objs]


@pytest.mark.asyncio
async def test_datetime_date_part_filter_coerces_numeric_string(db):
    """`datetime__year=`/`__month`/etc used to bind a numeric-string value straight through to
    the driver instead of converting it to `int` first - SQLite's UDF-backed extraction never
    matched a same-looking TEXT value against its own INTEGER result (0 rows), and asyncpg's own
    per-lookup binding disagreed by date part (`__second="7"` raised outright: "'str' object
    cannot be interpreted as an integer") - both now behave exactly like the equivalent int."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True):
        obj = await model.objects.create(datetime=datetime(2024, 1, 2, 3, 4, 7))

        assert await model.objects.filter(id=obj.id, datetime__year="2024").count() == 1
        assert await model.objects.filter(id=obj.id, datetime__month="1").count() == 1
        assert await model.objects.filter(id=obj.id, datetime__day="2").count() == 1
        assert await model.objects.filter(id=obj.id, datetime__hour="3").count() == 1
        assert await model.objects.filter(id=obj.id, datetime__second="7").count() == 1
        assert await model.objects.filter(id=obj.id, datetime__year="2023").count() == 0


@pytest.mark.asyncio
async def test_datetime_date_part_filter_rejects_non_numeric_value(db):
    """A non-scalar value used to reach the driver unvalidated - rust_pg panicked outright
    (`RustPanic: rust future panicked`) instead of raising an ORM-level error before the query
    was ever sent."""
    from hare.exceptions import UnSupportedError

    model = testmodels.DatetimeFields
    obj = await model.objects.create(datetime=datetime(2024, 1, 2, 3, 4, 7))

    with pytest.raises(UnSupportedError):
        await model.objects.filter(id=obj.id, datetime__year=[2024]).count()
    # A short (<=4 char) non-numeric string - long enough strings are instead rejected earlier,
    # with a ValidationError, by DatetimeField.to_db_value()'s own raw-datetime-string parsing
    # (unrelated to this lookup's own int coercion).
    with pytest.raises(UnSupportedError):
        await model.objects.filter(id=obj.id, datetime__year="abcd").count()


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", ["Europe/Berlin", "Asia/Kolkata"])
async def test_datetime_filter_by_year_extracts_in_configured_zone(db, tz):
    """field__year/__month/__day/__hour must extract in the CONFIGURED zone, not whatever zone
    the DB session/driver happens to report the stored TIMESTAMPTZ in (Postgres: UTC by
    default) - a row stored just after local midnight is still "yesterday" in UTC, so a naive
    bare EXTRACT(... FROM col) (no zone conversion) disagreed with the very same instant's own
    .year/.month/.day attributes, and every corresponding filter matched zero rows. Regression
    coverage for a DST-observing zone (Europe/Berlin) and a half-hour-offset one (Asia/Kolkata) -
    a fixed whole-hour zone like the old Europe/Moscow-only coverage can't catch either shape of
    this bug, and the suite otherwise runs entirely in UTC, where a session-zone/configured-zone
    mismatch can never show up at all.
    """
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone=tz):
        # 00:30 local, the day after a UTC-dated instant for every zone ahead of UTC (both
        # Europe/Berlin, +1/+2h, and Asia/Kolkata, +5:30h) - exactly the case a session-zone
        # extraction gets wrong.
        local_dt = datetime(2024, 1, 2, 0, 30, tzinfo=Timezone.default())
        obj = await model.objects.create(datetime=local_dt)

        assert obj.datetime.year == 2024
        assert obj.datetime.month == 1
        assert obj.datetime.day == 2
        assert obj.datetime.hour == 0

        matched = await model.objects.get(
            datetime__year=2024, datetime__month=1, datetime__day=2, datetime__hour=0, id=obj.id
        )
        assert matched == obj

        assert await model.objects.filter(datetime__year=2023, id=obj.id).count() == 0
        assert await model.objects.filter(datetime__day=1, id=obj.id).count() == 0


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_datetime_filter_by_hour_extracts_in_local_zone_with_use_tz_false(db):
    """field__hour/__day/__month/__year must extract in the machine's own local wall-clock zone
    when use_timezone=False, on Postgres specifically - the column is still TIMESTAMPTZ there even
    with use_timezone off, so a bare EXTRACT(... FROM col) (no AT TIME ZONE) instead extracted in
    whatever zone the Postgres SESSION reports (typically UTC), silently disagreeing with the
    naive value's own wall-clock digits whenever the local zone isn't UTC. SQLite stores the
    naive text as-is, so it was never affected.
    """
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        naive_dt = datetime(2024, 3, 10, 23, 30)
        obj = await model.objects.create(datetime=naive_dt)

        assert await model.objects.filter(id=obj.id, datetime__hour=23).count() == 1
        assert await model.objects.filter(id=obj.id, datetime__day=10).count() == 1
        assert await model.objects.filter(id=obj.id, datetime__month=3).count() == 1
        assert await model.objects.filter(id=obj.id, datetime__year=2024).count() == 1
        assert await model.objects.filter(id=obj.id, datetime__hour=20).count() == 0


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_datetime_filter_by_hour_use_tz_false_boundary_values(db):
    """Boundary coverage for the same local-zone extraction: end of year with a nonzero
    microsecond, and the first week-of-year boundary - both must still extract by the local
    wall-clock value, not the Postgres session zone's."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        end_of_year = await model.objects.create(datetime=datetime(2020, 12, 31, 23, 59, 59, 999999))
        assert await model.objects.filter(id=end_of_year.id, datetime__year=2020).count() == 1
        assert await model.objects.filter(id=end_of_year.id, datetime__month=12).count() == 1
        assert await model.objects.filter(id=end_of_year.id, datetime__day=31).count() == 1
        assert await model.objects.filter(id=end_of_year.id, datetime__hour=23).count() == 1
        assert await model.objects.filter(id=end_of_year.id, datetime__microsecond=999999).count() == 1

        first_week = datetime(2021, 1, 3, 0, 0)
        obj = await model.objects.create(datetime=first_week)
        assert await model.objects.filter(id=obj.id, datetime__day=3).count() == 1
        assert await model.objects.filter(id=obj.id, datetime__week=first_week.isocalendar()[1]).count() == 1


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_datetime_filter_by_hour_use_tz_false_across_dst_dates(db):
    """Two dates 6 months apart must each extract their own correct local wall-clock hour - a
    FIXED offset (instead of the local IANA zone) would silently break whichever of the two
    lands on the wrong side of a DST transition from the offset's own capture moment."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        winter = await model.objects.create(datetime=datetime(2024, 1, 15, 5, 0))
        summer = await model.objects.create(datetime=datetime(2024, 7, 15, 5, 0))
        assert await model.objects.filter(id=winter.id, datetime__hour=5).count() == 1
        assert await model.objects.filter(id=summer.id, datetime__hour=5).count() == 1


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_datetime_filter_by_hour_use_tz_true_unaffected_by_missing_tzlocal(db):
    """use_timezone=True never needs the local system zone at all - only use_timezone=False does."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        obj = await model.objects.create(datetime=datetime(2024, 1, 1, 10, 0, tzinfo=Timezone.default()))
        with patch.object(Timezone, "get_tzlocal", staticmethod(lambda: None)):
            assert await model.objects.filter(id=obj.id, datetime__hour=10).count() == 1


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_datetime_filter_by_hour_use_tz_false_raises_clear_error_without_tzlocal(db):
    """Missing the optional `tzlocal` dependency must raise a clear, actionable
    ConfigurationError - only on this specific Postgres + use_timezone=False + extraction-lookup
    combination, not for use_timezone=True or SQLite."""
    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=False):
        obj = await model.objects.create(datetime=datetime(2024, 1, 1, 10, 0))
        with patch.object(Timezone, "get_tzlocal", staticmethod(lambda: None)):
            with pytest.raises(ConfigurationError, match="tzlocal"):
                await model.objects.filter(id=obj.id, datetime__hour=10).count()


@pytest.mark.asyncio
async def test_datetime_ordering_and_comparison_across_dst_fall_back(db):
    """Storing an aware DatetimeField with its own (varying) UTC offset - rather than normalized
    to a fixed one - broke ordering/comparisons on sqlite, which has no TIMESTAMPTZ and instead
    compares the stored ISO text lexicographically: two clock readings inside the repeated
    Europe/Berlin fall-back hour (2024-10-27, 02:00-03:00 occurs twice - first as CEST/+02:00,
    then again as CET/+01:00) can have an EARLIER wall-clock minute yet a LATER real instant, or
    vice versa, once their differing offsets are taken into account.

    ``earlier`` (02:35 CEST, +02:00 -> 00:35 UTC) is the real earlier instant; ``later`` (02:15
    CET, +01:00 -> 01:15 UTC) is the real later one, even though "02:15" < "02:35" as raw text -
    exactly the shape of comparison a naive offset-suffixed TEXT column gets backwards. Postgres
    (TIMESTAMPTZ, a real instant type) was never affected; this is a regression guard for
    ``SqliteParameterAdapters.adapt_datetime()`` normalizing to UTC before storage.
    """
    earlier = datetime(2024, 10, 27, 2, 35, tzinfo=dt_timezone(timedelta(hours=2)))
    later = datetime(2024, 10, 27, 2, 15, tzinfo=dt_timezone(timedelta(hours=1)))
    assert earlier < later  # sanity check on the fixture itself, not the bug under test

    model = testmodels.DatetimeFields
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        obj_earlier = await model.objects.create(datetime=earlier)
        obj_later = await model.objects.create(datetime=later)
        ids = [obj_earlier.id, obj_later.id]

        ascending = await model.objects.filter(id__in=ids).order_by("datetime")
        assert [obj.id for obj in ascending] == [obj_earlier.id, obj_later.id]

        descending = await model.objects.filter(id__in=ids).order_by("-datetime")
        assert [obj.id for obj in descending] == [obj_later.id, obj_earlier.id]

        assert await model.objects.filter(id=obj_later.id, datetime__gt=obj_earlier.datetime).exists() is True
        assert await model.objects.filter(id=obj_earlier.id, datetime__gt=obj_later.datetime).exists() is False


@pytest.mark.asyncio
async def test_date_field_year_filter_not_affected_by_configured_zone(db):
    """A DateField has no time component for a zone to shift - field__year on it must render an
    unqualified extraction (no AT TIME ZONE wrapping / no zone passed to the sqlite UDF)
    regardless of what zone is configured. Exercised directly against the registered filter's
    operator rather than through a DB round trip - this asserts the *build-time decision itself*
    (is_datetime_field is False for a DateField), not just that it happens to still work.

    Takes the `db` fixture (unused directly) purely to guarantee `testmodels.DateFields` has
    gone through a real Hare.init() at least once in this worker.
    """
    model = testmodels.DateFields
    field_lookup = FieldLookups.get(model._meta.fields_map["date"])["year"]
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        term = SqlField("date")
        criterion = field_lookup.operator(term, 2024)
        assert "AT TIME ZONE" not in criterion.get_sql(DEFAULT_SQL_CONTEXT.copy(dialect=SQLITE_DIALECT))


def test_date_field_has_no_time_of_day_extraction_suffixes(db):
    """A DateField has no time-of-day component at all - `__hour`/`__minute`/`__second`/
    `__microsecond` must not even be registered (Postgres rejects EXTRACT(HOUR FROM a date
    column) outright, sqlite's own UDF would silently treat the stored date as midnight)."""
    model = testmodels.DateFields
    for suffix in ("hour", "minute", "second", "microsecond"):
        with pytest.raises(KeyError):
            FieldLookups.get(model._meta.fields_map["date"])[suffix]


def test_time_field_has_no_calendar_extraction_suffixes(db):
    """A TimeField has no calendar component at all - `__year`/`__quarter`/`__month`/`__week`/
    `__day` must not even be registered."""
    model = testmodels.TimeFields
    for suffix in ("year", "quarter", "month", "week", "day"):
        with pytest.raises(KeyError):
            FieldLookups.get(model._meta.fields_map["time"])[suffix]


@pytest.mark.asyncio
async def test_time_field_calendar_suffix_raises_field_error_end_to_end(db):
    from hare.exceptions import FieldError

    with pytest.raises(FieldError):
        await testmodels.TimeFields.objects.filter(time__year=2020).count()


@pytest.mark.asyncio
async def test_date_field_time_of_day_suffix_raises_field_error_end_to_end(db):
    from hare.exceptions import FieldError

    with pytest.raises(FieldError):
        await testmodels.DateFields.objects.filter(date__hour=2020).count()


def test_non_date_time_field_has_no_extraction_suffixes_at_all(db):
    """Every date/time extraction suffix used to be registered for EVERY field, regardless of
    type - an ordinary IntField/CharField/etc must not get any of them."""
    model = testmodels.IntFields
    for suffix in ("year", "quarter", "month", "week", "day", "hour", "minute", "second", "microsecond"):
        with pytest.raises(KeyError):
            FieldLookups.get(model._meta.fields_map["intnum"])[suffix]


@pytest.mark.asyncio
async def test_non_date_time_field_extraction_suffix_raises_field_error_end_to_end(db):
    from hare.exceptions import FieldError

    with pytest.raises(FieldError):
        await testmodels.IntFields.objects.filter(intnum__year=2020).count()


# ============================================================================
# TestTimeFields (postgres) -> test_time_*
# ============================================================================


def test_time_field_named_zone_is_stored_as_a_fixed_offset():
    """A named zone cannot be attached to a bare time: datetime.time has no date for the zone to
    resolve DST against, so time.utcoffset() returns None and the value stays naive as far as
    Python - and Hare's own Timezone.is_naive() - are concerned. Both Postgres drivers then
    reject it outright (rust_pg: "is not a fixed offset timezone"; asyncpg: "'NoneType' object
    has no attribute 'days'"), so with any named non-UTC timezone configured EVERY TimeField
    write failed, auto_now or not. Only UTC happened to work, since ZoneInfo('UTC') reports its
    offset even with no date - which is why the whole suite, running in UTC, never caught it.
    """
    field = testmodels.TimeFields._meta.fields_map["time"]
    with override_timezone(use_timezone=True, timezone="Europe/Moscow"):
        stored = field.to_db_value(time(12, 30, 45), testmodels.TimeFields)

        assert stored.utcoffset() == timedelta(hours=3)
        assert Timezone.is_aware(stored)
        # The wall-clock digits are untouched - this attaches the offset the naive value was
        # already implicitly in, it does not convert the value to another zone.
        assert stored.replace(tzinfo=None) == time(12, 30, 45)

        # from_db_value's own naive branch has to agree, or a value would change shape
        # depending on which direction it crossed the field.
        assert field.from_db_value(time(12, 30, 45)).utcoffset() == timedelta(hours=3)


@pytest.mark.asyncio
async def test_time_round_trip_with_named_non_utc_timezone(db):
    """Companion to test_time_field_named_zone_is_stored_as_a_fixed_offset: the real driver-level
    round trip that used to fail on both Postgres drivers for a plain naive write and for the
    auto_now field alike."""
    model = testmodels.TimeFields
    with override_timezone(use_timezone=True, timezone="Europe/Moscow"):
        # No naive-value warning here: construction routes the value through from_db_value,
        # which attaches the offset before to_db_value ever sees it.
        obj0 = await model.objects.create(time=time(12, 30, 45))
        obj1 = await model.objects.get(id=obj0.id)

        assert obj1.time.utcoffset() == timedelta(hours=3)
        assert obj1.time.replace(tzinfo=None) == time(12, 30, 45)
        assert obj1.time_auto.utcoffset() == timedelta(hours=3)


@pytest.mark.asyncio
async def test_time_field_hour_minute_second_filter(db):
    """`TimeField__hour`/`__minute`/`__second`=int used to reach `TimeField.to_db_value()`
    completely unconverted (no `value_encoder` on the date-part lookup) and crash with a raw
    `AttributeError: 'int' object has no attribute 'utcoffset'` on Postgres, both drivers,
    regardless of `use_timezone`."""
    model = testmodels.TimeFields
    obj = await model.objects.create(time=time(10, 30, 15))
    assert await model.objects.filter(id=obj.id, time__hour=10).count() == 1
    assert await model.objects.filter(id=obj.id, time__minute=30).count() == 1
    assert await model.objects.filter(id=obj.id, time__second=15).count() == 1


@pytest.mark.asyncio
async def test_time_field_write_and_filter_with_use_tz_false(db):
    """A TimeField column is TIMETZ on Postgres regardless of `use_timezone` - a naive value under
    `use_timezone=False` used to reach asyncpg completely unconverted and crash with
    `invalid input for query argument $1: ... ('NoneType' object has no attribute 'utcoffset')`,
    even for a bare `auto_now` write with no explicit value at all. rust_pg already tolerated a
    naive value; this exercises both drivers for parity."""
    model = testmodels.TimeFields
    with override_timezone(use_timezone=False):
        obj = await model.objects.create(time=time(10, 30, 15))
        fetched = await model.objects.get(id=obj.id)

        assert fetched.time == time(10, 30, 15)
        assert fetched.time.tzinfo is None
        assert fetched.time_auto.tzinfo is None

        assert await model.objects.filter(id=obj.id, time=time(10, 30, 15)).count() == 1
        assert await model.objects.filter(id=obj.id, time__gte=time(10, 0, 0)).count() == 1
        assert await model.objects.filter(id=obj.id, time__range=[time(10, 0, 0), time(11, 0, 0)]).count() == 1
        assert await model.objects.filter(id=obj.id, time__in=[time(10, 30, 15)]).count() == 1


@pytest.mark.asyncio
async def test_time_create(db):
    """Test creating time fields (postgres)."""
    model = testmodels.TimeFields
    now = Timezone.now().timetz()
    obj0 = await model.objects.create(time=now)
    obj1 = await model.objects.get(id=obj0.id)
    assert obj1.time == now


@pytest.mark.asyncio
async def test_time_cast(db):
    """Test time field accepts ISO format string (postgres)."""
    model = testmodels.TimeFields
    obj0 = await model.objects.create(time="21:00+00:00")
    obj1 = await model.objects.get(id=obj0.id)
    assert obj1.time == time.fromisoformat("21:00+00:00")


@pytest.mark.asyncio
async def test_time_values(db):
    """Test time field in values() query (postgres)."""
    model = testmodels.TimeFields
    now = Timezone.now().timetz()
    obj0 = await model.objects.create(time=now)
    values = await model.objects.get(id=obj0.id).values("time")
    assert values["time"] == now


@pytest.mark.asyncio
async def test_time_values_list(db):
    """Test time field in values_list() query (postgres)."""
    model = testmodels.TimeFields
    now = Timezone.now().timetz()
    obj0 = await model.objects.create(time=now)
    values = await model.objects.get(id=obj0.id).values_list("time", flat=True)
    assert values == now


@pytest.mark.asyncio
async def test_time_get(db):
    """Test getting by time field (postgres)."""
    model = testmodels.TimeFields
    now = Timezone.now().timetz()
    await model.objects.create(time=now)
    obj = await model.objects.get(time=now)
    assert obj.time == now


# ============================================================================
# TestDateFields -> test_date_*
# ============================================================================


@pytest.mark.asyncio
async def test_empty_date_fields(db):
    """Test that creating DateFields without required field raises IntegrityError."""
    with pytest.raises(IntegrityError):
        await testmodels.DateFields.objects.create()


@pytest.mark.asyncio
async def test_date_create(db):
    """Test creating date fields."""
    model = testmodels.DateFields
    today = date.today()
    obj0 = await model.objects.create(date=today)
    obj = await model.objects.get(id=obj0.id)
    assert obj.date == today
    assert obj.date_null is None
    await obj.save()
    obj2 = await model.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_date_cast(db):
    """Test date field accepts ISO format string."""
    model = testmodels.DateFields
    today = date.today()
    obj0 = await model.objects.create(date=today.isoformat())
    obj = await model.objects.get(id=obj0.id)
    assert obj.date == today


@pytest.mark.asyncio
async def test_date_values(db):
    """Test date field in values() query."""
    model = testmodels.DateFields
    today = date.today()
    obj0 = await model.objects.create(date=today)
    values = await model.objects.get(id=obj0.id).values("date")
    assert values["date"] == today


@pytest.mark.asyncio
async def test_date_values_list(db):
    """Test date field in values_list() query."""
    model = testmodels.DateFields
    today = date.today()
    obj0 = await model.objects.create(date=today)
    values = await model.objects.get(id=obj0.id).values_list("date", flat=True)
    assert values == today


@pytest.mark.asyncio
async def test_date_get(db):
    """Test getting by date field."""
    model = testmodels.DateFields
    today = date.today()
    await model.objects.create(date=today)
    obj = await model.objects.get(date=today)
    assert obj.date == today


@pytest.mark.asyncio
async def test_date_str(db):
    """Test date field with string input and filtering/updating."""
    model = testmodels.DateFields
    obj0 = await model.objects.create(date="2020-08-17")
    obj1 = await model.objects.get(date="2020-08-17")
    assert obj0.date == obj1.date
    with pytest.raises(ValidationError):
        await model.objects.create(date="2020-08-xx")
    await model.objects.filter(date="2020-08-17").update(date="2020-08-18")
    obj2 = await model.objects.get(date="2020-08-18")
    assert obj2.date == date(year=2020, month=8, day=18)


@pytest.mark.asyncio
async def test_date_plain_attribute_assignment_rejects_malformed_string(db):
    """A malformed string set via plain attribute assignment (not construction) used to be
    saved unconverted and only fail on a later read - it must fail at save() instead."""
    model = testmodels.DateFields
    obj = await model.objects.create(date=date.today())
    obj.date = "not-a-date"
    with pytest.raises(ValidationError):
        await obj.save()


@pytest.mark.asyncio
async def test_datetime_plain_attribute_assignment_rejects_malformed_string(db):
    """Same as test_date_plain_attribute_assignment_rejects_malformed_string, for DatetimeField."""
    model = testmodels.DatetimeFields
    obj = await model.objects.create(datetime=datetime.now())
    obj.datetime = "not-a-datetime"
    with pytest.raises(ValidationError):
        await obj.save()


# ============================================================================
# TestTimeDeltaFields -> test_timedelta_*
# ============================================================================


@pytest.mark.asyncio
async def test_empty_timedelta_fields(db):
    """Test that creating TimeDeltaFields without required field raises IntegrityError."""
    with pytest.raises(IntegrityError):
        await testmodels.TimeDeltaFields.objects.create()


def test_timedelta_field_to_db_value_wraps_wrong_type():
    """TimeDeltaField.to_db_value does `value.days * ... + value.seconds * ... +
    value.microseconds` with no type check at all beforehand - a non-timedelta value (e.g. a raw
    string from a plain attribute assignment) raised a bare AttributeError ('str' object has no
    attribute 'days') instead of the framework's own ValidationError, same unwrapped-exception
    bug class as every other date/time field's own conversion step fixed this session."""
    field = testmodels.TimeDeltaFields._meta.fields_map["timedelta"]
    with pytest.raises(ValidationError):
        field.to_db_value("not-a-timedelta", testmodels.TimeDeltaFields)


@pytest.mark.asyncio
async def test_timedelta_create(db):
    """Test creating timedelta fields."""
    model = testmodels.TimeDeltaFields
    obj0 = await model.objects.create(timedelta=timedelta(days=35, seconds=8, microseconds=1))
    obj = await model.objects.get(id=obj0.id)
    assert obj.timedelta == timedelta(days=35, seconds=8, microseconds=1)
    assert obj.timedelta_null is None
    await obj.save()
    obj2 = await model.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_timedelta_values(db):
    """Test timedelta field in values() query."""
    model = testmodels.TimeDeltaFields
    obj0 = await model.objects.create(timedelta=timedelta(days=35, seconds=8, microseconds=1))
    values = await model.objects.get(id=obj0.id).values("timedelta")
    assert values["timedelta"] == timedelta(days=35, seconds=8, microseconds=1)


@pytest.mark.asyncio
async def test_timedelta_values_list(db):
    """Test timedelta field in values_list() query."""
    model = testmodels.TimeDeltaFields
    obj0 = await model.objects.create(timedelta=timedelta(days=35, seconds=8, microseconds=1))
    values = await model.objects.get(id=obj0.id).values_list("timedelta", flat=True)
    assert values == timedelta(days=35, seconds=8, microseconds=1)


@pytest.mark.asyncio
async def test_timedelta_get(db):
    """Test getting by timedelta field."""
    model = testmodels.TimeDeltaFields
    delta = timedelta(days=35, seconds=8, microseconds=2)
    await model.objects.create(timedelta=delta)
    obj = await model.objects.get(timedelta=delta)
    assert obj.timedelta == delta


def test_zoneinfo():
    tz = Timezone.parse("Asia/Shanghai")
    tz2 = Timezone.parse("asia/shanghai")
    tz3 = Timezone.parse("asia/ShangHai")
    now = datetime.now()
    assert now.replace(tzinfo=tz) == now.replace(tzinfo=tz2) == now.replace(tzinfo=tz3)
    tz = Timezone.parse("US/central")
    tz2 = Timezone.parse("US/Central")
    assert now.replace(tzinfo=tz) == now.replace(tzinfo=tz2)
    tz_utc = Timezone.parse("UTC")
    tz_utc2 = Timezone.parse("utc")
    tz_utc3 = Timezone.parse("Utc")
    assert tz_utc.key == tz_utc2.zone == "UTC"
    assert (
        now.replace(tzinfo=UTC)
        == now.replace(tzinfo=tz_utc)
        == now.replace(tzinfo=tz_utc2)
        == now.replace(tzinfo=tz_utc3)
    )
    with pytest.raises(ZoneInfoNotFoundError):
        Timezone.parse("invalid-zone-name")
    with pytest.raises(ZoneInfoNotFoundError):
        Timezone.parse("Invalid/Zonename")


def test_timezone_parse_returns_the_same_cached_instance():
    """Timezone.parse()/default() construct a ZoneInfo subclass, whose stdlib cache is a
    WeakValueDictionary - without a strong reference held somewhere, that cache never actually
    caches, and every call re-parses tzdata from disk (measured ~590x slower than the stdlib's
    own cached ZoneInfo("UTC") lookup). Timezone.ZONES holds that strong reference; this asserts
    repeated calls return the identical object, not just an equal one."""
    from hare.time import Timezone

    assert Timezone.parse("Asia/Shanghai") is Timezone.parse("Asia/Shanghai")
    # the case-insensitive UTC shortcut hits the cache too, keyed by the resolved name rather
    # than the caller's own casing.
    assert Timezone.parse("UTC") is Timezone.parse("utc") is Timezone.parse("Utc")
    # a repeated call with the SAME (correctly-cased) name is the hot path this fix targets -
    # repeated differently-cased spellings of one zone ("US/central" vs "US/Central") aren't
    # unified to one cache entry, only each exact spelling once resolved is.
    assert Timezone.parse("US/Central") is Timezone.parse("US/Central")

    with override_timezone(use_timezone=True, timezone="Asia/Shanghai"):
        assert Timezone.default() is Timezone.default() is Timezone.ZONES[("Asia/Shanghai",)]


def test_timezone():
    with override_timezone(use_timezone=True):
        # test localtime
        assert Timezone.localtime() <= Timezone.now() <= Timezone.localtime()
        utcnow = datetime.now(UTC)
        tz_shanghai = ZoneInfo("Asia/Shanghai")
        assert Timezone.localtime(utcnow).utcoffset() == Timezone.now().utcoffset()
        localtime_shanghai = Timezone.localtime(utcnow, tz_shanghai.key)
        assert Timezone.localtime(utcnow, tz_shanghai) == localtime_shanghai
        assert localtime_shanghai.utcoffset() != Timezone.localtime(utcnow, "UTC").utcoffset()
        naive_dt = datetime.now()
        with pytest.raises(ValueError):
            Timezone.localtime(naive_dt)
        # test make_naive
        assert Timezone.make_naive(Timezone.now()).tzinfo is None
        with pytest.raises(ValueError):
            Timezone.make_naive(naive_dt)
        tz_shanghai = ZoneInfo("Asia/Shanghai")
        now_shanghai = datetime.now(tz_shanghai)
        offset = now_shanghai.utcoffset()
        naive_now = Timezone.make_naive(utcnow, tz_shanghai.key)
        assert (utcnow + offset).isoformat().split("+")[0] == naive_now.isoformat()
        # test make_aware
        with pytest.raises(ValueError):
            Timezone.make_aware(utcnow)
        aware_now = Timezone.make_aware(naive_now, tz_shanghai.key)
        assert aware_now.utcoffset() == now_shanghai.utcoffset()
        assert "+" in Timezone.make_aware(naive_now).isoformat()
        # test compatible with pytz
        with contextlib.suppress(ImportError):
            import pytz

            aware_now = Timezone.make_aware(naive_now, pytz.timezone(tz_shanghai.key))
            assert aware_now.utcoffset() == now_shanghai.utcoffset()
            pytz_shanghai = pytz.timezone(tz_shanghai.key)
            assert pytz_shanghai.zone == tz_shanghai.zone == tz_shanghai.key
            assert localtime_shanghai == Timezone.localtime(utcnow, pytz_shanghai)
            assert localtime_shanghai.utcoffset() == Timezone.localtime(timezone=pytz_shanghai).utcoffset()


def test_make_aware_is_dst_disambiguates_ambiguous_wall_clock():
    """Timezone.make_aware(..., is_dst=...) used to silently drop is_dst for every zone
    Timezone.parse() can actually produce - it only reads `is_dst` on the `hasattr(tz,
    "localize")` (pytz) branch, but `parse()` only ever returns a ZoneInfo, which has no
    `localize`, so every real call fell through to the unfolded `value.replace(tzinfo=tz)` -
    always the fold=0/daylight reading, regardless of what the caller asked for.

    02:30 on 2024-10-27 in Europe/Berlin occurs twice (the fall-back from CEST to CET) - the
    daylight (fold=0, UTC+2) and standard (fold=1, UTC+1) readings are genuinely different
    instants, an hour apart, so this can only be told apart on a real DST-observing zone.
    """
    ambiguous_wall_clock = datetime(2024, 10, 27, 2, 30)
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        daylight_reading = Timezone.make_aware(ambiguous_wall_clock, is_dst=True)
        standard_reading = Timezone.make_aware(ambiguous_wall_clock, is_dst=False)

        assert daylight_reading.utcoffset() == timedelta(hours=2)
        assert standard_reading.utcoffset() == timedelta(hours=1)
        assert daylight_reading.astimezone(UTC) == standard_reading.astimezone(UTC) - timedelta(hours=1)

        # is_dst=None (the default) is unchanged - still the daylight/fold=0 reading.
        default_reading = Timezone.make_aware(ambiguous_wall_clock)
        assert default_reading.utcoffset() == daylight_reading.utcoffset()


def test_make_aware_is_dst_no_effect_on_unambiguous_wall_clock():
    """A wall clock that only occurs once has a single correct reading regardless of is_dst -
    both folds resolve to the identical instant, so this must never differ."""
    unambiguous_wall_clock = datetime(2024, 6, 15, 12, 0)
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        with_dst_true = Timezone.make_aware(unambiguous_wall_clock, is_dst=True)
        with_dst_false = Timezone.make_aware(unambiguous_wall_clock, is_dst=False)
        assert with_dst_true == with_dst_false
        assert with_dst_true.utcoffset() == timedelta(hours=2)


def test_make_aware_raises_on_nonexistent_spring_forward_gap():
    """02:30 on 2024-03-31 in Europe/Berlin does not exist: the zone jumps straight from 01:59:59
    CET to 03:00:00 CEST that day. `is_dst=None` (the default, and what every write path in Hare
    actually calls with - Model.objects.create()/save()/.update()/bulk_create() never pass `is_dst`) used
    to silently fall through to the unfolded `value.replace(tzinfo=tz)`, producing an aware value
    that, read back, showed 03:30 instead of the 02:30 that was actually written - a silent
    one-hour shift with no error at all. It must now raise instead."""
    gap_wall_clock = datetime(2024, 3, 31, 2, 30)
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        with pytest.raises(NonExistentTimeError):
            Timezone.make_aware(gap_wall_clock)


def test_make_aware_raises_on_nonexistent_spring_forward_gap_regardless_of_is_dst():
    """Unlike an ambiguous (fall-back) wall clock, a nonexistent (gap) one has no real reading
    under either fold - is_dst can't rescue it, so both explicit values must still raise."""
    gap_wall_clock = datetime(2024, 3, 31, 2, 30)
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        with pytest.raises(NonExistentTimeError):
            Timezone.make_aware(gap_wall_clock, is_dst=True)
        with pytest.raises(NonExistentTimeError):
            Timezone.make_aware(gap_wall_clock, is_dst=False)


@pytest.mark.asyncio
async def test_datetime_create_raises_on_spring_forward_gap(db):
    """End-to-end regression for the DST spring-forward gap bug: creating a DatetimeField with a
    genuinely nonexistent naive wall-clock value must raise, not silently store a value shifted
    forward by the zone's DST delta."""
    model = testmodels.DatetimeFields
    gap_naive = datetime(2024, 3, 10, 2, 30, 0)  # America/Chicago: 01:59:59 CST -> 03:00:00 CDT
    with override_timezone(use_timezone=True, timezone="America/Chicago"):
        with pytest.raises(NonExistentTimeError):
            await model.objects.create(datetime=gap_naive)


def test_get_fixed_offset_default_is_independent_of_calendar_moment():
    """get_fixed_offset() without `at` used to inherit whatever offset applies to the REAL
    calendar moment the call happens to run at (`datetime.now(tz=UTC)`) - the same call made in
    winter and in summer returned two different offsets for a DST-observing zone, even though a
    bare time (no date of its own) has no "current" offset to begin with. It must now always
    return the zone's fixed standard (non-DST) offset, regardless of when the call is made."""
    winter_moment = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    summer_moment = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    with override_timezone(use_timezone=True, timezone="America/Chicago"):
        with patch("hare.time.timezone.datetime") as mock_datetime:
            mock_datetime.now.return_value = winter_moment
            winter_offset = Timezone.get_fixed_offset()
        with patch("hare.time.timezone.datetime") as mock_datetime:
            mock_datetime.now.return_value = summer_moment
            summer_offset = Timezone.get_fixed_offset()

        assert winter_offset == summer_offset == dt_timezone(timedelta(hours=-6))
        # `at=` explicit override still reports whatever offset actually applied at that moment.
        assert Timezone.get_fixed_offset(at=summer_moment).utcoffset(None) == timedelta(hours=-5)


@pytest.mark.asyncio
async def test_time_field_offset_independent_of_write_calendar_moment(db):
    """Companion DB round trip for test_get_fixed_offset_default_is_independent_of_calendar_moment:
    the same naive TimeField value written in winter and in summer must store the identical
    offset, and .filter(time=that same naive value) must find BOTH rows - previously each row
    got whichever offset happened to apply at its own write time, making them unequal as Python
    objects and invisible to each other's filter."""
    model = testmodels.TimeFields
    winter_moment = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    summer_moment = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    naive_value = time(9, 0, 0)
    with override_timezone(use_timezone=True, timezone="America/Chicago"):
        with patch("hare.time.timezone.datetime") as mock_datetime:
            mock_datetime.now.return_value = winter_moment
            winter_obj = await model.objects.create(time=naive_value)
        with patch("hare.time.timezone.datetime") as mock_datetime:
            mock_datetime.now.return_value = summer_moment
            summer_obj = await model.objects.create(time=naive_value)

        assert winter_obj.time.utcoffset() == summer_obj.time.utcoffset() == timedelta(hours=-6)

        found = await model.objects.filter(time=naive_value)
        found_ids = {obj.id for obj in found}
        assert {winter_obj.id, summer_obj.id} <= found_ids


def test_timefield_auto_now_only():
    """Test TimeField with only auto_now=True."""
    field = fields.TimeField(auto_now=True)
    assert field.auto_now is True
    assert field.auto_now_add is False


def test_timefield_auto_now_add_only():
    """Test TimeField with only auto_now_add=True."""
    field = fields.TimeField(auto_now_add=True)
    assert field.auto_now is False
    assert field.auto_now_add is True


def test_timefield_both_flags_raises():
    """Test TimeField raises when both auto_now and auto_now_add are True."""
    with pytest.raises(ConfigurationError, match="You can choose only 'auto_now' or 'auto_now_add'"):
        fields.TimeField(auto_now=True, auto_now_add=True)


# ============================================================================
# auto_now / epoch / infinity consistency across write and read paths
# ============================================================================


def test_time_field_auto_now_uses_standard_offset_in_summer():
    """TimeField(auto_now=True) stamps the local wall clock with the zone's STANDARD offset - the
    same offset every naive time gets - not the offset of the current (DST) moment."""
    summer_moment = datetime(2024, 7, 15, 16, 0, tzinfo=UTC)
    with override_timezone(use_timezone=True, timezone="America/New_York"):
        stamped = fields.TimeField.get_auto_now_value(summer_moment)
        naive_written = testmodels.TimeFields._meta.fields_map["time"].from_db_value(time(12, 0))
    assert stamped == time(12, 0, tzinfo=dt_timezone(timedelta(hours=-5)))
    assert stamped == naive_written


def test_time_field_auto_now_naive_local_with_use_tz_false():
    with override_timezone(use_timezone=False):
        stamped = fields.TimeField.get_auto_now_value(datetime(2024, 7, 15, 16, 0))
    assert stamped == time(16, 0)
    assert stamped.tzinfo is None


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_time_field_auto_now_bumped_as_time_by_queryset_update(db):
    """QuerySet.update() bumps a TimeField(auto_now=True) with a local time, like save() -
    previously a full datetime, which failed under use_timezone=False and stored the UTC wall clock
    under use_timezone=True."""
    model = testmodels.TimeFields
    with override_timezone(use_timezone=False):
        obj = await model.objects.create(time=time(10, 0))
        assert await model.objects.filter(id=obj.id).update(time=time(11, 0)) == 1
        fetched = await model.objects.get(id=obj.id)
        assert fetched.time == time(11, 0)
        assert fetched.time_auto.tzinfo is None
    tz = "Asia/Tokyo"
    with override_timezone(use_timezone=True, timezone=tz):
        obj = await model.objects.create(time=time(10, 0))
        before = datetime.now(UTC)
        await model.objects.filter(id=obj.id).update(time=time(11, 0))
        fetched = await model.objects.get(id=obj.id)
        assert fetched.time_auto.utcoffset() == timedelta(hours=9)
        local_before = Timezone.localtime(before, tz)
        stamped = datetime.combine(local_before.date(), fetched.time_auto.replace(tzinfo=None))
        assert abs((stamped - local_before.replace(tzinfo=None)).total_seconds()) < 5


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_time_field_auto_now_bumped_by_bulk_update_with_use_tz_false(db):
    model = testmodels.TimeFields
    with override_timezone(use_timezone=False):
        obj = await model.objects.create(time=time(10, 0))
        obj.time = time(12, 0)
        await model.objects.bulk_update([obj], fields=["time"])
        fetched = await model.objects.get(id=obj.id)
        assert fetched.time == time(12, 0)
        assert fetched.time_auto.tzinfo is None


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_time_field_large_in_list_with_use_tz_false(db):
    """A TimeField __in list large enough to be bound as ONE TIMETZ[] array parameter must encode
    naive times under use_timezone=False (both drivers used to fail on it)."""
    model = testmodels.TimeFields
    with override_timezone(use_timezone=False):
        obj = await model.objects.create(time=time(10, 0))
        values = [time(10, 0)] + [time(1, minute) for minute in range(40)]
        assert await model.objects.filter(id=obj.id, time__in=values).count() == 1
        assert await model.objects.filter(id=obj.id, time__not_in=values[1:]).count() == 1


@pytest.mark.asyncio
@test.requires_features(dialect="postgresql")
async def test_time_field_bulk_create_copy_with_use_tz_false(db_truncate):
    """bulk_create(use_copy=True) with naive TimeField values (explicit and auto_now) under
    use_timezone=False - rust_pg's binary COPY used to reject a naive time for a TIMETZ column."""
    model = testmodels.TimeFields
    with override_timezone(use_timezone=False):
        await model.objects.bulk_create([model(time=time(10, 0)), model(time=time(11, 30))], use_copy=True)
        assert sorted(obj.time for obj in await model.objects.all()) == [time(10, 0), time(11, 30)]
        assert await model.objects.filter(time=time(11, 30)).count() == 1


EPOCH_SECONDS = 1700000000


@pytest.mark.asyncio
async def test_datetime_epoch_int_accepted_by_every_write_path(db):
    """An int epoch is converted the same way by create(), filter(), update() and save() after a
    plain attribute assignment - previously only create() converted it (SQLite stored a raw
    INTEGER, Postgres raised)."""
    model = testmodels.DatetimeFields
    obj = await model.objects.create(datetime=EPOCH_SECONDS)
    assert obj.datetime == datetime.fromtimestamp(EPOCH_SECONDS, tz=UTC)
    assert await model.objects.filter(datetime=EPOCH_SECONDS).count() == 1
    assert await model.objects.filter(datetime__in=[EPOCH_SECONDS]).count() == 1

    assert await model.objects.filter(id=obj.id).update(datetime=EPOCH_SECONDS + 60) == 1
    fetched = await model.objects.get(id=obj.id)
    assert fetched.datetime == datetime.fromtimestamp(EPOCH_SECONDS + 60, tz=UTC)
    assert await model.objects.filter(datetime__gte=datetime(2023, 11, 14, tzinfo=UTC)).count() == 1

    fetched.datetime = EPOCH_SECONDS + 120
    await fetched.save()
    assert (await model.objects.get(id=obj.id)).datetime == datetime.fromtimestamp(EPOCH_SECONDS + 120, tz=UTC)


@pytest.mark.asyncio
async def test_datetime_field_to_db_value_converts_epoch_int_with_use_tz_false(db):
    field = testmodels.DatetimeFields._meta.fields_map["datetime"]
    types = testmodels.DatetimeFields._meta.connection.dialect.types
    with override_timezone(use_timezone=False):
        value = types.get_db_value(field, EPOCH_SECONDS, testmodels.DatetimeFields)
        stores_utc_instants = types.get_db_converter(type(field)) is not None
    if stores_utc_instants:
        # A Postgres TIMESTAMPTZ column is bound the instant itself.
        assert value == datetime.fromtimestamp(EPOCH_SECONDS, tz=UTC)
    else:
        assert value == datetime.fromtimestamp(EPOCH_SECONDS)
        assert value.tzinfo is None


@pytest.mark.parametrize("pure_python", [False, True])
@pytest.mark.asyncio
async def test_datetime_max_round_trips_in_non_utc_zone(db, pure_python):
    """datetime.max (UTC) round-trips as the same instant on every backend - asyncpg reads a
    TIMESTAMPTZ 'infinity' back as a NAIVE datetime.max, which used to be tagged with the
    configured zone instead of being read as UTC."""
    from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

    model = testmodels.DatetimeFields
    value = datetime.max.replace(tzinfo=UTC)
    hydrate_patch = patch.object(HydrateAccelerator, "module", None) if pure_python else contextlib.nullcontext()
    with override_timezone(use_timezone=True, timezone="America/New_York"), hydrate_patch:
        obj = await model.objects.create(datetime=value)
        fetched = await model.objects.get(id=obj.id)
    assert fetched.datetime == value


def test_datetime_field_to_python_value_reads_naive_max_as_utc():
    field = testmodels.DatetimeFields._meta.fields_map["datetime"]
    with override_timezone(use_timezone=True, timezone="America/New_York"):
        assert field.from_db_value(datetime.max) == datetime.max.replace(tzinfo=UTC)
    with override_timezone(use_timezone=True, timezone="Asia/Tokyo"):
        assert field.from_db_value(datetime.min) == datetime.min.replace(tzinfo=UTC)


# ============================================================================
# Write-path validation: every write path rejects a value that isn't a date/datetime
# ============================================================================

INVALID_DATETIME_VALUES = ["2024", "2024-05", "abc", 1.5, True, b"2024-01-01"]
INVALID_DATE_VALUES = ["2024", "2024-05", "abc", 1.5, 20240101, True]


async def assert_every_write_path_rejects(model, field_name: str, value, **required) -> None:
    """Asserts create/bulk_create/update/save/bulk_update/update_or_create all raise ValidationError
    for `value`, and that nothing unreadable was written."""
    existing = await model.objects.create(**required)
    with pytest.raises(ValidationError):
        await model.objects.create(**{**required, field_name: value})
    with pytest.raises(ValidationError):
        await model.objects.bulk_create([model(**{**required, field_name: value})])
    with pytest.raises(ValidationError):
        await model.objects.filter(id=existing.id).update(**{field_name: value})
    with pytest.raises(ValidationError):
        await model.objects.update_or_create(id=existing.id, defaults={field_name: value})
    instance = await model.objects.get(id=existing.id)
    setattr(instance, field_name, value)
    with pytest.raises(ValidationError):
        await instance.save()
    instance = await model.objects.get(id=existing.id)
    setattr(instance, field_name, value)
    with pytest.raises(ValidationError):
        await model.objects.bulk_update([instance], fields=[field_name])
    assert await model.objects.filter(id=existing.id).values_list(field_name, flat=True) == [
        getattr(existing, field_name)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", INVALID_DATETIME_VALUES)
async def test_datetime_field_rejects_non_datetime_on_every_write_path(db, value):
    """A float, a bare year or other non-date text used to be stored verbatim on SQLite (an
    unreadable row) and reach Postgres as a raw driver error."""
    await assert_every_write_path_rejects(
        testmodels.DatetimeFields, "datetime_null", value, datetime=datetime(2024, 1, 1, tzinfo=UTC)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", INVALID_DATE_VALUES)
async def test_date_field_rejects_non_date_on_every_write_path(db, value):
    await assert_every_write_path_rejects(testmodels.DateFields, "date_null", value, date=date(2024, 1, 1))


@pytest.mark.asyncio
async def test_datetime_field_out_of_range_epoch_is_a_validation_error(db):
    with pytest.raises(ValidationError, match="out of the supported datetime range"):
        await testmodels.DatetimeFields.objects.create(datetime=10**12)
    obj = await testmodels.DatetimeFields.objects.create(datetime=datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ValidationError, match="out of the supported datetime range"):
        await testmodels.DatetimeFields.objects.filter(id=obj.id).update(datetime=10**12)


@pytest.mark.asyncio
async def test_datetime_year_lookups_still_accept_a_bare_year(db):
    """Only a written value must be a full date - `__year` compares an extracted number."""
    obj = await testmodels.DatetimeFields.objects.create(datetime=datetime(2024, 5, 1, tzinfo=UTC))
    date_obj = await testmodels.DateFields.objects.create(date=date(2024, 5, 1))

    assert await testmodels.DatetimeFields.objects.filter(id=obj.id, datetime__year="2024").count() == 1
    assert await testmodels.DatetimeFields.objects.filter(id=obj.id, datetime__year=2024).count() == 1
    assert await testmodels.DateFields.objects.filter(id=date_obj.id, date__year="2024").count() == 1


@pytest.mark.asyncio
async def test_datetime_field_create_with_a_date_stores_its_midnight(db):
    """create(dt=date(...)) raised a raw parse error while save()/update() wrote its midnight."""
    with override_timezone(use_timezone=True, timezone="UTC"):
        obj = await testmodels.DatetimeFields.objects.create(datetime=date(2024, 5, 5))
        assert obj.datetime == datetime(2024, 5, 5, tzinfo=UTC)
        assert (await testmodels.DatetimeFields.objects.get(id=obj.id)).datetime == datetime(2024, 5, 5, tzinfo=UTC)
        await testmodels.DatetimeFields.objects.filter(id=obj.id).update(datetime=date(2024, 6, 6))
        assert (await testmodels.DatetimeFields.objects.get(id=obj.id)).datetime == datetime(2024, 6, 6, tzinfo=UTC)


def test_construction_errors_are_validation_errors():
    """Model(...) construction raised raw ParseError/ValueError/TypeError."""
    with pytest.raises(ValidationError):
        testmodels.DatetimeFields(datetime="garbage")
    with pytest.raises(ValidationError):
        testmodels.DatetimeFields(datetime=1.5)
    with pytest.raises(ValidationError):
        testmodels.DateFields(date="garbage")
    with pytest.raises(ValidationError):
        testmodels.DateFields(date=20240101)
    with pytest.raises(ValidationError):
        testmodels.TimeFields(time="25:99")
    with pytest.raises(ValidationError):
        testmodels.TimeFields(time=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timezone_name, value",
    [
        ("America/New_York", datetime(1, 1, 1, tzinfo=UTC)),
        ("Asia/Tokyo", datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ("Asia/Tokyo", datetime(1, 1, 1, 3, 0)),
    ],
)
async def test_datetime_out_of_range_in_the_configured_zone_is_a_validation_error(db, timezone_name, value):
    """A value that overflows once converted to the configured zone (or to UTC) raised a raw
    OverflowError or a driver error, differently per backend."""
    with override_timezone(use_timezone=True, timezone=timezone_name):
        with pytest.raises(ValidationError, match="out of the supported datetime range"):
            await testmodels.DatetimeFields.objects.create(datetime=value)


# ============================================================================
# use_timezone=False before 1970 - datetime.astimezone() fails there on Windows
# ============================================================================


@pytest.mark.asyncio
async def test_pre_epoch_datetime_round_trips_with_use_tz_false(db):
    naive = datetime(1965, 6, 1, 12, 0)
    aware = datetime(1965, 6, 1, 12, 0, tzinfo=UTC)
    with override_timezone(use_timezone=False):
        naive_obj = await testmodels.DatetimeFields.objects.create(datetime=naive)
        aware_obj = await testmodels.DatetimeFields.objects.create(datetime=aware)
        naive_read = await testmodels.DatetimeFields.objects.get(id=naive_obj.id)
        aware_read = await testmodels.DatetimeFields.objects.get(id=aware_obj.id)
        values = await testmodels.DatetimeFields.objects.filter(id=aware_obj.id).values_list("datetime", flat=True)

        # rust_pg binds a naive value through its own notion of the local zone, so only the
        # round trip's consistency is checked here, not the exact wall clock.
        assert naive_read.datetime.tzinfo is None
        assert aware_read.datetime.tzinfo is None
        assert values == [aware_read.datetime]
        assert await testmodels.DatetimeFields.objects.filter(id=aware_obj.id, datetime=aware).count() == 1
        assert (
            await testmodels.DatetimeFields.objects.filter(id=naive_obj.id, datetime=naive_read.datetime).count() == 1
        )


def test_system_local_conversion_falls_back_when_astimezone_fails():
    """`datetime.astimezone()` raises OSError before 1970 on Windows - the system zone is taken
    from `Timezone.get_system_zone_fallback()` instead."""
    aware = datetime(1965, 6, 1, 12, 0, tzinfo=UTC)
    fallback_zone = dt_timezone(timedelta(hours=3))

    class FailingDatetime(datetime):
        def astimezone(self, tz=None):
            if tz is None:
                raise OSError(22, "Invalid argument")
            return super().astimezone(tz)

    failing_aware = FailingDatetime(1965, 6, 1, 12, 0, tzinfo=UTC)
    failing_naive = FailingDatetime(1965, 6, 1, 12, 0)
    with patch.object(Timezone, "get_system_zone_fallback", return_value=fallback_zone):
        assert Timezone.get_system_local_naive(failing_aware) == datetime(1965, 6, 1, 15, 0)
        assert Timezone.make_system_local_aware(failing_naive) == datetime(1965, 6, 1, 9, 0, tzinfo=UTC)
    assert Timezone.get_system_local_naive(aware).tzinfo is None


# ============================================================================
# TimeField on SQLite
# ============================================================================


@pytest.mark.asyncio
async def test_time_field_round_trips_and_filters_on_every_backend(db):
    """SQLite couldn't bind a datetime.time at all."""
    with override_timezone(use_timezone=False):
        early = await testmodels.TimeFields.objects.create(time=time(9, 5, 1, 250))
        late = await testmodels.TimeFields.objects.create(time=time(17, 30))

        assert (await testmodels.TimeFields.objects.get(id=early.id)).time == time(9, 5, 1, 250)
        assert await testmodels.TimeFields.objects.filter(time__lt=time(12, 0)).values_list("id", flat=True) == [
            early.id
        ]
        assert await testmodels.TimeFields.objects.filter(time__in=[time(17, 30)]).values_list("id", flat=True) == [
            late.id
        ]
        assert await testmodels.TimeFields.objects.all().order_by("-time").values_list("id", flat=True) == [
            late.id,
            early.id,
        ]
        assert await testmodels.TimeFields.objects.filter(time__hour=17).values_list("id", flat=True) == [late.id]

    offset_value = time(12, 30, tzinfo=dt_timezone(timedelta(hours=3)))
    with override_timezone(use_timezone=True, timezone="UTC"):
        aware = await testmodels.TimeFields.objects.create(time=offset_value)
        read = (await testmodels.TimeFields.objects.get(id=aware.id)).time
        assert read == offset_value
        assert read.utcoffset() == timedelta(hours=3)
