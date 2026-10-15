"""Field-by-field parity check between the two BaseExecutor.execute_select() hydration paths:
the compiled pure-Python row reader (ModelColumns) and the experimental Rust accelerator
(rust/hydrate/, wired in via BaseExecutor.hydrate). Covers every FieldBucket - NATIVE is
already exercised by the rest of the suite via the plain hydration path, so this module targets
DEFAULT (BooleanField, the only real DEFAULT-bucket field in tests/testmodels.py) and every
hydrate-internal fast-path bucket at the FieldBucket.COMPLEX level (Decimal/UUID/JSON/enum,
plus DatetimeField/DateField/TimeField/TimeDeltaField - the last four each get their own further
Rust-side bucket, 3/8/9/10 respectively, not the generic bucket-2 fallback; DateField in
particular lands there only on SQLite - Postgres already gets FieldBucket.NATIVE for it, see
HydrateAccelerator.get_plan_for_zone()), including a null-valued row for every
nullable field to exercise both branches of _init_from_db_positional's per-bucket conversion.

Skipped entirely when rust/hydrate/ hasn't been built locally (`maturin develop --release` -
see hare/backends/base/executor.py's own hydrate import for how that's detected); CI does
not build it, so this only runs when someone opts in.
"""

import datetime
import uuid
from decimal import Decimal

import pytest

from hare import fields
from hare.contrib import test
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ValidationError
from hare.fields.data.choices import CharEnumFieldInstance, IntEnumFieldInstance
from hare.models import FieldBucket
from hare.query.rows.enums import ReadCodecType
from hare.query.rows.native.field_codecs import FieldCodecs
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.time import Timezone
from tests.testmodels import (
    BooleanFields,
    Currency,
    DateFields,
    DatetimeFields,
    DecimalFields,
    EnumFields,
    JSONFields,
    Service,
    TimeDeltaFields,
    TimeFields,
    UUIDFields,
)
from tests.utils.model_rows_queries import get_model_rows_query
from tests.utils.row_hydration import RowHydration
from tests.utils.timezone_context import override_timezone

_hydrate_candidate = pytest.importorskip("rust.native.rows")
# rust/ has no __init__.py anywhere (a deliberate PEP 420 namespace package, see
# rust/hydrate/pyproject.toml's own module-name docs) - so rust/hydrate/ itself (the crate
# source directory, always present in a checkout) is ALSO a valid, empty namespace-package
# portion for "rust.hydrate". Confirmed live: with the compiled extension absent,
# `import rust.hydrate` resolves to that bare directory instead of raising ImportError, so
# importorskip alone never skips - only checking for the real extension's own attribute does.
if not hasattr(_hydrate_candidate, "ModelReader"):
    pytest.skip(
        "rust.hydrate is an empty namespace package (extension not built) - run "
        "`maturin develop --release --manifest-path rust/hydrate/Cargo.toml` to opt in",
        allow_module_level=True,
    )
hydrate = _hydrate_candidate


def _get_decode_plan(model, only_fields: list[str] | None = None):
    """Builds a real decode_plan for `model` from query shape alone - works on an empty table,
    since decode_plan only reflects the SELECT column list, not any actual row data. Restricting
    to `only_fields` (always include the pk) keeps a branch-coverage test from having to invent
    a plausible synthetic value for every other field on the model."""
    qs = model.objects.all()
    if only_fields:
        qs = qs.only(*only_fields)
    return RowHydration.get_decode_plan(qs)


def _synthetic_row(decode_plan, values_by_name: dict) -> tuple:
    """One row tuple in decode_plan's column order, sourcing each column's value from
    values_by_name (by model field name) - lets a test hand-craft exact raw values (an invalid
    enum int, malformed JSON text, a bool column stored as a plain int, a UUID column stored as
    a plain string) that a real driver round-trip may never actually produce, either because a
    given backend doesn't shape data that way or because the ORM's own write-path validation
    would reject it before it ever reached storage."""
    return tuple(values_by_name[name] for name, _field, _bucket, _reader in decode_plan)


def _hydrate_rust(model, decode_plan, is_partial: bool, rows: list[tuple]):
    types = model.get_connection().dialect.types
    return HydrateAccelerator.get_model_reader(
        model, decode_plan, is_partial, types, Timezone.get_aware_zone_name()
    ).read(list(rows))


def _hydrate_both(model, decode_plan, is_partial: bool, rows: list[tuple]):
    python_instances = [RowHydration.hydrate_in_python(model, row, decode_plan, is_partial) for row in rows]
    rust_instances = _hydrate_rust(model, decode_plan, is_partial, rows)
    return python_instances, rust_instances


async def _assert_rust_matches_python(model, create_kwargs_list: list[dict]) -> None:
    await model.objects.bulk_create([model(**kwargs) for kwargs in create_kwargs_list])

    compiler = get_model_rows_query(model.objects.all())
    compiler._make_query()
    sql, values = compiler.query.get_parameterized_sql()
    _, raw_rows = await compiler._connection.execute(sql, values)
    assert len(raw_rows) == len(create_kwargs_list)

    decode_plan = compiler._decode_plan
    assert decode_plan is not None, f"{model.__name__}.all() didn't produce a decode_plan"
    is_partial = compiler._decode_plan_is_partial
    plan_names = [name for name, _field, _bucket, _reader in decode_plan]

    python_instances = [RowHydration.hydrate_in_python(model, row, decode_plan, is_partial) for row in raw_rows]
    rust_instances = HydrateAccelerator.get_model_reader(
        model, decode_plan, is_partial, compiler._connection.dialect.types, Timezone.get_aware_zone_name()
    ).read(list(raw_rows))
    assert len(rust_instances) == len(python_instances)

    for python_instance, rust_instance in zip(python_instances, rust_instances, strict=True):
        for field_name in plan_names:
            python_value = getattr(python_instance, field_name)
            rust_value = getattr(rust_instance, field_name)
            assert rust_value == python_value, (
                f"{model.__name__}.{field_name}: rust hydration gave {rust_value!r}, "
                f"python hydration gave {python_value!r}"
            )
            assert type(rust_value) is type(python_value), (
                f"{model.__name__}.{field_name}: rust hydration gave a {type(rust_value).__name__}, "
                f"python hydration gave a {type(python_value).__name__}"
            )


@pytest.mark.asyncio
async def test_boolean_fields_default_bucket(db):
    """The only real FieldBucket.DEFAULT field in the test model matrix (bool isn't in
    DB_NATIVE and BooleanField doesn't override from_db_value)."""
    await _assert_rust_matches_python(
        BooleanFields,
        [
            {"boolean": True, "boolean_null": False},
            {"boolean": False, "boolean_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_decimal_fields_complex_bucket(db):
    await _assert_rust_matches_python(
        DecimalFields,
        [
            {"decimal": Decimal("1234.5678"), "decimal_nodec": Decimal("42"), "decimal_null": Decimal("0.0001")},
            {"decimal": Decimal("0.0000"), "decimal_nodec": Decimal("0"), "decimal_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_datetime_fields_complex_bucket(db):
    """datetime is deliberately not in DB_NATIVE-shortcut territory here - from_db_value is
    overridden (Timezone.localtime), so every configured Timezone (not just UTC) must match."""
    now = datetime.datetime(2024, 3, 15, 12, 30, 45, 123456, tzinfo=datetime.timezone.utc)
    await _assert_rust_matches_python(
        DatetimeFields,
        [
            {"datetime": now, "datetime_null": now},
            {"datetime": now, "datetime_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_timedelta_fields_bucket_10(db):
    """TimeDeltaField always lands on bucket 10 (never FieldBucket.NATIVE on either dialect -
    datetime.timedelta is never DB_NATIVE, the column is a plain BIGINT of microseconds)."""
    await _assert_rust_matches_python(
        TimeDeltaFields,
        [
            {
                "timedelta": datetime.timedelta(hours=1, minutes=2, seconds=3),
                "timedelta_null": datetime.timedelta(days=1),
            },
            {"timedelta": datetime.timedelta(0), "timedelta_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_date_fields_bucket_8_or_native(db):
    """DateField is FieldBucket.NATIVE on Postgres (datetime.date is in the base DB_NATIVE set)
    and rust_hydrate bucket 8 on SQLite (narrower DB_NATIVE, no datetime.date) - this same
    end-to-end comparison exercises whichever one the current dialect actually takes."""
    await _assert_rust_matches_python(
        DateFields,
        [
            {"date": datetime.date(2024, 3, 15), "date_null": datetime.date(2000, 1, 1)},
            {"date": datetime.date(1999, 12, 31), "date_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_time_fields_bucket_9(db):
    """TimeField always lands on rust_hydrate bucket 9 (datetime.time is in neither dialect's
    DB_NATIVE)."""
    await _assert_rust_matches_python(
        TimeFields,
        [
            {"time": datetime.time(12, 30, 45), "time_null": datetime.time(0, 0, 0)},
            {"time": datetime.time(23, 59, 59), "time_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_json_fields_complex_bucket(db):
    await _assert_rust_matches_python(
        JSONFields,
        [
            {"data": {"a": 1, "b": [1, 2, 3]}, "data_null": {"x": "y"}},
            {"data": [1, 2, 3], "data_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_uuid_fields_complex_bucket(db):
    await _assert_rust_matches_python(
        UUIDFields,
        [
            {"data": uuid.uuid4(), "data_null": uuid.uuid4()},
            {"data": uuid.uuid4(), "data_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_enum_fields_complex_bucket(db):
    await _assert_rust_matches_python(
        EnumFields,
        [
            {"service": Service.python_programming, "currency": Currency.EUR},
            {"service": Service.system_administration, "currency": Currency.USD},
        ],
    )


# ============================================================================
# Explicit branch coverage below - the tests above compare Rust vs Python end to end on
# whatever shape a real DB round-trip happens to produce; these instead hand-craft raw values
# (via _synthetic_row(), bypassing the DB entirely) to deterministically exercise a SPECIFIC
# branch inside each fast path - including ones a real driver round-trip may never hit on its
# own (an invalid enum int, malformed JSON text, a bool column stored as a plain 0/1 int) or
# that depend on runtime config a plain read wouldn't vary (use_timezone, a non-UTC Timezone).
# ============================================================================


@pytest.mark.asyncio
@test.requires_features(dialect="sqlite")  # bool is in postgres's own DB_NATIVE, only sqlite hits bucket 1 for it
async def test_boolean_default_bucket_skip_and_fallback_branches(db):
    """Bucket 1 (DEFAULT) skips field_type()/bool() when the raw value already isinstance-
    matches and calls it otherwise. Postgres's DB_NATIVE includes bool (asyncpg always gives a
    real bool for a native BOOLEAN column), so BooleanField is bucket 0 (NATIVE, no conversion
    at all) there - only sqlite (no BOOLEAN type at all, a boolean column is stored and read
    back as a plain 0/1 INTEGER) actually classifies it as bucket 1, so only sqlite exercises
    the branches this test is about."""
    decode_plan, is_partial = _get_decode_plan(BooleanFields, only_fields=["id", "boolean", "boolean_null"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "boolean": True, "boolean_null": False}),  # already bool
        _synthetic_row(decode_plan, {"id": 2, "boolean": 1, "boolean_null": 0}),  # int - needs bool()
        _synthetic_row(decode_plan, {"id": 3, "boolean": 0, "boolean_null": None}),
    ]
    python_instances, rust_instances = _hydrate_both(BooleanFields, decode_plan, is_partial, rows)
    for python_instance, rust_instance in zip(python_instances, rust_instances, strict=True):
        assert rust_instance.boolean == python_instance.boolean
        assert type(rust_instance.boolean) is bool
        assert rust_instance.boolean_null == python_instance.boolean_null


@pytest.mark.asyncio
async def test_enum_invalid_value_raises_same_error(db):
    """A raw int with no matching enum member (the map-lookup-miss branch) must fall back to
    field.from_db_value() and raise the exact same error Python's own EnumClass(value) would -
    the ORM's own write-path validation means this can never come from a normal .create() +
    round-trip, only from data written some other way (a migration, a raw SQL insert, another
    process/version writing a since-removed enum member)."""
    decode_plan, is_partial = _get_decode_plan(EnumFields, only_fields=["id", "service", "currency"])
    row = _synthetic_row(decode_plan, {"id": 1, "service": 9999, "currency": "EUR"})

    with pytest.raises(ValidationError) as python_exc_info:
        RowHydration.hydrate_in_python(EnumFields, row, decode_plan, is_partial)
    with pytest.raises(ValidationError) as rust_exc_info:
        _hydrate_rust(EnumFields, decode_plan, is_partial, [row])

    assert str(rust_exc_info.value) == str(python_exc_info.value)


@test.requires_features(dialect="sqlite")  # Decimal is in postgres's own DB_NATIVE, bucket 6 never fires there
@pytest.mark.asyncio
async def test_decimal_invalid_value_raises_same_error(db):
    """A raw value that isn't a valid Decimal literal (garbage written outside the ORM) must fall
    back to field.from_db_value() and raise the exact same ValidationError on both sides - the
    Rust fast path used to call Decimal(value).quantize(...) unconditionally and let the raw
    decimal.InvalidOperation through instead of falling back like the enum/UUID buckets already
    did."""
    decode_plan, is_partial = _get_decode_plan(DecimalFields, only_fields=["id", "decimal", "decimal_nodec"])
    row = _synthetic_row(decode_plan, {"id": 1, "decimal": "not-a-decimal", "decimal_nodec": Decimal("1")})

    with pytest.raises(ValidationError) as python_exc_info:
        RowHydration.hydrate_in_python(DecimalFields, row, decode_plan, is_partial)
    with pytest.raises(ValidationError) as rust_exc_info:
        _hydrate_rust(DecimalFields, decode_plan, is_partial, [row])

    assert str(rust_exc_info.value) == str(python_exc_info.value)


@pytest.mark.asyncio
async def test_json_already_object_passthrough_branch(db):
    """A raw value that's already a dict/list (not str/bytes) exercises JSONField's passthrough
    branch - the decoder is never called at all, on either side."""
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": {"already": "decoded", "n": [1, 2, 3]}})
    python_instances, rust_instances = _hydrate_both(JSONFields, decode_plan, is_partial, [row])
    assert rust_instances[0].data == python_instances[0].data == {"already": "decoded", "n": [1, 2, 3]}


@pytest.mark.asyncio
async def test_json_invalid_value_raises_same_field_error(db):
    """Malformed JSON text must raise the exact same ValidationError JSONField.from_db_value
    itself raises, message included - rust_hydrate replicates that wrapping itself rather than
    letting the raw decoder's own exception type/message leak through unwrapped."""
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": "{not valid json"})

    with pytest.raises(ValidationError) as python_exc_info:
        RowHydration.hydrate_in_python(JSONFields, row, decode_plan, is_partial)
    with pytest.raises(ValidationError) as rust_exc_info:
        _hydrate_rust(JSONFields, decode_plan, is_partial, [row])

    assert str(rust_exc_info.value) == str(python_exc_info.value)


@pytest.mark.asyncio
async def test_json_invalid_value_preserves_context_chain(db):
    """Malformed JSON text must also chain the original decode error as __context__ - Python's
    own `raise ValidationError(...) from exc` inside `except Exception as exc:` does this, so a
    traceback shows "The above exception was the direct cause of the following exception"
    pointing at the real decode error, not just the wrapping ValidationError alone."""
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": "{not valid json"})

    with pytest.raises(ValidationError) as rust_exc_info:
        _hydrate_rust(JSONFields, decode_plan, is_partial, [row])

    assert rust_exc_info.value.__cause__ is not None


@pytest.mark.asyncio
async def test_json_invalid_utf8_bytes_raises_same_field_error(db):
    """Bytes that are both invalid JSON AND invalid utf-8 used to mask the intended
    ValidationError with a bare UnicodeDecodeError from the error message's own .decode() call,
    on both the Python and Rust sides - each falls back to repr(value) when that decode itself
    fails."""
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": b"\xff\xfe not valid utf-8 or json"})

    with pytest.raises(ValidationError) as python_exc_info:
        RowHydration.hydrate_in_python(JSONFields, row, decode_plan, is_partial)
    with pytest.raises(ValidationError) as rust_exc_info:
        _hydrate_rust(JSONFields, decode_plan, is_partial, [row])

    assert str(rust_exc_info.value) == str(python_exc_info.value)


@pytest.mark.asyncio
async def test_uuid_string_fallback_branch(db):
    """A raw string value (sqlite's actual on-disk shape for a UUID column - there's no native
    UUID type, it's stored and read back as TEXT) exercises UUIDField.from_db_value's
    UUID(value) fallback, not the isinstance-already-UUID no-op asyncpg's native uuid columns
    hit instead."""
    decode_plan, is_partial = _get_decode_plan(UUIDFields, only_fields=["id", "data"])
    value = uuid.uuid4()
    row = _synthetic_row(decode_plan, {"id": uuid.uuid4(), "data": str(value)})
    python_instances, rust_instances = _hydrate_both(UUIDFields, decode_plan, is_partial, [row])
    assert rust_instances[0].data == python_instances[0].data == value
    assert type(rust_instances[0].data) is uuid.UUID


@pytest.mark.asyncio
async def test_datetime_naive_raw_value_with_use_tz(db):
    """A naive datetime.datetime raw value under use_timezone=True (the default) exercises
    DatetimeField.from_db_value's make_aware branch (`replace(tzinfo=tz)`), not the
    aware/astimezone one - and never the zero-call fast path either, which only ever applies to
    an already-aware value."""
    decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
    naive = datetime.datetime(2024, 6, 15, 9, 30, 0)
    row = _synthetic_row(decode_plan, {"id": 1, "datetime": naive})
    python_instances, rust_instances = _hydrate_both(DatetimeFields, decode_plan, is_partial, [row])
    assert rust_instances[0].datetime == python_instances[0].datetime
    assert rust_instances[0].datetime.tzinfo is not None


@pytest.mark.asyncio
async def test_datetime_aware_raw_value_without_use_tz(db):
    """An aware datetime.datetime raw value under use_timezone=False exercises the
    astimezone().replace(tzinfo=None) strip-to-naive branch - the one other real per-row call
    path besides the zero-call fast path and the plain astimezone(tz) call."""
    with override_timezone(use_timezone=False):
        decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
        aware = datetime.datetime(2024, 6, 15, 9, 30, 0, tzinfo=datetime.timezone.utc)
        row = _synthetic_row(decode_plan, {"id": 1, "datetime": aware})
        python_instances, rust_instances = _hydrate_both(DatetimeFields, decode_plan, is_partial, [row])
        assert rust_instances[0].datetime == python_instances[0].datetime
        assert rust_instances[0].datetime.tzinfo is None


@pytest.mark.asyncio
async def test_datetime_non_utc_target_dst_transition_not_zero_call(db):
    """Regression guard for the riskiest fast path in this whole module: the zero-Python-call
    datetime shortcut must NEVER fire for a non-UTC target, since a DST-observing zone's offset
    genuinely varies by date. Picks two real UTC instants either side of an actual US DST
    transition (2024-03-10, America/New_York) - if the shortcut wrongly fired here, both would
    come back with the SAME wall-clock offset instead of the real 1-hour jump, since the
    shortcut only ever copies fields without recomputing anything."""
    with override_timezone(use_timezone=True, timezone="America/New_York"):
        decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
        before_transition = datetime.datetime(2024, 3, 10, 6, 0, 0, tzinfo=datetime.timezone.utc)  # EST, UTC-5
        after_transition = datetime.datetime(2024, 3, 10, 8, 0, 0, tzinfo=datetime.timezone.utc)  # EDT, UTC-4
        rows = [
            _synthetic_row(decode_plan, {"id": 1, "datetime": before_transition}),
            _synthetic_row(decode_plan, {"id": 2, "datetime": after_transition}),
        ]
        python_instances, rust_instances = _hydrate_both(DatetimeFields, decode_plan, is_partial, rows)
        for python_instance, rust_instance in zip(python_instances, rust_instances, strict=True):
            assert rust_instance.datetime == python_instance.datetime
            assert rust_instance.datetime.utcoffset() == python_instance.datetime.utcoffset()
        # Sanity-check the test data itself, not just Rust/Python parity - if this ever failed,
        # the two chosen instants wouldn't actually straddle a real DST transition any more.
        assert python_instances[0].datetime.utcoffset() != python_instances[1].datetime.utcoffset()


@test.requires_features(dialect="sqlite")  # postgresql reads DatetimeField through its own reader - no bucket 3
@pytest.mark.asyncio
async def test_plan_cache_reflects_timezone_config_change(db):
    """The accelerator's hydrate plan is cached, but keyed on the live Timezone
    config at call time specifically so a runtime config change produces a fresh plan instead of
    silently reusing a use_timezone/tz baked in before the change - the exact staleness bug the cache
    design was checked against before being added."""
    decode_plan, _is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])

    types = DatetimeFields.get_connection().dialect.types
    default_reader = HydrateAccelerator.get_model_reader(
        DatetimeFields, decode_plan, _is_partial, types, Timezone.get_aware_zone_name()
    )
    assert Timezone.get_use_timezone() is True  # USE_TZ defaults to true

    with override_timezone(use_timezone=False):
        changed_reader = HydrateAccelerator.get_model_reader(
            DatetimeFields, decode_plan, _is_partial, types, Timezone.get_aware_zone_name()
        )
        assert changed_reader is not default_reader, "stale reader after a Timezone config change"


@pytest.mark.asyncio
@test.requires_features(dialect="sqlite")  # DateField is FieldBucket.NATIVE on postgres - bucket 8 unreachable there
async def test_date_string_parse_and_native_passthrough_branches(db):
    """Bucket 8 only exists on sqlite (see test_date_fields_bucket_8_or_native) - its two raw
    value shapes: an ISO string (needs parse_datetime(value).date(), sqlite's actual on-disk
    shape for a DATE column) and an already-native datetime.date (the isinstance passthrough
    DateField.from_db_value's own `isinstance(value, datetime.date)` check would also take -
    never produced by a real sqlite round-trip, but from_db_value still handles it, so
    rust_hydrate must match it too)."""
    decode_plan, is_partial = _get_decode_plan(DateFields, only_fields=["id", "date"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "date": "2024-03-15"}),
        _synthetic_row(decode_plan, {"id": 2, "date": datetime.date(2000, 1, 1)}),
    ]
    python_instances, rust_instances = _hydrate_both(DateFields, decode_plan, is_partial, rows)
    for python_instance, rust_instance in zip(python_instances, rust_instances, strict=True):
        assert rust_instance.date == python_instance.date
        assert type(rust_instance.date) is datetime.date


@pytest.mark.asyncio
async def test_time_tzinfo_dispatch_branches(db):
    """Bucket 9's tzinfo dispatch has only two ACTIVE branches (unlike DatetimeField's four) -
    TimeField.from_db_value never converts a wall-clock value between zones, it only ever
    attaches or strips tzinfo: naive+use_timezone -> attach the default zone; aware+not-use_timezone ->
    strip it. The other two combinations (aware+use_timezone, naive+not-use_timezone) are no-ops - covered
    here too, so a wrongly "helpful" rust_hydrate that mutates them anyway would be caught."""
    decode_plan, is_partial = _get_decode_plan(TimeFields, only_fields=["id", "time"])
    naive = datetime.time(9, 30, 0)
    aware = datetime.time(9, 30, 0, tzinfo=datetime.timezone.utc)

    # use_timezone=True (default): naive gets a tzinfo attached, already-aware is left untouched.
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "time": naive}),
        _synthetic_row(decode_plan, {"id": 2, "time": aware}),
    ]
    python_instances, rust_instances = _hydrate_both(TimeFields, decode_plan, is_partial, rows)
    assert rust_instances[0].time == python_instances[0].time
    assert rust_instances[0].time.tzinfo is not None  # naive -> attached
    assert rust_instances[1].time == python_instances[1].time
    assert rust_instances[1].time.tzinfo is not None  # already aware -> untouched, still aware

    # use_timezone=False: aware gets its tzinfo stripped, already-naive is left untouched.
    with override_timezone(use_timezone=False):
        decode_plan, is_partial = _get_decode_plan(TimeFields, only_fields=["id", "time"])
        rows = [
            _synthetic_row(decode_plan, {"id": 1, "time": naive}),
            _synthetic_row(decode_plan, {"id": 2, "time": aware}),
        ]
        python_instances, rust_instances = _hydrate_both(TimeFields, decode_plan, is_partial, rows)
        assert rust_instances[0].time == python_instances[0].time
        assert rust_instances[0].time.tzinfo is None  # already naive -> untouched, still naive
        assert rust_instances[1].time == python_instances[1].time
        assert rust_instances[1].time.tzinfo is None  # aware -> stripped


@pytest.mark.asyncio
async def test_time_timedelta_and_string_fallback_branches(db):
    """A raw datetime.timedelta value (TimeField.from_db_value's own early-return branch for a
    legacy interval-style TIME shape) and a raw ISO time string (needs datetime.time.
    fromisoformat - deliberately left to the Python fallback rather than reimplemented in Rust,
    see hydrate_time_value's own doc comment) both bypass the tzinfo dispatch entirely."""
    decode_plan, is_partial = _get_decode_plan(TimeFields, only_fields=["id", "time"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "time": datetime.timedelta(hours=25)}),
        _synthetic_row(decode_plan, {"id": 2, "time": "14:30:00"}),
    ]
    python_instances, rust_instances = _hydrate_both(TimeFields, decode_plan, is_partial, rows)
    assert rust_instances[0].time == python_instances[0].time == datetime.timedelta(hours=25)
    assert rust_instances[1].time == python_instances[1].time


@pytest.mark.asyncio
async def test_timedelta_zero_negative_and_multiday_branches(db):
    """Bucket 10 always fires for TimeDeltaField (never FieldBucket.NATIVE) - covers the raw int
    microseconds shape across a zero value, a negative one, and one spanning multiple days
    (large enough to need day/second/microsecond normalization, which rust_hydrate leaves to the
    real datetime.timedelta constructor rather than reimplementing - see hydrate_timedelta_
    value's own doc comment), plus the already-timedelta passthrough branch."""
    decode_plan, is_partial = _get_decode_plan(TimeDeltaFields, only_fields=["id", "timedelta"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "timedelta": 0}),
        _synthetic_row(decode_plan, {"id": 2, "timedelta": -1_500_000}),
        _synthetic_row(decode_plan, {"id": 3, "timedelta": 3 * 24 * 60 * 60 * 1_000_000 + 123_456}),
        _synthetic_row(decode_plan, {"id": 4, "timedelta": datetime.timedelta(minutes=5)}),
    ]
    python_instances, rust_instances = _hydrate_both(TimeDeltaFields, decode_plan, is_partial, rows)
    for python_instance, rust_instance in zip(python_instances, rust_instances, strict=True):
        assert rust_instance.timedelta == python_instance.timedelta
        assert type(rust_instance.timedelta) is datetime.timedelta


@pytest.mark.parametrize("bad_model_cls", [None, 123, 3.14, [], {}, object(), "not a type object"])
def test_hydrate_rows_rejects_non_type_model_cls(bad_model_cls):
    """hydrate_rows casts model_cls straight to *mut PyTypeObject and hands it to
    PyType_GenericAlloc, which reads tp_basicsize/tp_alloc off of it - undefined behavior for
    any object that isn't actually a type. Confirmed live (before this fix) that this crashed
    the process outright for some object shapes (an empty list) and raised a bogus MemoryError
    for others (str/int/dict/None), depending on what garbage tp_basicsize happened to read as.
    hydrate_rows is a public PyO3 function, reachable from any Python code, not just hare's own
    trusted call site (BaseExecutor.execute_select(), which always passes a real Model
    subclass) - this is the same "not the crate's only caller" reasoning already applied
    elsewhere in this crate."""
    with pytest.raises(TypeError):
        hydrate.ModelReader(bad_model_cls, [], False, False)


@pytest.mark.parametrize("bad_model_cls", [range, slice, memoryview, dict, set, frozenset, type])
def test_hydrate_rows_rejects_non_heap_type_model_cls(bad_model_cls):
    """is_instance_of::<PyType>() alone doesn't catch every unsafe model_cls: range, slice,
    memoryview, dict, set, frozenset and type are all real Python types (so they pass that
    check), but they're static/built-in C types with no heap-type layout - confirmed live
    (before this fix) that range/slice/memoryview crashed the process outright when handed to
    PyType_GenericAlloc, which blindly zero-allocates based on tp_basicsize/tp_alloc without
    setting up their internal C state. The Py_TPFLAGS_HEAPTYPE check rejects all of them while
    still accepting any ordinary Python-defined class, including a Model subclass."""
    with pytest.raises(TypeError, match="is not a class defined in Python"):
        hydrate.ModelReader(bad_model_cls, [], False, False).read([()])


# ============================================================================
# Round-3 finding: every isinstance-gated fast-path bucket above (3/4/5/6/7/8/9/10) is a
# bit-for-bit native reimplementation of ITS OWN base field class's from_db_value - a further
# subclass overriding from_db_value with genuinely different logic (encryption, a custom
# encoding, ...) used to still get routed into that SAME native fast path (isinstance() alone
# decided it), silently running the BASE class's conversion instead of the subclass's own - the
# override was only ever consulted as a fallback for a raw-value SHAPE the fast path itself
# doesn't recognize, never for the common, successfully-fast-pathed case. Confirmed via
# EncryptedJSONField (a JSONField subclass in a downstream service) round-tripping ciphertext
# unmodified on read instead of decrypting it.
# ============================================================================


def _overridden_field_bucket_code(field) -> ReadCodecType:
    entry = (field.model_field_name, field, FieldBucket.COMPLEX, None)
    codec_type, options = FieldCodecs.get_read_specification(entry, SQL_DIALECT.types, None)
    assert options["reader"] == field.from_db_value, "an overridden field must always carry its OWN from_db_value"
    return codec_type


def test_overridden_datetime_field_falls_back_to_complex_bucket():
    class CustomDatetimeField(fields.DatetimeField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomDatetimeField()
    field.model_field_name = "dt"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_uuid_field_falls_back_to_complex_bucket():
    class CustomUUIDField(fields.UUIDField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomUUIDField()
    field.model_field_name = "u"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_int_enum_field_falls_back_to_complex_bucket():
    class CustomIntEnumField(IntEnumFieldInstance):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomIntEnumField(Service)
    field.model_field_name = "svc"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_char_enum_field_falls_back_to_complex_bucket():
    class CustomCharEnumField(CharEnumFieldInstance):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomCharEnumField(Currency)
    field.model_field_name = "cur"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_decimal_field_falls_back_to_complex_bucket():
    class CustomDecimalField(fields.DecimalField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomDecimalField(max_digits=10, decimal_places=2)
    field.model_field_name = "dec"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_json_field_falls_back_to_complex_bucket():
    """The exact reported scenario (EncryptedJSONField) - bucket 7 called field.decoder
    directly (not even field.from_db_value), so an override was never consulted at all,
    not even as a fallback."""

    class CustomJSONField(fields.JSONField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomJSONField()
    field.model_field_name = "j"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_date_field_falls_back_to_complex_bucket():
    class CustomDateField(fields.DateField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomDateField()
    field.model_field_name = "d"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_time_field_falls_back_to_complex_bucket():
    class CustomTimeField(fields.TimeField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomTimeField()
    field.model_field_name = "t"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL


def test_overridden_timedelta_field_falls_back_to_complex_bucket():
    class CustomTimeDeltaField(fields.TimeDeltaField):
        def from_db_value(self, value):
            return super().from_db_value(value)

    field = CustomTimeDeltaField()
    field.model_field_name = "td"
    assert _overridden_field_bucket_code(field) == ReadCodecType.CALL
