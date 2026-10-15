"""Field-by-field parity check between the two write-side serialization paths:
BulkCreateQuery._execute_many()'s pure-Python `field.to_db_value(getattr(instance, name),
instance)` loop and the Rust accelerator, wired in via BulkWriteBatches._build_rust_serialize_plan.
Mirrors the read-side hydration parity test's own structure and conventions exactly - same skip
mechanism, same value+type diffing, same error-message-parity convention for failure cases.

Covers buckets 0 (NATIVE_INLINED)/1 (NATIVE_REAL_VALIDATE)/2 (COMPLEX fallback)/3 (DateField)/4
(UUIDField)/5 (Enum)/6 (JSONField) - the full bucket set. Every remaining field type not covered
by a dedicated bucket (auto_now Datetime/Time) lands on bucket 2, which calls the real
field.to_db_value() unconditionally - covered here anyway, since bucket 2's OWN correctness (not
skipping instance-mutation side effects, threading the instance argument through correctly)
still needs verification even though it does no inline logic itself.

Skipped entirely when the Rust extension hasn't been built locally.
"""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from typing import Any

import pytest

from hare.core.caching.caches import Caches
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ValidationError
from hare.fields.data.choices import CharEnumFieldInstance, IntEnumFieldInstance
from hare.fields.data.json import JSONField
from hare.fields.data.temporal import DateField
from hare.fields.data.text import CharField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field
from hare.fields.validators import MaxValueValidator, MinValueValidator, Validator
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.rows.enums import WriteCodecType
from hare.query.rows.native.field_codecs import FieldCodecs
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from hare.time import Timezone
from tests.testmodels import (
    BooleanFields,
    CharFields,
    Currency,
    DateFields,
    DatetimeFields,
    DecimalFields,
    EnumFields,
    FloatFields,
    IntFields,
    JSONFields,
    Service,
    TextFields,
    TimeDeltaFields,
    UUIDFields,
)

_hydrate_candidate = pytest.importorskip("rust.native.rows")
# rust/ has no __init__.py anywhere (a deliberate PEP 420 namespace package, see
# rust/hydrate/pyproject.toml's own module-name docs) - so rust/hydrate/ itself (the crate
# source directory, always present in a checkout) is ALSO a valid, empty namespace-package
# portion for "rust.hydrate". Confirmed live: with the compiled extension absent,
# `import rust.hydrate` resolves to that bare directory instead of raising ImportError, so
# importorskip alone never skips - only checking for the real extension's own attribute does.
if not hasattr(_hydrate_candidate, "ModelWriter"):
    pytest.skip(
        "rust.hydrate is an empty namespace package (extension not built) - run "
        "`maturin develop --release --manifest-path rust/hydrate/Cargo.toml` to opt in",
        allow_module_level=True,
    )
hydrate = _hydrate_candidate


def _serialize_both(model, columns: tuple[str, ...], instances: list) -> tuple[list, list]:
    """Runs both paths over the SAME instances and returns (python_rows, rust_rows) - never
    diffs against a value computed only once, since to_db_value() can mutate the instance
    (auto_now/auto_now_add) and running it twice on the same object would double-apply that
    side effect. Python runs first (on the original, unmutated instances); the Rust call
    that follows sees whatever state Python's own pass already left behind for any
    side-effecting field, exactly like it would in a real caller alternating between the two
    within the SAME batch (never happens in practice - a batch is either all-Rust or
    all-Python - but that's the honest way to compare two calls against shared object state
    without allocating fresh instances for each path)."""
    fields_map = model._meta.fields_map
    python_rows = [[fields_map[name].to_db_value(getattr(inst, name), inst) for name in columns] for inst in instances]

    writer = HydrateAccelerator.get_model_writer(model, columns, SQL_DIALECT.types)
    rust_rows = writer.write_rows(instances)
    return python_rows, rust_rows


def _assert_rows_match(model, columns: tuple[str, ...], python_rows: list, rust_rows: list) -> None:
    assert len(rust_rows) == len(python_rows)
    for python_row, rust_row in zip(python_rows, rust_rows, strict=True):
        for name, python_value, rust_value in zip(columns, python_row, rust_row, strict=True):
            assert rust_value == python_value, (
                f"{model.__name__}.{name}: rust serialize gave {rust_value!r}, python gave {python_value!r}"
            )
            assert type(rust_value) is type(python_value), (
                f"{model.__name__}.{name}: rust serialize gave a {type(rust_value).__name__}, "
                f"python gave a {type(python_value).__name__}"
            )


def _bucket_of(model, columns: tuple[str, ...]) -> dict[str, WriteCodecType]:
    fields_map = model._meta.fields_map
    return {
        name: FieldCodecs.get_write_specification(fields_map[name], SQL_DIALECT.types, Timezone.get_aware_zone_name())[
            0
        ]
        for name in columns
    }


def _validates_inline(model, column: str) -> bool:
    _codec_type, options = FieldCodecs.get_write_specification(
        model._meta.fields_map[column], SQL_DIALECT.types, Timezone.get_aware_zone_name()
    )
    return "checks" in options


# --- bucket 0 (NATIVE_INLINED): base Field.to_db_value, only cheap/static validators ---


class _BareIntField(Field[int]):
    """Every built-in int-shaped field now overrides to_db_value (IntField itself coerces a bool
    to a real int first - see its own to_db_value docstring), so none of them are bucket-0/1-
    eligible any more. This bare field - identical shape to what IntField looked like before that
    override, base Field.to_db_value plus the same int32 bounds - stands in for a real-world
    custom Field subclass that never overrides to_db_value at all, keeping bucket 0/1's own
    mechanics covered.
    """

    field_type = int

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.validators.append(MinValueValidator(-(2**31)))
        self.validators.append(MaxValueValidator(2**31 - 1))


def test_int_field_bucket_is_native_inlined():
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", _BareIntField())
    try:
        assert _bucket_of(IntFields, ("intnum",))["intnum"] == WriteCodecType.SCALAR
    finally:
        _restore_field(IntFields, "intnum", original_field)


def test_int_field_success_value_and_type_parity():
    columns = ("intnum", "intnum_null")
    instances = [IntFields(intnum=5, intnum_null=None), IntFields(intnum="42", intnum_null=7)]
    python_rows, rust_rows = _serialize_both(IntFields, columns, instances)
    _assert_rows_match(IntFields, columns, python_rows, rust_rows)


def test_char_field_max_length_violation_error_parity():
    columns = ("char",)
    inst = CharFields(char="x" * 300, char_null=None)
    writer = HydrateAccelerator.get_model_writer(CharFields, columns, SQL_DIALECT.types)
    fields_map = CharFields._meta.fields_map

    with pytest.raises(ValidationError) as python_exc:
        fields_map["char"].to_db_value(getattr(inst, "char"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_int_field_out_of_range_error_parity():
    columns = ("intnum", "intnum_null")
    inst = IntFields(intnum=99999999999999, intnum_null=None)
    fields_map = IntFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(IntFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["intnum"].to_db_value(getattr(inst, "intnum"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_int_field_type_conversion_failure_error_parity():
    # Round 5 audit bug: bucket 0's field_type() conversion call (int(raw) here) let the raw
    # PyO3 error (a bare ValueError) through unwrapped instead of the ValidationError
    # Field.to_db_value's own try/except around this exact call produces - meaning the exception
    # TYPE a caller sees for identical malformed input depended on whether the optional Rust
    # accelerator happened to be installed. This is bucket 0's conversion-failure counterpart to
    # test_int_field_out_of_range_error_parity above (which covers a VALIDATOR rejection, not a
    # conversion failure). Uses _BareIntField, not a real IntFields.intnum - see its own docstring.
    columns = ("intnum", "intnum_null")
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", _BareIntField())
    try:
        inst = IntFields(intnum=1, intnum_null=None)
        object.__setattr__(inst, "intnum", "not-an-int")
        fields_map = IntFields._meta.fields_map
        writer = HydrateAccelerator.get_model_writer(IntFields, columns, SQL_DIALECT.types)
        assert _bucket_of(IntFields, columns)["intnum"] == WriteCodecType.SCALAR

        with pytest.raises(ValidationError) as python_exc:
            fields_map["intnum"].to_db_value(getattr(inst, "intnum"), inst)
        with pytest.raises(ValidationError) as rust_exc:
            writer.write_rows([inst])

        assert str(rust_exc.value) == str(python_exc.value)
    finally:
        _restore_field(IntFields, "intnum", original_field)


def test_decimal_field_type_conversion_failure_error_parity():
    # DecimalField overrides to_db_value (to quantize a value before validating - see its own
    # to_db_value docstring), so it lands on bucket 2 (COMPLEX fallback), not bucket 1 - bucket
    # 1's own mechanics are covered generically by test_custom_validator_field_is_bucket_1_and_
    # matches below (IntField + a non-inlineable custom validator) instead. Kept here anyway
    # since bucket 2's OWN error-parity still needs verification.
    columns = ("decimal", "decimal_null")
    inst = DecimalFields(decimal=Decimal("1.0000"), decimal_null=None)
    object.__setattr__(inst, "decimal", "not-a-decimal")
    fields_map = DecimalFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(DecimalFields, columns, SQL_DIALECT.types)
    assert _bucket_of(DecimalFields, columns)["decimal"] == WriteCodecType.DECIMAL

    with pytest.raises(ValidationError) as python_exc:
        fields_map["decimal"].to_db_value(getattr(inst, "decimal"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_none_on_non_nullable_field_error_parity():
    columns = ("intnum", "intnum_null")
    inst = IntFields(intnum=1, intnum_null=None)
    inst.intnum = None  # bypass constructor-time validation, matches a direct attribute assignment
    fields_map = IntFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(IntFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["intnum"].to_db_value(getattr(inst, "intnum"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_none_on_nullable_field_returns_none_both_paths():
    columns = ("intnum", "intnum_null")
    inst = IntFields(intnum=1, intnum_null=None)
    python_rows, rust_rows = _serialize_both(IntFields, columns, [inst])
    assert python_rows == [[1, None]]
    assert rust_rows == [[1, None]]


def test_decimal_field_max_digits_validator_is_bucket_2_and_matches():
    # DecimalField overrides to_db_value (to quantize a value before validating), which
    # unconditionally lands it on bucket 2 regardless of its validators - MaxDigitsValidator
    # (appended in __init__ to actually enforce max_digits) is exercised here anyway, since
    # bucket 2's own field.validate() call-through still needs verification.
    columns = ("decimal", "decimal_null")
    assert _bucket_of(DecimalFields, columns)["decimal"] == WriteCodecType.DECIMAL
    inst = DecimalFields(decimal=Decimal("12.3456"), decimal_null=None)
    python_rows, rust_rows = _serialize_both(DecimalFields, columns, [inst])
    _assert_rows_match(DecimalFields, columns, python_rows, rust_rows)

    # Bucket 2 always calls the real field.to_db_value() - confirm MaxDigitsValidator's rejection
    # is identical between the pure-Python path and the Rust bucket-2 fallback.
    over_limit = DecimalFields(decimal=Decimal("123456789012345.6789"), decimal_null=None)
    fields_map = DecimalFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(DecimalFields, columns, SQL_DIALECT.types)
    with pytest.raises(ValidationError) as python_exc:
        fields_map["decimal"].to_db_value(over_limit.decimal, over_limit)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([over_limit])
    assert str(rust_exc.value) == str(python_exc.value)


def test_decimal_bounded_validator_excluded_from_bucket_0():
    # A Decimal-valued Min/MaxValueValidator must never be inlined (f64 precision risk on
    # money-shaped data) - DecimalField already falls to bucket 2 unconditionally (its own
    # to_db_value override), so this now just confirms adding one more validator doesn't change
    # that.
    #
    # _build_rust_serialize_plan is lru_cache-wrapped, keyed on (model, columns) - real
    # production code never mutates field.validators after model-class-definition time, but this
    # test does exactly that to exercise a different bucket, so it must clear the cache both
    # before (in case an EARLIER test already cached a bucket-0 plan for this same (model,
    # columns) pair) and after (so it doesn't leave a stale bucket-2 entry behind for a LATER
    # test) - see the analogous real-world guard on the read side,
    # test_plan_cache_reflects_timezone_config_change in test_rust_hydrate_parity.py.
    field = DecimalFields._meta.fields_map["decimal"]
    original_validators = list(field.validators)
    StatementPlans.model_writers.clear()
    field.validators.append(MinValueValidator(Decimal("0.0000")))
    try:
        assert _bucket_of(DecimalFields, ("decimal",))["decimal"] == WriteCodecType.DECIMAL
        inst = DecimalFields(decimal=Decimal("5.0000"), decimal_null=None)
        python_rows, rust_rows = _serialize_both(DecimalFields, ("decimal",), [inst])
        _assert_rows_match(DecimalFields, ("decimal",), python_rows, rust_rows)
    finally:
        field.validators = original_validators
        StatementPlans.model_writers.clear()


# --- bucket 1 (NATIVE_REAL_VALIDATE): base to_db_value, but a non-inlineable validator ---


class _MultipleOfThreeValidator(Validator):
    """Deliberately not in `_is_rust_serialize_inlineable_validator`'s recognized set, so a field
    carrying it can never qualify for bucket 0 - used below to force bucket 1 on a field that
    otherwise uses base `Field.to_db_value`.
    """

    def __call__(self, value: Any) -> None:
        if value % 3 != 0:
            self._raise("Value must be a multiple of three")


def test_custom_validator_field_is_bucket_1_and_matches():
    # A custom Validator subclass is not in the recognized inlineable set - forces bucket 1 even
    # though the field otherwise uses base Field.to_db_value. Uses _BareIntField, not a real
    # IntFields.intnum - see its own docstring for why no built-in field qualifies any more.
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", _BareIntField())
    tag_field = IntFields._meta.fields_map["intnum"]
    tag_field.validators.append(_MultipleOfThreeValidator())
    StatementPlans.model_writers.clear()
    try:
        assert _bucket_of(IntFields, ("intnum",))["intnum"] == WriteCodecType.SCALAR
        assert not _validates_inline(IntFields, "intnum")
        inst = IntFields(intnum=9, intnum_null=None)
        python_rows, rust_rows = _serialize_both(IntFields, ("intnum",), [inst])
        _assert_rows_match(IntFields, ("intnum",), python_rows, rust_rows)

        bad_inst = IntFields(intnum=10, intnum_null=None)  # violates the multiple-of-three rule
        fields_map = IntFields._meta.fields_map
        writer = HydrateAccelerator.get_model_writer(IntFields, ("intnum",), SQL_DIALECT.types)
        with pytest.raises(ValidationError) as python_exc:
            fields_map["intnum"].to_db_value(getattr(bad_inst, "intnum"), bad_inst)
        with pytest.raises(ValidationError) as rust_exc:
            writer.write_rows([bad_inst])
        assert str(rust_exc.value) == str(python_exc.value)
    finally:
        _restore_field(IntFields, "intnum", original_field)


# --- bucket 3 (DateField): str-parse (if applicable) -> validate ---


def test_date_field_bucket_is_3():
    assert _bucket_of(DateFields, ("date",))["date"] == WriteCodecType.DATE


def test_date_field_success_value_and_type_parity():
    import datetime

    columns = ("date", "date_null")
    instances = [
        DateFields(date=datetime.date(2024, 6, 15), date_null=None),
        DateFields(date=datetime.date(2020, 1, 1), date_null=datetime.date(2021, 2, 2)),
    ]
    python_rows, rust_rows = _serialize_both(DateFields, columns, instances)
    _assert_rows_match(DateFields, columns, python_rows, rust_rows)


def test_date_field_raw_string_is_parsed_identically():
    # A raw ISO string (bypasses from_db_value, like a plain attribute assignment) - only
    # strings longer than 4 chars get parsed at all, per DateField.to_db_value's own condition.
    columns = ("date",)
    inst = DateFields(date="2023-11-30T00:00:00", date_null=None)
    inst.date = "2023-11-30T00:00:00"
    python_rows, rust_rows = _serialize_both(DateFields, columns, [inst])
    _assert_rows_match(DateFields, columns, python_rows, rust_rows)


def test_date_field_malformed_string_error_parity():
    import datetime

    columns = ("date",)
    inst = DateFields(date=datetime.date(2024, 1, 1), date_null=None)
    inst.date = "not-a-date-string-longer-than-4-chars"
    fields_map = DateFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(DateFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["date"].to_db_value(getattr(inst, "date"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_date_field_none_on_non_nullable_returns_none_both_paths():
    # Unlike Int/CharField, DateField carries NO built-in validators at all (no
    # MinValueValidator/MaxLengthValidator equivalent) - None on a "non-nullable" DateField is
    # NOT rejected by to_db_value() itself, only later by the database's own NOT NULL
    # constraint. Not an error-parity case - both paths must silently agree on returning None.
    import datetime

    columns = ("date", "date_null")
    inst = DateFields(date=datetime.date(2024, 1, 1), date_null=None)
    inst.date = None
    python_rows, rust_rows = _serialize_both(DateFields, columns, [inst])
    assert python_rows == [[None, None]]
    assert rust_rows == [[None, None]]


# --- bucket 4 (UUIDField): validate(RAW value) -> isinstance/UUID()-coercion -> str() ---


def test_uuid_field_bucket_is_4():
    assert _bucket_of(UUIDFields, ("data",))["data"] == WriteCodecType.UUID


def test_uuid_field_success_value_and_type_parity():
    columns = ("data", "data_null")
    inst = UUIDFields(data=uuid.uuid4(), data_null=None)
    python_rows, rust_rows = _serialize_both(UUIDFields, columns, [inst])
    _assert_rows_match(UUIDFields, columns, python_rows, rust_rows)


def test_uuid_field_invalid_string_error_parity():
    columns = ("data",)
    inst = UUIDFields(data=uuid.uuid4())
    inst.data = "not-a-valid-uuid"
    fields_map = UUIDFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(UUIDFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["data"].to_db_value(getattr(inst, "data"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_uuid_field_none_on_non_nullable_returns_none_both_paths():
    # Like DateField, UUIDField carries no built-in validators - None on a "non-nullable" field
    # isn't rejected by to_db_value() itself, only later by the DB's own NOT NULL constraint.
    columns = ("data",)
    inst = UUIDFields(data=uuid.uuid4())
    inst.data = None
    python_rows, rust_rows = _serialize_both(UUIDFields, columns, [inst])
    assert python_rows == [[None]]
    assert rust_rows == [[None]]


# --- bucket 2 (COMPLEX fallback): every field overriding to_db_value ---


def test_boolean_field_bucket():
    # BooleanField overrides to_db_value (to reject a string instead of the truthy-coercion trap -
    # see its own to_db_value docstring) and declares that override as its native_write_check,
    # so it lands on bucket 7, not bucket 2.
    assert _bucket_of(BooleanFields, ("boolean",))["boolean"] == WriteCodecType.SCALAR
    inst = BooleanFields(boolean=True, boolean_null=None)
    python_rows, rust_rows = _serialize_both(BooleanFields, ("boolean", "boolean_null"), [inst])
    _assert_rows_match(BooleanFields, ("boolean", "boolean_null"), python_rows, rust_rows)


def test_float_field_bucket_and_parity():
    # FloatField overrides to_db_value (to reject NaN uniformly - see its own to_db_value
    # docstring) and declares that check as its native_write_check, so it lands on bucket 7.
    assert _bucket_of(FloatFields, ("floatnum",))["floatnum"] == WriteCodecType.SCALAR
    inst = FloatFields(floatnum=3.14, floatnum_null=None)
    python_rows, rust_rows = _serialize_both(FloatFields, ("floatnum", "floatnum_null"), [inst])
    _assert_rows_match(FloatFields, ("floatnum", "floatnum_null"), python_rows, rust_rows)


def test_float_field_nan_error_parity():
    columns = ("floatnum", "floatnum_null")
    inst = FloatFields(floatnum=1.0, floatnum_null=None)
    object.__setattr__(inst, "floatnum", math.nan)
    fields_map = FloatFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(FloatFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["floatnum"].to_db_value(getattr(inst, "floatnum"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


# --- bucket 6 (JSONField): dict/list fast path (validate -> encoder), else fallback ---


def test_json_field_bucket_is_6():
    assert _bucket_of(JSONFields, ("data",))["data"] == WriteCodecType.JSON


def test_json_field_dict_and_list_value_and_type_parity():
    columns = ("data", "data_null", "data_default")
    inst = JSONFields(data={"a": 1}, data_null=None, data_default={"b": [1, 2, 3]})
    python_rows, rust_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, rust_rows)


def test_json_field_list_value_parity():
    columns = ("data",)
    inst = JSONFields(data=[1, "two", {"three": 3}])
    python_rows, rust_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, rust_rows)


def test_json_field_encoder_failure_error_parity():
    # A dict containing something the encoder can't serialize - exercises bucket 6's OWN
    # encoder-failure wrapping (ValidationError), not the fallback path.
    columns = ("data",)

    class Unserializable:
        pass

    inst = JSONFields(data={"a": 1})
    inst.data = {"a": Unserializable()}
    fields_map = JSONFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(JSONFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["data"].to_db_value(getattr(inst, "data"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


@pytest.mark.parametrize(
    "value",
    [{"a": "x" + chr(0)}, [{"k" + chr(0): 1}], {"a": float("nan")}, [float("-inf")]],
    ids=repr,
)
def test_json_field_unstorable_value_error_parity(value):
    """Bucket 6 runs the same null-byte/NaN checks JSONField.to_db_value does."""
    columns = ("data",)
    inst = JSONFields(data={"a": 1})
    inst.data = value
    writer = HydrateAccelerator.get_model_writer(JSONFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        JSONFields._meta.fields_map["data"].to_db_value(value, inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_json_field_long_integer_value_parity():
    columns = ("data",)
    inst = JSONFields(data={"a": 2**64, "b": [-(2**63) - 1]})
    python_rows, rust_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, rust_rows)
    assert rust_rows[0][0] == '{"a":18446744073709551616,"b":[-9223372036854775809]}'


@pytest.mark.parametrize(
    ("model", "field_name", "sensitive_field"),
    [
        (JSONFields, "data", JSONField(sensitive=True)),
        (CharFields, "char", CharField(max_length=4, sensitive=True)),
        (UUIDFields, "data", UUIDField(sensitive=True)),
        (IntFields, "intnum", _BareIntField(sensitive=True)),
    ],
)
def test_sensitive_field_always_takes_bucket_2(model, field_name, sensitive_field):
    """Every fast-path bucket builds its own error messages from the raw value - a sensitive
    field's value is only ever handled by its real to_db_value()."""
    original_field = _swap_field_and_clear_cache(model, field_name, sensitive_field)
    try:
        assert _bucket_of(model, (field_name,))[field_name] == WriteCodecType.CALL
    finally:
        _restore_field(model, field_name, original_field)


def test_sensitive_inlined_validator_error_parity():
    columns = ("char",)
    original_field = _swap_field_and_clear_cache(CharFields, "char", CharField(max_length=4, sensitive=True))
    try:
        inst = CharFields(char="ab", char_null=None)
        inst.char = "SECRET-PIN"
        writer = HydrateAccelerator.get_model_writer(CharFields, columns, SQL_DIALECT.types)
        with pytest.raises(ValidationError) as python_exc:
            CharFields._meta.fields_map["char"].to_db_value(inst.char, inst)
        with pytest.raises(ValidationError) as rust_exc:
            writer.write_rows([inst])
    finally:
        _restore_field(CharFields, "char", original_field)

    assert str(rust_exc.value) == str(python_exc.value)
    assert "SECRET" not in str(rust_exc.value)


def test_json_field_str_value_uses_fallback_path():
    # A str value bypasses bucket 6's dict/list fast path entirely - exercises the fallback to
    # the real to_db_value() (its own decode-verify branch), matching bucket 2's own discipline.
    columns = ("data",)
    inst = JSONFields(data={"a": 1})
    inst.data = '{"a": 1}'
    python_rows, rust_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, rust_rows)


def test_json_field_invalid_non_dict_list_value_raises_same_error():
    # A non-dict/list value on a field with its own custom validator - bypasses the fast path
    # (not dict/list), exercises the fallback path's error handling.
    columns = ("data_validate",)
    inst = JSONFields(data_validate={"a": 1})
    inst.data_validate = 12345  # bypass constructor-time validation - raise_if_not_dict_or_list
    # (data_validate's own validator) should reject a non-dict/list value at to_db_value() time.
    fields_map = JSONFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(JSONFields, columns, SQL_DIALECT.types)

    with pytest.raises(Exception) as python_exc:
        fields_map["data_validate"].to_db_value(getattr(inst, "data_validate"), inst)
    with pytest.raises(Exception) as rust_exc:
        writer.write_rows([inst])

    assert type(rust_exc.value) is type(python_exc.value)
    assert str(rust_exc.value) == str(python_exc.value)


def test_uuid_field_raw_string_is_coerced_identically():
    columns = ("data",)
    raw = str(uuid.uuid4())
    inst = UUIDFields(data=raw)
    inst.data = raw  # bypass constructor-time from_db_value coercion, matches a raw attribute assignment
    python_rows, rust_rows = _serialize_both(UUIDFields, columns, [inst])
    _assert_rows_match(UUIDFields, columns, python_rows, rust_rows)


# --- bucket 5 (Enum): isinstance(raw, enum_type) fast path, else fallback to real to_db_value ---


def test_int_enum_field_is_bucket_5_and_matches():
    columns = ("service",)
    assert _bucket_of(EnumFields, columns)["service"] == WriteCodecType.ENUMERATION
    inst = EnumFields(service=Service.python_programming)
    python_rows, rust_rows = _serialize_both(EnumFields, columns, [inst])
    _assert_rows_match(EnumFields, columns, python_rows, rust_rows)


def test_int_enum_invalid_value_raises_same_error_type_and_message():
    columns = ("service",)
    inst = EnumFields(service=Service.python_programming)
    inst.service = 999999  # not a valid Service member
    fields_map = EnumFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(EnumFields, columns, SQL_DIALECT.types)

    with pytest.raises(Exception) as python_exc:
        fields_map["service"].to_db_value(getattr(inst, "service"), inst)
    with pytest.raises(Exception) as rust_exc:
        writer.write_rows([inst])

    # IntEnumFieldInstance.to_db_value now wraps the enum constructor's ValueError as a
    # ValidationError (see the fix in hare/fields/data/choices.py, tests/fields/test_enum.py's own
    # test_int_enum_invalid_value_raises_validation_error) - both paths must raise the SAME
    # (now-fixed) exception type.
    assert type(rust_exc.value) is type(python_exc.value) is ValidationError
    assert str(rust_exc.value) == str(python_exc.value)


def test_int_enum_raw_int_fallback_path_matches():
    # A raw int matching a valid member's value, but NOT an actual enum instance - fails
    # bucket 5's isinstance(raw, enum_type) fast-path check, exercising the FALLBACK to the real
    # to_db_value() succeeding (not just erroring, unlike the invalid-value test above).
    columns = ("service",)
    inst = EnumFields(service=Service.python_programming)
    inst.service = 2  # Service.database_design's value, but a plain int, not an enum member
    python_rows, rust_rows = _serialize_both(EnumFields, columns, [inst])
    _assert_rows_match(EnumFields, columns, python_rows, rust_rows)


def test_char_enum_field_is_bucket_5_and_matches():
    columns = ("currency",)
    assert _bucket_of(EnumFields, columns)["currency"] == WriteCodecType.ENUMERATION
    inst = EnumFields(currency=Currency.USD)
    python_rows, rust_rows = _serialize_both(EnumFields, columns, [inst])
    _assert_rows_match(EnumFields, columns, python_rows, rust_rows)


def test_char_enum_invalid_value_raises_validation_error_not_value_error():
    # CharEnumFieldInstance validates the RAW value first (inherited CharField.validate, via its
    # auto-computed max_length) - a value too long to be any real member fails length validation
    # before conversion is even attempted, unlike IntEnumFieldInstance's ValueError above.
    columns = ("currency",)
    inst = EnumFields(currency=Currency.USD)
    inst.currency = "not-a-real-currency-code-thats-way-too-long"
    fields_map = EnumFields._meta.fields_map
    writer = HydrateAccelerator.get_model_writer(EnumFields, columns, SQL_DIALECT.types)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["currency"].to_db_value(getattr(inst, "currency"), inst)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([inst])

    assert str(rust_exc.value) == str(python_exc.value)


def test_timedelta_field_is_bucket_2_and_matches():
    import datetime

    columns = ("timedelta", "timedelta_null")
    assert _bucket_of(TimeDeltaFields, columns)["timedelta"] == WriteCodecType.TIMEDELTA
    inst = TimeDeltaFields(timedelta=datetime.timedelta(days=2, hours=3), timedelta_null=None)
    python_rows, rust_rows = _serialize_both(TimeDeltaFields, columns, [inst])
    _assert_rows_match(TimeDeltaFields, columns, python_rows, rust_rows)


def test_datetime_field_plain_is_bucket_2_and_matches():
    import datetime

    columns = ("datetime", "datetime_null")
    assert _bucket_of(DatetimeFields, columns)["datetime"] == WriteCodecType.DATETIME
    inst = DatetimeFields(
        datetime=datetime.datetime(2024, 1, 1, 12, 0, 0),
        datetime_null=None,
        datetime_auto=datetime.datetime(2024, 1, 1),
        datetime_add=datetime.datetime(2024, 1, 1),
    )
    python_rows, rust_rows = _serialize_both(DatetimeFields, columns, [inst])
    _assert_rows_match(DatetimeFields, columns, python_rows, rust_rows)


def test_datetime_field_auto_now_side_effect_matches_between_paths():
    """auto_now/auto_now_add mutates the OWNING INSTANCE (setattr) as a side effect of
    to_db_value() - bucket 2 always calls the real method, so this should just work, but the
    side effect specifically (not just the returned value) needs its own assertion - a Rust bug
    threading the instance argument incorrectly would be invisible if only the return value were
    checked."""
    import datetime

    from tests.testmodels import DatetimeFields as DF

    # datetime_add (auto_now_add) is non-nullable, so the constructor rejects None outright -
    # construct with a real placeholder value, then bypass-assign None directly (matching every
    # other "simulate a direct attribute assignment" test in this file) so
    # to_db_value()'s own `getattr(instance, self.model_field_name) is None` check actually
    # fires and takes the auto_now_add mutation branch.
    python_inst = DF(
        datetime=datetime.datetime(2024, 1, 1, 12, 0, 0),
        datetime_null=None,
        datetime_auto=datetime.datetime(2020, 1, 1),
        datetime_add=datetime.datetime(2020, 1, 1),
    )
    python_inst.datetime_add = None
    rust_inst = DF(
        datetime=datetime.datetime(2024, 1, 1, 12, 0, 0),
        datetime_null=None,
        datetime_auto=datetime.datetime(2020, 1, 1),
        datetime_add=datetime.datetime(2020, 1, 1),
    )
    rust_inst.datetime_add = None
    columns = ("datetime_auto", "datetime_add")
    fields_map = DF._meta.fields_map
    python_row = [fields_map[name].to_db_value(getattr(python_inst, name), python_inst) for name in columns]

    writer = HydrateAccelerator.get_model_writer(DF, columns, SQL_DIALECT.types)
    rust_row = writer.write_rows([rust_inst])[0]

    # Both instances' own attributes must have been mutated to a real "now" value, not left as
    # the original (Falsy/pre-save) value passed to the constructor.
    assert python_inst.datetime_auto != datetime.datetime(2020, 1, 1)
    assert rust_inst.datetime_auto != datetime.datetime(2020, 1, 1)
    assert python_inst.datetime_add is not None
    assert rust_inst.datetime_add is not None
    assert type(rust_row[0]) is type(python_row[0])
    assert type(rust_row[1]) is type(python_row[1])


# ============================================================================
# Round-3 finding: the SAME class of bug as the read-side hydrate plan (see
# test_rust_hydrate_parity.py's own section) - DateField/UUIDField/Enum/JSONField's own
# isinstance-gated fast-path buckets (3/4/5/6) here ran their base class's write-side logic
# regardless of whether a further subclass overrode to_db_value with something genuinely
# different, only ever falling back to the real (overridden) to_db_value for a raw-value shape
# the fast path itself doesn't recognize - never for the common, successfully-fast-pathed case.
# ============================================================================


def _swap_field_and_clear_cache(model, field_name: str, new_field):
    # Every cache built for the model (compiled INSERT serializers, Rust serialize plans, ...) is
    # built from its fields - dropped on the swap and again by _restore_field().
    original_field = model._meta.fields_map[field_name]
    new_field.model_field_name = original_field.model_field_name
    new_field.source_field = original_field.source_field
    model._meta.fields_map[field_name] = new_field
    Caches.forget_model_caches([model])
    return original_field


def _restore_field(model, field_name: str, original_field):
    model._meta.fields_map[field_name] = original_field
    Caches.forget_model_caches([model])


def test_overridden_date_field_falls_back_to_complex_bucket():
    class CustomDateField(DateField):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance)

    original_field = _swap_field_and_clear_cache(DateFields, "date", CustomDateField())
    try:
        assert _bucket_of(DateFields, ("date",))["date"] == WriteCodecType.CALL
    finally:
        _restore_field(DateFields, "date", original_field)


def test_overridden_uuid_field_falls_back_to_complex_bucket():
    class CustomUUIDField(UUIDField):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance)

    original_field = _swap_field_and_clear_cache(UUIDFields, "data", CustomUUIDField())
    try:
        assert _bucket_of(UUIDFields, ("data",))["data"] == WriteCodecType.CALL
    finally:
        _restore_field(UUIDFields, "data", original_field)


def test_overridden_int_enum_field_falls_back_to_complex_bucket():
    class CustomIntEnumField(IntEnumFieldInstance):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance)

    original_field = _swap_field_and_clear_cache(EnumFields, "service", CustomIntEnumField(Service))
    try:
        assert _bucket_of(EnumFields, ("service",))["service"] == WriteCodecType.CALL
    finally:
        _restore_field(EnumFields, "service", original_field)


def test_overridden_char_enum_field_falls_back_to_complex_bucket():
    class CustomCharEnumField(CharEnumFieldInstance):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance)

    original_field = _swap_field_and_clear_cache(EnumFields, "currency", CustomCharEnumField(Currency))
    try:
        assert _bucket_of(EnumFields, ("currency",))["currency"] == WriteCodecType.CALL
    finally:
        _restore_field(EnumFields, "currency", original_field)


def test_overridden_json_field_falls_back_to_complex_bucket():
    class CustomJSONField(JSONField):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance)

    original_field = _swap_field_and_clear_cache(JSONFields, "data", CustomJSONField())
    try:
        assert _bucket_of(JSONFields, ("data",))["data"] == WriteCodecType.CALL
    finally:
        _restore_field(JSONFields, "data", original_field)


def test_pydantic_typed_json_field_falls_back_to_complex_bucket():
    """A field_type=SomePydanticModel JSONField (data_pydantic) must NOT take bucket 6 - the
    Rust-side json_field_value() has no equivalent to to_db_value()'s own field_type(**value)
    shape check, so bucket 6 would silently skip it (see the fix's own comment in
    rust_serialize_mixin.py for the full reasoning)."""
    assert _bucket_of(JSONFields, ("data_pydantic",))["data_pydantic"] == WriteCodecType.CALL


@pytest.mark.asyncio
async def test_pydantic_typed_json_field_rejects_malformed_dict_via_bulk_create(db):
    """End-to-end sibling of test_encrypted_style_json_subclass_override_actually_runs_on_real_write
    below: the real bug this fixes was bulk_create() (the only write path that goes through
    _build_rust_serialize_plan/hydrate.serialize_rows at all) silently writing a dict that
    doesn't match data_pydantic's own TestSchemaForJSONField shape, instead of raising
    ValidationError the way .save()/create() (which never go through the Rust bucket
    classification) already did."""
    malformed = {"totally": "wrong shape"}

    with pytest.raises(ValidationError):
        await JSONFields.objects.create(data={"a": 1}, data_pydantic=malformed)

    with pytest.raises(ValidationError):
        await JSONFields.objects.bulk_create([JSONFields(data={"a": 1}, data_pydantic=malformed)])


@pytest.mark.asyncio
async def test_encrypted_style_json_subclass_override_actually_runs_on_real_write(db):
    """End-to-end: a JSONField subclass whose to_db_value adds a detectable marker must see that
    marker actually land in the DB on a REAL bulk_create() (the only write path that goes
    through _build_rust_serialize_plan/hydrate.serialize_rows at all - a plain .create()/
    save() always calls field.to_db_value() directly, regardless of any Rust bucket, so it
    wouldn't have exercised this fix's own code path) - not just at the bucket-classification
    level tested above."""

    class MarkerJSONField(JSONField):
        def to_db_value(self, value, instance):
            if isinstance(value, dict):
                value = {**value, "_marked": True}
            return super().to_db_value(value, instance)

    # returning=True isn't supported for bulk_create() on SQLite (RETURNING's row order isn't
    # guaranteed there for a multi-row INSERT) - fetched back via a plain .get() instead (the
    # db fixture's own isolated transaction guarantees this is the only row) rather than
    # matching by a backfilled pk.
    original_field = _swap_field_and_clear_cache(JSONFields, "data", MarkerJSONField())
    try:
        await JSONFields.objects.bulk_create([JSONFields(data={"a": 1})])
    finally:
        # Restored BEFORE reading back, so the read goes through the plain (unpatched) field's
        # own from_db_value/decoder - isolates the WRITE side, independent of the read-side
        # fast path this same round covers separately (test_rust_hydrate_parity.py).
        _restore_field(JSONFields, "data", original_field)

    refreshed = await JSONFields.objects.get()
    assert refreshed.data == {"a": 1, "_marked": True}


# --- buckets 7/8 (NATIVE_CHECKED): fields whose own to_db_value declares native_write_check ---


class _Label(str):
    """A str subclass - never "exactly str", so buckets 7/8 hand it to the real to_db_value."""


def _instance_with_raw(model, **raw_values):
    """An instance holding each value exactly as given - no assignment coercion - so both paths
    see the same raw value."""
    instance = model()
    instance.__dict__.update(raw_values)
    return instance


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (IntFields, "intnum"),
        (BooleanFields, "boolean"),
        (CharFields, "char"),
        (TextFields, "text"),
        (FloatFields, "floatnum"),
    ],
)
def test_builtin_fields_with_write_checks_take_the_native_checked_bucket(model, column):
    """Bug: the bool/null-byte/NaN checks IntField, BooleanField, CharField, TextField and
    FloatField added to their own to_db_value sent every one of them to bucket 2 - a Python
    to_db_value call for every value of every bulk write."""
    assert _bucket_of(model, (column,))[column] == WriteCodecType.SCALAR


@pytest.mark.parametrize(
    ("model", "column", "raw"),
    [
        (IntFields, "intnum", 5),
        (IntFields, "intnum", True),
        (IntFields, "intnum", 7.0),
        (IntFields, "intnum", Decimal("8")),
        (IntFields, "intnum", "42"),
        (BooleanFields, "boolean", True),
        (BooleanFields, "boolean", 0),
        (CharFields, "char", "plain text"),
        (CharFields, "char", 123),
        (CharFields, "char", _Label("labelled")),
        (CharFields, "char", Currency.EUR),
        (TextFields, "text", "long text " * 50),
        (FloatFields, "floatnum", 1.5),
        (FloatFields, "floatnum", 3),
        (FloatFields, "floatnum", Decimal("2.5")),
        (FloatFields, "floatnum", math.inf),
    ],
)
def test_native_checked_value_and_type_parity(model, column, raw):
    instance = _instance_with_raw(model, **{column: raw})
    python_rows, rust_rows = _serialize_both(model, (column,), [instance])
    _assert_rows_match(model, (column,), python_rows, rust_rows)


@pytest.mark.parametrize(
    ("model", "column", "raw"),
    [
        (IntFields, "intnum", 7.5),
        (IntFields, "intnum", 2**40),
        (IntFields, "intnum", None),
        (BooleanFields, "boolean", "false"),
        (CharFields, "char", "a\x00b"),
        (CharFields, "char", "x" * 300),
        (CharFields, "char", "\x00" + "x" * 300),
        (TextFields, "text", "a\x00b"),
        (FloatFields, "floatnum", math.nan),
    ],
)
def test_native_checked_error_parity(model, column, raw):
    instance = _instance_with_raw(model, **{column: raw})
    field = model._meta.fields_map[column]
    writer = HydrateAccelerator.get_model_writer(model, (column,), SQL_DIALECT.types)
    with pytest.raises(ValidationError) as python_exc:
        field.to_db_value(raw, instance)
    with pytest.raises(ValidationError) as rust_exc:
        writer.write_rows([instance])
    assert str(rust_exc.value) == str(python_exc.value)


def test_native_checked_field_with_a_custom_validator_takes_bucket_8():
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", IntFields._meta.fields_map["intnum"].__copy__())
    IntFields._meta.fields_map["intnum"].validators.append(_MultipleOfThreeValidator())
    StatementPlans.model_writers.clear()
    try:
        assert _bucket_of(IntFields, ("intnum",))["intnum"] == WriteCodecType.SCALAR
        assert not _validates_inline(IntFields, "intnum")
        python_rows, rust_rows = _serialize_both(IntFields, ("intnum",), [_instance_with_raw(IntFields, intnum=9)])
        _assert_rows_match(IntFields, ("intnum",), python_rows, rust_rows)

        bad_instance = _instance_with_raw(IntFields, intnum=10)
        writer = HydrateAccelerator.get_model_writer(IntFields, ("intnum",), SQL_DIALECT.types)
        with pytest.raises(ValidationError) as python_exc:
            IntFields._meta.fields_map["intnum"].to_db_value(10, bad_instance)
        with pytest.raises(ValidationError) as rust_exc:
            writer.write_rows([bad_instance])
        assert str(rust_exc.value) == str(python_exc.value)
    finally:
        _restore_field(IntFields, "intnum", original_field)


def test_subclass_overriding_to_db_value_again_leaves_the_native_checked_bucket():
    class ShoutingCharField(CharField):
        def to_db_value(self, value, instance):
            value = super().to_db_value(value, instance)
            return value.upper() if isinstance(value, str) else value

    original_field = _swap_field_and_clear_cache(CharFields, "char", ShoutingCharField(max_length=255))
    try:
        assert _bucket_of(CharFields, ("char",))["char"] == WriteCodecType.CALL
        python_rows, rust_rows = _serialize_both(CharFields, ("char",), [_instance_with_raw(CharFields, char="hi")])
        assert rust_rows == python_rows == [["HI"]]
    finally:
        _restore_field(CharFields, "char", original_field)
