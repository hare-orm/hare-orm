"""Batch writers preserve field values, validation and instance side effects."""

from __future__ import annotations

import math
import uuid
from copy import copy
from decimal import Decimal
from functools import partial
from typing import Any

import pytest

from hare.core.caching.caches import Caches
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ValidationError
from hare.fields.data.choices import CharEnumFieldInstance, IntEnumFieldInstance
from hare.fields.data.json import JSONField
from hare.fields.data.temporal import DateField
from hare.fields.data.text import CharField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field
from hare.fields.validators import MaxValueValidator, MinValueValidator, Validator
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from tests.shared_models.testmodels import (
    BooleanFields,
    CharFields,
    Currency,
    DateFields,
    DatetimeFields,
    EnumFields,
    FloatFields,
    IntFields,
    JSONFields,
    Service,
    TextFields,
    TimeDeltaFields,
    UUIDFields,
)
from tests.shared_models.testmodels_postgres import DecimalFields


def _serialize_both(model, columns: tuple[str, ...], instances: list) -> tuple[list, list]:
    reference_instances = [copy(instance) for instance in instances]
    fields_map = model._meta.fields_map
    python_rows = [
        [fields_map[name].to_db_value(getattr(inst, name), inst) for name in columns] for inst in reference_instances
    ]

    writer = partial(BulkWriteBatches.serialize_instances, model, SQL_DIALECT.types, columns=columns)
    batch_rows = writer(instances)
    return python_rows, batch_rows


def _assert_rows_match(model, columns: tuple[str, ...], python_rows: list, batch_rows: list) -> None:
    assert len(batch_rows) == len(python_rows)
    for python_row, batch_row in zip(python_rows, batch_rows, strict=True):
        for name, python_value, batch_value in zip(columns, python_row, batch_row, strict=True):
            assert batch_value == python_value, (
                f"{model.__name__}.{name}: batch serialize gave {batch_value!r}, python gave {python_value!r}"
            )
            assert type(batch_value) is type(python_value), (
                f"{model.__name__}.{name}: batch serialize gave a {type(batch_value).__name__}, "
                f"python gave a {type(python_value).__name__}"
            )


class _BareIntField(Field[int]):
    field_type = int

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.validators.append(MinValueValidator(-(2**31)))
        self.validators.append(MaxValueValidator(2**31 - 1))


@pytest.mark.database_independent
def test_int_field_success_value_and_type_parity():
    columns = ("intnum", "intnum_null")
    instances = [IntFields(intnum=5, intnum_null=None), IntFields(intnum=42, intnum_null=7)]
    python_rows, batch_rows = _serialize_both(IntFields, columns, instances)
    _assert_rows_match(IntFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_char_field_max_length_violation_error_parity():
    columns = ("char",)
    inst = CharFields(char="x" * 300, char_null=None)
    writer = partial(BulkWriteBatches.serialize_instances, CharFields, SQL_DIALECT.types, columns=columns)
    fields_map = CharFields._meta.fields_map

    with pytest.raises(ValidationError) as python_exc:
        fields_map["char"].to_db_value(getattr(inst, "char"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_int_field_out_of_range_error_parity():
    columns = ("intnum", "intnum_null")
    inst = IntFields(intnum=99999999999999, intnum_null=None)
    fields_map = IntFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, IntFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["intnum"].to_db_value(getattr(inst, "intnum"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_int_field_type_conversion_failure_error_parity():
    columns = ("intnum", "intnum_null")
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", _BareIntField())
    try:
        inst = IntFields(intnum=1, intnum_null=None)
        object.__setattr__(inst, "intnum", "not-an-int")
        fields_map = IntFields._meta.fields_map
        writer = partial(BulkWriteBatches.serialize_instances, IntFields, SQL_DIALECT.types, columns=columns)

        with pytest.raises(ValidationError) as python_exc:
            fields_map["intnum"].to_db_value(getattr(inst, "intnum"), inst)
        with pytest.raises(ValidationError) as batch_exc:
            writer([inst])

        assert str(batch_exc.value) == str(python_exc.value)
    finally:
        _restore_field(IntFields, "intnum", original_field)


@pytest.mark.postgresql_models
@pytest.mark.database_independent
def test_decimal_field_type_conversion_failure_error_parity():
    columns = ("decimal", "decimal_null")
    inst = DecimalFields(decimal=Decimal("1.0000"), decimal_null=None)
    object.__setattr__(inst, "decimal", "not-a-decimal")
    fields_map = DecimalFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, DecimalFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["decimal"].to_db_value(getattr(inst, "decimal"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_none_on_non_nullable_field_error_parity():
    columns = ("intnum", "intnum_null")
    inst = IntFields(intnum=1, intnum_null=None)
    inst.intnum = None  # bypass constructor-time validation, matches a direct attribute assignment
    fields_map = IntFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, IntFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["intnum"].to_db_value(getattr(inst, "intnum"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_none_on_nullable_field_returns_none_both_paths():
    columns = ("intnum", "intnum_null")
    inst = IntFields(intnum=1, intnum_null=None)
    python_rows, batch_rows = _serialize_both(IntFields, columns, [inst])
    assert python_rows == [[1, None]]
    assert batch_rows == [[1, None]]


@pytest.mark.postgresql_models
@pytest.mark.database_independent
def test_decimal_max_digits_validation_matches():
    columns = ("decimal", "decimal_null")
    inst = DecimalFields(decimal=Decimal("12.3456"), decimal_null=None)
    python_rows, batch_rows = _serialize_both(DecimalFields, columns, [inst])
    _assert_rows_match(DecimalFields, columns, python_rows, batch_rows)

    over_limit = DecimalFields(decimal=Decimal("123456789012345.6789"), decimal_null=None)
    fields_map = DecimalFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, DecimalFields, SQL_DIALECT.types, columns=columns)
    with pytest.raises(ValidationError) as python_exc:
        fields_map["decimal"].to_db_value(over_limit.decimal, over_limit)
    with pytest.raises(ValidationError) as batch_exc:
        writer([over_limit])
    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.postgresql_models
@pytest.mark.database_independent
def test_decimal_bound_validator_preserves_precision():
    field = DecimalFields._meta.fields_map["decimal"]
    original_validators = list(field.validators)
    BulkWriteBatches.SERIALIZE_INSTANCES_CACHE.clear()
    field.validators.append(MinValueValidator(Decimal("0.0000")))
    try:
        inst = DecimalFields(decimal=Decimal("5.0000"), decimal_null=None)
        python_rows, batch_rows = _serialize_both(DecimalFields, ("decimal",), [inst])
        _assert_rows_match(DecimalFields, ("decimal",), python_rows, batch_rows)
    finally:
        field.validators = original_validators
        BulkWriteBatches.SERIALIZE_INSTANCES_CACHE.clear()


class _MultipleOfThreeValidator(Validator):
    def __call__(self, value: Any) -> None:
        if value % 3 != 0:
            self._raise("Value must be a multiple of three")


@pytest.mark.database_independent
def test_base_field_custom_validator_is_called():
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", _BareIntField())
    tag_field = IntFields._meta.fields_map["intnum"]
    tag_field.validators.append(_MultipleOfThreeValidator())
    BulkWriteBatches.SERIALIZE_INSTANCES_CACHE.clear()
    try:
        inst = IntFields(intnum=9, intnum_null=None)
        python_rows, batch_rows = _serialize_both(IntFields, ("intnum",), [inst])
        _assert_rows_match(IntFields, ("intnum",), python_rows, batch_rows)

        bad_inst = IntFields(intnum=10, intnum_null=None)  # violates the multiple-of-three rule
        fields_map = IntFields._meta.fields_map
        writer = partial(BulkWriteBatches.serialize_instances, IntFields, SQL_DIALECT.types, columns=("intnum",))
        with pytest.raises(ValidationError) as python_exc:
            fields_map["intnum"].to_db_value(getattr(bad_inst, "intnum"), bad_inst)
        with pytest.raises(ValidationError) as batch_exc:
            writer([bad_inst])
        assert str(batch_exc.value) == str(python_exc.value)
    finally:
        _restore_field(IntFields, "intnum", original_field)


@pytest.mark.database_independent
def test_date_field_success_value_and_type_parity():
    import datetime

    columns = ("date", "date_null")
    instances = [
        DateFields(date=datetime.date(2024, 6, 15), date_null=None),
        DateFields(date=datetime.date(2020, 1, 1), date_null=datetime.date(2021, 2, 2)),
    ]
    python_rows, batch_rows = _serialize_both(DateFields, columns, instances)
    _assert_rows_match(DateFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_date_field_raw_string_is_parsed_identically():
    # A raw ISO string (bypasses from_db_value, like a plain attribute assignment) - only
    # strings longer than 4 chars get parsed at all, per DateField.to_db_value's own condition.
    columns = ("date",)
    inst = DateFields(date="2023-11-30T00:00:00", date_null=None)
    inst.date = "2023-11-30T00:00:00"
    python_rows, batch_rows = _serialize_both(DateFields, columns, [inst])
    _assert_rows_match(DateFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_date_field_malformed_string_error_parity():
    import datetime

    columns = ("date",)
    inst = DateFields(date=datetime.date(2024, 1, 1), date_null=None)
    inst.date = "not-a-date-string-longer-than-4-chars"
    fields_map = DateFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, DateFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["date"].to_db_value(getattr(inst, "date"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_date_field_none_on_non_nullable_returns_none_both_paths():
    # Unlike Int/CharField, DateField carries NO built-in validators at all (no
    # MinValueValidator/MaxLengthValidator equivalent) - None on a "non-nullable" DateField is
    # NOT rejected by to_db_value() itself, only later by the database's own NOT NULL
    # constraint. Not an error-parity case - both paths must silently agree on returning None.
    import datetime

    columns = ("date", "date_null")
    inst = DateFields(date=datetime.date(2024, 1, 1), date_null=None)
    inst.date = None
    python_rows, batch_rows = _serialize_both(DateFields, columns, [inst])
    assert python_rows == [[None, None]]
    assert batch_rows == [[None, None]]


@pytest.mark.database_independent
def test_uuid_field_success_value_and_type_parity():
    columns = ("data", "data_null")
    inst = UUIDFields(data=uuid.uuid4(), data_null=None)
    python_rows, batch_rows = _serialize_both(UUIDFields, columns, [inst])
    _assert_rows_match(UUIDFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_uuid_field_invalid_string_error_parity():
    columns = ("data",)
    inst = UUIDFields(data=uuid.uuid4())
    inst.data = "not-a-valid-uuid"
    fields_map = UUIDFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, UUIDFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["data"].to_db_value(getattr(inst, "data"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_uuid_field_none_on_non_nullable_returns_none_both_paths():
    # Like DateField, UUIDField carries no built-in validators - None on a "non-nullable" field
    # isn't rejected by to_db_value() itself, only later by the DB's own NOT NULL constraint.
    columns = ("data",)
    inst = UUIDFields(data=uuid.uuid4())
    inst.data = None
    python_rows, batch_rows = _serialize_both(UUIDFields, columns, [inst])
    assert python_rows == [[None]]
    assert batch_rows == [[None]]


@pytest.mark.parametrize(
    ("model_class", "columns", "values"),
    [
        # BooleanField overrides to_db_value (to reject a string instead of the truthy-coercion trap)
        # and declares that override as its native_write_check.
        pytest.param(BooleanFields, ("boolean", "boolean_null"), {"boolean": True}, id="boolean_field_bucket"),
        pytest.param(
            FloatFields, ("floatnum", "floatnum_null"), {"floatnum": 3.14}, id="float_field_bucket_and_parity"
        ),
    ],
)
@pytest.mark.database_independent
def test_checked_scalar_values_match(model_class, columns, values):
    inst = model_class(**values, **{columns[1]: None})
    python_rows, batch_rows = _serialize_both(model_class, columns, [inst])
    _assert_rows_match(model_class, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_float_field_nan_error_parity():
    columns = ("floatnum", "floatnum_null")
    inst = FloatFields(floatnum=1.0, floatnum_null=None)
    object.__setattr__(inst, "floatnum", math.nan)
    fields_map = FloatFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, FloatFields, SQLITE_DIALECT.types, columns=columns)

    # SQLite stores a NaN as NULL - its own writer refuses it.
    with pytest.raises(ValidationError) as python_exc:
        SQLITE_DIALECT.types.get_db_writer(fields_map["floatnum"])(getattr(inst, "floatnum"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_json_field_dict_and_list_value_and_type_parity():
    columns = ("data", "data_null", "data_default")
    inst = JSONFields(data={"a": 1}, data_null=None, data_default={"b": [1, 2, 3]})
    python_rows, batch_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_json_field_list_value_parity():
    columns = ("data",)
    inst = JSONFields(data=[1, "two", {"three": 3}])
    python_rows, batch_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_json_field_encoder_failure_error_parity():
    columns = ("data",)

    class Unserializable:
        pass

    inst = JSONFields(data={"a": 1})
    inst.data = {"a": Unserializable()}
    fields_map = JSONFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, JSONFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["data"].to_db_value(getattr(inst, "data"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.parametrize(
    "value",
    [{"a": float("nan")}, [float("-inf")]],
    ids=repr,
)
def test_json_field_unstorable_value_error_parity(value):
    """Bucket 6 runs the same NaN checks JSONField.to_db_value does."""
    columns = ("data",)
    inst = JSONFields(data={"a": 1})
    inst.data = value
    writer = partial(BulkWriteBatches.serialize_instances, JSONFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        JSONFields._meta.fields_map["data"].to_db_value(value, inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_json_field_long_integer_value_parity():
    columns = ("data",)
    inst = JSONFields(data={"a": 2**64, "b": [-(2**63) - 1]})
    python_rows, batch_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, batch_rows)
    assert batch_rows[0][0] == '{"a":18446744073709551616,"b":[-9223372036854775809]}'


@pytest.mark.database_independent
def test_sensitive_inlined_validator_error_parity():
    columns = ("char",)
    original_field = _swap_field_and_clear_cache(CharFields, "char", CharField(max_length=4, sensitive=True))
    try:
        inst = CharFields(char="ab", char_null=None)
        inst.char = "SECRET-PIN"
        writer = partial(BulkWriteBatches.serialize_instances, CharFields, SQL_DIALECT.types, columns=columns)
        with pytest.raises(ValidationError) as python_exc:
            CharFields._meta.fields_map["char"].to_db_value(inst.char, inst)
        with pytest.raises(ValidationError) as batch_exc:
            writer([inst])
    finally:
        _restore_field(CharFields, "char", original_field)

    assert str(batch_exc.value) == str(python_exc.value)
    assert "SECRET" not in str(batch_exc.value)


@pytest.mark.database_independent
def test_json_field_str_value_uses_fallback_path():
    columns = ("data",)
    inst = JSONFields(data={"a": 1})
    inst.data = '{"a": 1}'
    python_rows, batch_rows = _serialize_both(JSONFields, columns, [inst])
    _assert_rows_match(JSONFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_json_field_invalid_non_dict_list_value_raises_same_error():
    # A non-dict/list value on a field with its own custom validator - bypasses the fast path
    # (not dict/list), exercises the fallback path's error handling.
    columns = ("data_validate",)
    inst = JSONFields(data_validate={"a": 1})
    inst.data_validate = 12345  # bypass constructor-time validation - raise_if_not_dict_or_list
    # (data_validate's own validator) should reject a non-dict/list value at to_db_value() time.
    fields_map = JSONFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, JSONFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(Exception) as python_exc:
        fields_map["data_validate"].to_db_value(getattr(inst, "data_validate"), inst)
    with pytest.raises(Exception) as batch_exc:
        writer([inst])

    assert type(batch_exc.value) is type(python_exc.value)
    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_uuid_field_raw_string_is_coerced_identically():
    columns = ("data",)
    raw = str(uuid.uuid4())
    inst = UUIDFields(data=raw)
    inst.data = raw  # bypass constructor-time from_db_value coercion, matches a raw attribute assignment
    python_rows, batch_rows = _serialize_both(UUIDFields, columns, [inst])
    _assert_rows_match(UUIDFields, columns, python_rows, batch_rows)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("service", Service.python_programming, id="int_enum_field_is_bucket_5_and_matches"),
        pytest.param("currency", Currency.USD, id="char_enum_field_is_bucket_5_and_matches"),
    ],
)
@pytest.mark.database_independent
def test_enum_values_match(column, value):
    columns = (column,)
    inst = EnumFields(**{column: value})
    python_rows, batch_rows = _serialize_both(EnumFields, columns, [inst])
    _assert_rows_match(EnumFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_int_enum_invalid_value_raises_same_error_type_and_message():
    columns = ("service",)
    inst = EnumFields(service=Service.python_programming)
    inst.service = 999999  # not a valid Service member
    fields_map = EnumFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, EnumFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(Exception) as python_exc:
        fields_map["service"].to_db_value(getattr(inst, "service"), inst)
    with pytest.raises(Exception) as batch_exc:
        writer([inst])

    # IntEnumFieldInstance.to_db_value now wraps the enum constructor's ValueError as a
    # ValidationError (see the fix in hare/fields/data/choices.py, tests/fields/value_types/test_enum.py's own
    # test_int_enum_invalid_value_raises_validation_error) - both paths must raise the SAME
    # (now-fixed) exception type.
    assert type(batch_exc.value) is type(python_exc.value) is ValidationError
    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_int_enum_raw_int_fallback_path_matches():
    columns = ("service",)
    inst = EnumFields(service=Service.python_programming)
    inst.service = 2  # Service.database_design's value, but a plain int, not an enum member
    python_rows, batch_rows = _serialize_both(EnumFields, columns, [inst])
    _assert_rows_match(EnumFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_char_enum_invalid_value_raises_validation_error_not_value_error():
    # CharEnumFieldInstance validates the RAW value first (inherited CharField.validate, via its
    # auto-computed max_length) - a value too long to be any real member fails length validation
    # before conversion is even attempted, unlike IntEnumFieldInstance's ValueError above.
    columns = ("currency",)
    inst = EnumFields(currency=Currency.USD)
    inst.currency = "not-a-real-currency-code-thats-way-too-long"
    fields_map = EnumFields._meta.fields_map
    writer = partial(BulkWriteBatches.serialize_instances, EnumFields, SQL_DIALECT.types, columns=columns)

    with pytest.raises(ValidationError) as python_exc:
        fields_map["currency"].to_db_value(getattr(inst, "currency"), inst)
    with pytest.raises(ValidationError) as batch_exc:
        writer([inst])

    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_timedelta_values_match():
    import datetime

    columns = ("timedelta", "timedelta_null")
    inst = TimeDeltaFields(timedelta=datetime.timedelta(days=2, hours=3), timedelta_null=None)
    python_rows, batch_rows = _serialize_both(TimeDeltaFields, columns, [inst])
    _assert_rows_match(TimeDeltaFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_datetime_values_match():
    import datetime

    columns = ("datetime", "datetime_null")
    inst = DatetimeFields(
        datetime=datetime.datetime(2024, 1, 1, 12, 0, 0),
        datetime_null=None,
        datetime_auto=datetime.datetime(2024, 1, 1),
        datetime_add=datetime.datetime(2024, 1, 1),
    )
    python_rows, batch_rows = _serialize_both(DatetimeFields, columns, [inst])
    _assert_rows_match(DatetimeFields, columns, python_rows, batch_rows)


@pytest.mark.database_independent
def test_datetime_field_auto_now_values_are_written_as_they_are_on_both_paths():
    import datetime

    from tests.shared_models.testmodels import DatetimeFields as DF

    python_inst = DF(
        datetime=datetime.datetime(2024, 1, 1, 12, 0, 0),
        datetime_null=None,
        datetime_auto=datetime.datetime(2020, 1, 1),
        datetime_add=datetime.datetime(2020, 1, 1),
    )
    batch_inst = DF(
        datetime=datetime.datetime(2024, 1, 1, 12, 0, 0),
        datetime_null=None,
        datetime_auto=datetime.datetime(2020, 1, 1),
        datetime_add=datetime.datetime(2020, 1, 1),
    )
    columns = ("datetime_auto", "datetime_add")
    held_values = [(inst.datetime_auto, inst.datetime_add) for inst in (python_inst, batch_inst)]
    fields_map = DF._meta.fields_map
    python_row = [fields_map[name].to_db_value(getattr(python_inst, name), python_inst) for name in columns]

    writer = partial(BulkWriteBatches.serialize_instances, DF, SQL_DIALECT.types, columns=columns)
    batch_row = writer([batch_inst])[0]

    assert [(inst.datetime_auto, inst.datetime_add) for inst in (python_inst, batch_inst)] == held_values
    assert batch_row == python_row


def _swap_field_and_clear_cache(model, field_name: str, new_field):
    original_field = model._meta.fields_map[field_name]
    new_field.model_field_name = original_field.model_field_name
    new_field.source_field = original_field.source_field
    model._meta.fields_map[field_name] = new_field
    Caches.forget_model_caches([model])
    return original_field


def _restore_field(model, field_name: str, original_field):
    model._meta.fields_map[field_name] = original_field
    Caches.forget_model_caches([model])


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("model", "name", "field_class", "args"),
    [
        (DateFields, "date", DateField, ()),
        (UUIDFields, "data", UUIDField, ()),
        (EnumFields, "service", IntEnumFieldInstance, (Service,)),
        (EnumFields, "currency", CharEnumFieldInstance, (Currency,)),
        (JSONFields, "data", JSONField, ()),
    ],
)
def test_batch_writer_calls_subclass_with_each_instance(model, name, field_class, args):
    calls = []

    class CustomField(field_class):
        def to_db_value(self, value, instance):
            calls.append((value, instance))
            return f"converted:{value}"

    original = _swap_field_and_clear_cache(model, name, CustomField(*args))
    try:
        instances = [_instance_with_raw(model, **{name: value}) for value in ("first", "second")]
        rows = BulkWriteBatches.serialize_instances(model, SQL_DIALECT.types, instances, [name])
        assert rows == [["converted:first"], ["converted:second"]]
        assert calls == [("first", instances[0]), ("second", instances[1])]
    finally:
        _restore_field(model, name, original)


@pytest.mark.asyncio
async def test_encrypted_style_json_subclass_override_actually_runs_on_real_write(db):

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
        _restore_field(JSONFields, "data", original_field)

    refreshed = await JSONFields.objects.get()
    assert refreshed.data == {"a": 1, "_marked": True}


class _Label(str):
    """A distinct text subclass for scalar conversion checks."""


def _instance_with_raw(model, **raw_values):
    """An instance holding each value exactly as given - no assignment coercion - so both paths
    see the same raw value."""
    instance = model()
    instance.__dict__.update(raw_values)
    return instance


@pytest.mark.parametrize(
    ("model", "column", "raw"),
    [
        (IntFields, "intnum", 5),
        (BooleanFields, "boolean", True),
        (CharFields, "char", "plain text"),
        (CharFields, "char", _Label("labelled")),
        (CharFields, "char", Currency.EUR),
        (TextFields, "text", "long text " * 50),
        (FloatFields, "floatnum", 1.5),
        (FloatFields, "floatnum", 3),
        (FloatFields, "floatnum", Decimal("2.5")),
        (FloatFields, "floatnum", math.inf),
    ],
)
def test_scalar_value_and_type_parity(model, column, raw):
    instance = _instance_with_raw(model, **{column: raw})
    python_rows, batch_rows = _serialize_both(model, (column,), [instance])
    _assert_rows_match(model, (column,), python_rows, batch_rows)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("model", "column", "raw"),
    [
        (IntFields, "intnum", 7.5),
        (IntFields, "intnum", 2**40),
        (IntFields, "intnum", None),
        (BooleanFields, "boolean", "false"),
        (CharFields, "char", 123),
        (CharFields, "char", "x" * 300),
        (CharFields, "char", "\x00" + "x" * 300),
        (FloatFields, "floatnum", math.nan),
    ],
)
def test_scalar_error_parity(model, column, raw):
    instance = _instance_with_raw(model, **{column: raw})
    field = model._meta.fields_map[column]
    writer = partial(BulkWriteBatches.serialize_instances, model, SQLITE_DIALECT.types, columns=(column,))
    with pytest.raises(ValidationError) as python_exc:
        SQLITE_DIALECT.types.get_db_writer(field)(raw, instance)
    with pytest.raises(ValidationError) as batch_exc:
        writer([instance])
    assert str(batch_exc.value) == str(python_exc.value)


@pytest.mark.database_independent
def test_scalar_custom_validator_is_called():
    original_field = _swap_field_and_clear_cache(IntFields, "intnum", IntFields._meta.fields_map["intnum"].__copy__())
    IntFields._meta.fields_map["intnum"].validators.append(_MultipleOfThreeValidator())
    BulkWriteBatches.SERIALIZE_INSTANCES_CACHE.clear()
    try:
        python_rows, batch_rows = _serialize_both(IntFields, ("intnum",), [_instance_with_raw(IntFields, intnum=9)])
        _assert_rows_match(IntFields, ("intnum",), python_rows, batch_rows)

        bad_instance = _instance_with_raw(IntFields, intnum=10)
        writer = partial(BulkWriteBatches.serialize_instances, IntFields, SQL_DIALECT.types, columns=("intnum",))
        with pytest.raises(ValidationError) as python_exc:
            IntFields._meta.fields_map["intnum"].to_db_value(10, bad_instance)
        with pytest.raises(ValidationError) as batch_exc:
            writer([bad_instance])
        assert str(batch_exc.value) == str(python_exc.value)
    finally:
        _restore_field(IntFields, "intnum", original_field)


@pytest.mark.database_independent
def test_custom_text_writer_changes_values():
    class ShoutingCharField(CharField):
        def to_db_value(self, value, instance):
            value = super().to_db_value(value, instance)
            return value.upper() if isinstance(value, str) else value

    original_field = _swap_field_and_clear_cache(CharFields, "char", ShoutingCharField(max_length=255))
    try:
        python_rows, batch_rows = _serialize_both(CharFields, ("char",), [_instance_with_raw(CharFields, char="hi")])
        assert batch_rows == python_rows == [["HI"]]
    finally:
        _restore_field(CharFields, "char", original_field)
