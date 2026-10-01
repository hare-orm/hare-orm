import datetime
from decimal import Decimal

import pytest

from hare.contrib import test
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.fields.ranges import (
    BigIntRangeField,
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
    Range,
)
from hare.dialects.postgresql.lookups.encoders import PostgresqlValueEncoders
from hare.dialects.registry import DialectRegistry
from hare.exceptions import UnSupportedError
from hare.query.expressions import F
from hare.sql.functions import Cast
from hare.utils import Timezone
from tests.dialects.postgresql.models_ranges import (
    DateRangeThing,
    DateTimeRangeThing,
    DecimalRangeThing,
    GeneratedRangeThing,
    RangeThing,
)
from tests.utils.timezone_context import override_timezone


@pytest.mark.parametrize(
    "field_cls, expected_sql_type",
    [
        (IntRangeField, "int4range"),
        (BigIntRangeField, "int8range"),
        (DecimalRangeField, "numrange"),
        (DateRangeField, "daterange"),
        (DateTimeRangeField, "tstzrange"),
    ],
)
def test_sql_type_raises_unsupported_error_for_non_postgres_dialect(field_cls, expected_sql_type):
    """Every RangeField subclass's SQL_TYPE (int4range/int8range/numrange/daterange/tstzrange) is
    Postgres-only - resolving it for another dialect's DDL must raise a clear ConfigurationError
    instead of silently handing back Postgres syntax for schema generation to choke on."""
    field = field_cls()
    field.model_field_name = "span"
    with pytest.raises(UnSupportedError, match=f"{field_cls.__name__}.*span.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == expected_sql_type


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_date_range_bounds_are_coerced_from_strings(db_ranges):
    """RangeField.to_db_value() never coerced lower/upper through its own element type (unlike
    ArrayField, which does this via base_field.to_db_value()) - a string bound reached the driver
    completely unconverted. asyncpg's own range codec at least rejected it with a catchable
    OperationalError, but rust_pg's range encoder has no equivalent check to its own Value::Array
    one and silently wrote the string's raw bytes as if they were already the element's binary
    wire format - Postgres then canonicalized the nonsense result to 'empty', so the row silently
    stored a wrong, empty range instead of the dates the caller actually asked for."""
    obj = await DateRangeThing.objects.create(span=Range(lower="2024-01-01", upper="2024-01-10"))
    fresh = await DateRangeThing.objects.get(pk=obj.pk)
    assert fresh.span.is_empty is False
    assert fresh.span.lower is not None
    assert fresh.span.upper is not None
    assert fresh.span.lower.isoformat() == "2024-01-01"
    assert fresh.span.upper.isoformat() == "2024-01-10"


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_decimal_range_bounds_are_coerced_from_strings(db_ranges):
    obj = await DecimalRangeThing.objects.create(
        span=Range(lower="1.5", upper="10.5", lower_inc=False, upper_inc=True)
    )
    fresh = await DecimalRangeThing.objects.get(pk=obj.pk)
    assert fresh.span.is_empty is False
    assert str(fresh.span.lower) == "1.5"
    assert str(fresh.span.upper) == "10.5"


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_int_range_bounds_are_coerced_from_strings(db_ranges):
    obj = await RangeThing.objects.create(span=Range(lower="1", upper="10"))
    fresh = await RangeThing.objects.get(pk=obj.pk)
    assert fresh.span == Range(lower=1, upper=10, lower_inc=True, upper_inc=False)


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_normal_range_round_trips(db_ranges):
    obj = await RangeThing.objects.create(span=Range(lower=1, upper=10))
    fresh = await RangeThing.objects.get(pk=obj.pk)
    assert fresh.span == Range(lower=1, upper=10, lower_inc=True, upper_inc=False)
    assert fresh.span.is_empty is False


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_empty_range_round_trips_without_becoming_unbounded(db_ranges):
    """RangeField.from_db_value used to decode an asyncpg.Range without reading its own
    isempty flag - a genuinely empty range (Postgres canonicalizes e.g. equal bounds under the
    default half-open [) interpretation to 'empty') decodes with lower=None/upper=None, the
    exact same shape a real UNBOUNDED range reports. Writing it back (e.g. the row's span
    column getting no explicit update but still passing through to_db_value/from_db_value on
    a full-row save()) reconstructed asyncpg.Range(None, None, ...) with no empty= kwarg
    (defaults to False) - Postgres then stored a universal (-infinity, +infinity) range instead
    of the original empty one, the semantic opposite of the value that was actually there.
    Also exercises the identical gap in the rust_pg driver's own value.rs (extract_range/
    range_to_py never reading/writing is_empty), fixed the same way."""
    # Postgres canonicalizes equal bounds under the default [) interpretation to 'empty' -
    # an ordinary value an application might construct (e.g. a zero-length interval).
    obj = await RangeThing.objects.create(span=Range(lower=5, upper=5))
    fresh = await RangeThing.objects.get(pk=obj.pk)
    assert fresh.span.is_empty is True

    # Saving the row again (as a full-row UPDATE would, incidental to some unrelated change)
    # must not corrupt the already-empty range into an unbounded one.
    await fresh.save()
    reloaded = await RangeThing.objects.get(pk=obj.pk)
    assert reloaded.span.is_empty is True
    assert reloaded.span.lower is None
    assert reloaded.span.upper is None


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_unbounded_range_is_distinct_from_empty(db_ranges):
    obj = await RangeThing.objects.create(span=Range(lower=None, upper=None))
    fresh = await RangeThing.objects.get(pk=obj.pk)
    assert fresh.span.is_empty is False
    assert fresh.span.lower is None
    assert fresh.span.upper is None


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_freshly_constructed_empty_range_is_stored_as_empty(db_ranges):
    """Sibling of test_empty_range_round_trips_without_becoming_unbounded, but for a
    Range(is_empty=True) built directly by the caller rather than one decoded from a prior
    Postgres value - the rust_pg driver's extract_range() never even attempted to read
    is_empty off the Python object at all, so this write-from-scratch case was broken
    independently of (and more broadly than) the decode-then-rewrite round trip."""
    obj = await RangeThing.objects.create(span=Range(lower=None, upper=None, is_empty=True))
    fresh = await RangeThing.objects.get(pk=obj.pk)
    assert fresh.span.is_empty is True
    assert fresh.span.lower is None
    assert fresh.span.upper is None


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_lookup_uses_real_range_containment_not_a_text_substring_match(db_ranges):
    """RangeField fell through entirely into the generic scalar-field filter dict (no
    isinstance(field, RangeField) branch existed) - its __contains lookup silently ran the
    generic dict's string-cast `Cast(field, VARCHAR) LIKE '%value%'` instead of a real Postgres
    range-containment check. `span__contains=0` against a stored `[100,200)` used to match,
    since "0" is a substring of the range's own rendered text "[100,200)", despite 0 obviously
    not being IN that range - a silent wrong-results bug, not a crash."""
    entry = await RangeThing.objects.create(span=Range(lower=100, upper=200))

    assert await RangeThing.objects.filter(span__contains=0) == []
    assert await RangeThing.objects.filter(span__contains=150) == [entry]
    assert await RangeThing.objects.filter(span__contains=Range(lower=110, upper=120)) == [entry]
    assert await RangeThing.objects.filter(span__contains=Range(lower=90, upper=120)) == []


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_lookup_on_an_annotated_alias_uses_real_range_containment(db_ranges):
    """Filtering directly on the field (the test above) goes through get_filters_for_field()
    with the real field object - filtering on a .annotate()'d ALIAS of it used a completely
    separate, field-blind code path (Q._get_custom_kwarg(), built from QuerySet.
    _annotation_filters_for_key() with field=None, since an annotation's real output_field
    isn't known until query-build time) that fell through to the exact same generic-dict
    substring-LIKE bug the direct-field path was already fixed for. `myspan__contains=0`
    against a stored `[100,200)` used to either match on `10` being a text-substring of
    `[100,200)` (a string value) or crash outright (`__contains=0`, an int, into `escape_like()`,
    which expects a string)."""
    entry = await RangeThing.objects.create(span=Range(lower=100, upper=200))

    assert await RangeThing.objects.annotate(myspan=F("span")).filter(myspan__contains=0) == []
    assert await RangeThing.objects.annotate(myspan=F("span")).filter(myspan__contains=150) == [entry]
    assert await RangeThing.objects.annotate(myspan=F("span")).filter(
        myspan__contains=Range(lower=110, upper=120)
    ) == [entry]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_overlap_and_contained_by_lookups(db_ranges):
    """__overlap/__contained_by didn't exist at all for RangeField before this - filtering on
    either raised FieldError (`Unknown filter param`), not a silent wrong result, but the
    functionality itself - the main practical reason to reach for a range field at all - was
    simply missing."""
    entry = await RangeThing.objects.create(span=Range(lower=100, upper=200))

    assert await RangeThing.objects.filter(span__overlap=Range(lower=150, upper=300)) == [entry]
    assert await RangeThing.objects.filter(span__overlap=Range(lower=300, upper=400)) == []

    assert await RangeThing.objects.filter(span__contained_by=Range(lower=0, upper=1000)) == [entry]
    assert await RangeThing.objects.filter(span__contained_by=Range(lower=110, upper=120)) == []


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_equality_and_isnull_lookups_still_work(db_ranges):
    """RangeField's own dedicated filter dict (get_range_filter()) must still cover the ordinary
    lookups the generic dict already handled correctly - eq/not/isnull/not_isnull - not just the
    3 new/fixed ones above."""
    entry = await RangeThing.objects.create(span=Range(lower=100, upper=200))
    other = await RangeThing.objects.create(span=Range(lower=300, upper=400))

    assert await RangeThing.objects.filter(span=Range(lower=100, upper=200)) == [entry]
    assert await RangeThing.objects.filter(span__not=Range(lower=100, upper=200)) == [other]
    assert await RangeThing.objects.filter(span__isnull=True) == []
    assert await RangeThing.objects.filter(span__isnull=False).order_by("id") == [entry, other]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_contains_and_overlap_lookups_work_on_a_generated_range_column(db_ranges):
    """GeneratedField wraps its real SQL/Python type via composition (output_field=), not
    inheritance - isinstance(field, RangeField) was False for GeneratedField(output_field=
    DecimalRangeField()) even though the generated column IS one at the SQL level, so filtering
    fell through into the generic scalar-field dict (the same text-substring-match bug
    get_range_filter() was written to avoid for a plain RangeField) and __overlap wasn't
    registered at all. Confirmed live: span__contains=0 against a stored [10,20) matched by
    rendered-text substring, and span__overlap raised FieldError entirely."""
    await GeneratedRangeThing.objects.create(low=10, high=20)
    await GeneratedRangeThing.objects.create(low=100, high=200)

    assert [t.low for t in await GeneratedRangeThing.objects.filter(span__contains=15)] == [10]
    assert await GeneratedRangeThing.objects.filter(span__contains=0) == []

    assert [t.low for t in await GeneratedRangeThing.objects.filter(span__overlap=(15, 25))] == [10]
    assert await GeneratedRangeThing.objects.filter(span__overlap=(300, 400)) == []


def test_range_or_element_encoder_normalizes_a_naive_datetime_scalar_to_the_configured_timezone():
    """A bare naive datetime for `__contains` skipped `DateTimeRangeField.coerce_bound()` entirely
    (only the Range/tuple branch went through it), so the driver bound it using the client
    process's OS-local timezone instead of the configured `Timezone.default()`. Checked at the
    encoder level (no database, independent of the machine's own timezone): the produced bind
    value must be aware and interpreted in the configured timezone."""
    field = DateTimeRangeField()
    field.model_field_name = "span"
    naive = datetime.datetime(2026, 6, 1, 11, 0)

    with override_timezone(use_tz=True, timezone="Asia/Tokyo"):
        encoded = PostgresqlValueEncoders.encode_range_or_element(naive, None, field, POSTGRESQL_DIALECT)  # type: ignore[arg-type]

    assert isinstance(encoded, Cast)
    assert encoded.as_type == "timestamptz"
    bound = encoded.args[0].value
    assert bound.tzinfo is not None
    assert bound.utcoffset() == datetime.timedelta(hours=9)
    assert bound == datetime.datetime(2026, 6, 1, 2, 0, tzinfo=datetime.UTC)


def test_range_or_element_encoder_keeps_an_aware_datetime_scalar_unchanged():
    field = DateTimeRangeField()
    field.model_field_name = "span"
    aware = datetime.datetime(2026, 6, 1, 11, 0, tzinfo=datetime.UTC)

    encoded = PostgresqlValueEncoders.encode_range_or_element(aware, None, field, POSTGRESQL_DIALECT)  # type: ignore[arg-type]

    assert isinstance(encoded, Cast)
    assert encoded.args[0].value == aware
    assert encoded.args[0].value.tzinfo is not None


@pytest.mark.parametrize(
    "field_cls, scalar, expected_scalar, expected_type",
    [
        (IntRangeField, 5, 5, "integer"),
        (BigIntRangeField, 5, 5, "bigint"),
        (DecimalRangeField, Decimal("1.5"), Decimal("1.5"), "numeric"),
        (DateRangeField, datetime.date(2024, 1, 5), datetime.date(2024, 1, 5), "date"),
    ],
)
def test_range_or_element_encoder_keeps_other_range_scalars_as_they_were(
    field_cls, scalar, expected_scalar, expected_type
):
    field = field_cls()
    field.model_field_name = "span"

    encoded = PostgresqlValueEncoders.encode_range_or_element(scalar, None, field, POSTGRESQL_DIALECT)  # type: ignore[arg-type]

    assert isinstance(encoded, Cast)
    assert encoded.as_type == expected_type
    assert encoded.args[0].value == expected_scalar


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_datetime_range_contains_naive_scalar_uses_configured_timezone_not_os_local(db_ranges):
    """Live check of the encoder fix above: with the configured timezone set to Asia/Tokyo (UTC+9),
    a naive `11:00` means 02:00 UTC. The stored range is [01:00, 03:00) UTC, so it's inside; a naive
    `14:00` (05:00 UTC) is outside. Before the fix the result depended on the client OS timezone
    (a false negative for the first and a false positive for the second on a UTC+3 machine)."""
    entry = await DateTimeRangeThing.objects.create(
        span=Range(
            lower=datetime.datetime(2026, 6, 1, 1, 0, tzinfo=datetime.UTC),
            upper=datetime.datetime(2026, 6, 1, 3, 0, tzinfo=datetime.UTC),
        )
    )

    with override_timezone(use_tz=True, timezone="Asia/Tokyo"):
        assert await DateTimeRangeThing.objects.filter(span__contains=datetime.datetime(2026, 6, 1, 11, 0)) == [entry]
        assert await DateTimeRangeThing.objects.filter(span__contains=datetime.datetime(2026, 6, 1, 14, 0)) == []

    aware_inside = datetime.datetime(2026, 6, 1, 2, 0, tzinfo=datetime.UTC)
    assert await DateTimeRangeThing.objects.filter(span__contains=aware_inside) == [entry]


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_other_range_types_scalar_contains_still_works(db_ranges):
    decimal_entry = await DecimalRangeThing.objects.create(span=Range(lower=Decimal("1.5"), upper=Decimal("10.5")))
    date_entry = await DateRangeThing.objects.create(
        span=Range(lower=datetime.date(2024, 1, 1), upper=datetime.date(2024, 1, 10))
    )

    assert await DecimalRangeThing.objects.filter(span__contains=Decimal("5")) == [decimal_entry]
    assert await DecimalRangeThing.objects.filter(span__contains=Decimal("11")) == []
    assert await DateRangeThing.objects.filter(span__contains=datetime.date(2024, 1, 5)) == [date_entry]
    assert await DateRangeThing.objects.filter(span__contains=datetime.date(2024, 2, 5)) == []


def test_assigned_discrete_range_is_canonicalized_like_postgres_returns_it():
    """A freshly assigned range holds what a read returns: coerced bounds, [) for discrete
    ranges, exclusive unbounded sides and the empty range for no values."""
    int_field = IntRangeField()
    int_field.model_field_name = "span"
    date_field = DateRangeField()
    date_field.model_field_name = "span"

    assert int_field.to_python(Range(lower=1, upper=5, lower_inc=False, upper_inc=True)) == Range(lower=2, upper=6)
    assert int_field.to_python(("1", "5")) == Range(lower=1, upper=5)
    assert int_field.to_python(Range(lower=3, upper=3)).is_empty
    assert int_field.to_python(Range(lower=None, upper=5)) == Range(lower=None, upper=5, lower_inc=False)
    assert date_field.to_python(("2024-01-01", "2024-01-10")) == Range(
        lower=datetime.date(2024, 1, 1), upper=datetime.date(2024, 1, 10)
    )


def test_assigned_datetime_range_bounds_become_aware():
    field = DateTimeRangeField()
    field.model_field_name = "span"

    with override_timezone(use_tz=True, timezone="UTC"):
        assigned = field.to_python(("2024-01-01T10:00:00", "2024-01-02T10:00:00"))

    assert assigned.lower == datetime.datetime(2024, 1, 1, 10, 0, tzinfo=datetime.UTC)
    assert assigned.upper == datetime.datetime(2024, 1, 2, 10, 0, tzinfo=datetime.UTC)


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_created_range_in_memory_matches_the_stored_one(db_ranges):
    int_entry = await RangeThing.objects.create(span=Range(lower=1, upper=5, lower_inc=False, upper_inc=True))
    date_entry = await DateRangeThing.objects.create(span=("2024-01-01", "2024-01-10"))
    empty_entry = await RangeThing.objects.create(span=(4, 4))

    assert (await RangeThing.objects.get(id=int_entry.id)).span == int_entry.span
    assert (await DateRangeThing.objects.get(id=date_entry.id)).span == date_entry.span
    assert (await RangeThing.objects.get(id=empty_entry.id)).span.is_empty
    assert empty_entry.span.is_empty


@pytest.mark.asyncio
async def test_infinite_bounds_read_the_same_on_both_drivers_and_write_back_as_infinity(db_ranges):
    """rust_pg wrote a decoded infinity bound back as a finite date, and asyncpg read a tstzrange
    infinity bound naive - both drivers now read it UTC-aware and write it back as infinity."""
    client = DateTimeRangeThing._meta.db
    await client.execute_script(
        "INSERT INTO datetimerangething (id, span) VALUES (1, '[-infinity,infinity)'), "
        "(2, '[-infinity,\"2026-01-01 00:00+00\")');"
        "INSERT INTO daterangething (id, span) VALUES (1, '[2026-01-01,infinity)')"
    )
    utc_min = datetime.datetime.min.replace(tzinfo=datetime.UTC)
    utc_max = datetime.datetime.max.replace(tzinfo=datetime.UTC)

    with override_timezone(use_tz=True, timezone="Asia/Tokyo"):
        unbounded = await DateTimeRangeThing.objects.get(id=1)
        half_bounded = await DateTimeRangeThing.objects.get(id=2)
        dates = await DateRangeThing.objects.get(id=1)
        assert unbounded.span == Range(utc_min, utc_max)
        assert half_bounded.span.lower == utc_min
        assert half_bounded.span.lower.tzinfo is not None
        assert dates.span == Range(datetime.date(2026, 1, 1), datetime.date.max)

        await DateTimeRangeThing.objects.filter(id=1).update(span=unbounded.span)
        await DateTimeRangeThing.objects.filter(id=2).update(span=half_bounded.span)
        await DateRangeThing.objects.filter(id=1).update(span=dates.span)
        await DateTimeRangeThing.objects.create(id=3, span=Range(datetime.datetime.min, datetime.datetime.max))

    rows = await client.execute_dicts("SELECT id, span::text AS span FROM datetimerangething ORDER BY id")
    assert rows == [
        {"id": 1, "span": "[-infinity,infinity)"},
        {"id": 2, "span": '[-infinity,"2026-01-01 00:00:00+00")'},
        {"id": 3, "span": "[-infinity,infinity)"},
    ]
    date_rows = await client.execute_dicts("SELECT span::text AS span FROM daterangething")
    assert date_rows == [{"span": "[2026-01-01,infinity)"}]


def test_datetime_range_bounds_follow_datetime_field_conversion():
    """Bounds are read like a DatetimeField value: in the configured zone under use_tz=True,
    as naive system-local wall-clock time otherwise."""
    field = DateTimeRangeField()
    field.model_field_name = "span"
    utc_lower = datetime.datetime(2024, 6, 1, 13, 0, tzinfo=datetime.UTC)
    utc_upper = datetime.datetime(2024, 6, 1, 15, 0, tzinfo=datetime.UTC)

    with override_timezone(use_tz=True, timezone="America/New_York"):
        read = field.from_db_value(Range(utc_lower, utc_upper))
        assert read.lower == utc_lower
        assert read.lower.utcoffset() == datetime.timedelta(hours=-4)
        assert getattr(read.lower.tzinfo, "key", None) == "America/New_York"

    with override_timezone(use_tz=False):
        read = field.from_db_value(Range(utc_lower, utc_upper))
        assert read.lower.tzinfo is None
        assert read.lower == Timezone.get_system_local_naive(utc_lower)
        assigned = field.to_python(Range(datetime.datetime(2024, 6, 1, 9, 0), None))
        assert assigned.lower == datetime.datetime(2024, 6, 1, 9, 0)


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_datetime_range_naive_bounds_mean_what_a_naive_datetime_field_value_means(db_ranges):
    """Under use_tz=False a naive bound was stored as UTC while a naive DatetimeField value is
    stored as system-local time - the same wall clock landed on two different instants."""
    naive_lower = datetime.datetime(2024, 6, 1, 9, 0)
    naive_upper = datetime.datetime(2024, 6, 1, 11, 0)
    with override_timezone(use_tz=False):
        entry = await DateTimeRangeThing.objects.create(span=Range(naive_lower, naive_upper))
        read = await DateTimeRangeThing.objects.get(id=entry.id)

        assert read.span == Range(naive_lower, naive_upper)
        assert read.span.lower.tzinfo is None
        assert (
            await DateTimeRangeThing.objects.filter(span__contains=datetime.datetime(2024, 6, 1, 10, 0)).count() == 1
        )
        assert (
            await DateTimeRangeThing.objects.filter(span__contains=datetime.datetime(2024, 6, 1, 12, 0)).count() == 0
        )

    with override_timezone(use_tz=True, timezone="America/New_York"):
        entry = await DateTimeRangeThing.objects.create(span=Range(naive_lower, naive_upper))
        read = await DateTimeRangeThing.objects.get(id=entry.id)
        assert read.span.lower == datetime.datetime(2024, 6, 1, 13, 0, tzinfo=datetime.UTC)
        assert getattr(read.span.lower.tzinfo, "key", None) == "America/New_York"
