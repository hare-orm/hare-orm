import pytest

from hare import fields
from hare.exceptions import IntegrityError, ValidationError
from hare.query.expressions import F
from tests.shared_models import testmodels, testmodels_postgres


@pytest.mark.parametrize(
    ("field_class", "options", "required"),
    [
        pytest.param(fields.Field, {}, True, id="required_by_default"),
        pytest.param(fields.Field, {"generated": True}, False, id="if_generated_then_not_required"),
        pytest.param(fields.Field, {"null": True}, False, id="if_null_then_not_required"),
        pytest.param(fields.TextField, {"default": ""}, False, id="if_has_non_null_default_then_not_required"),
        pytest.param(fields.TextField, {"default": None}, True, id="if_null_default_then_required"),
    ],
)
@pytest.mark.database_independent
def test_the_required_flag_of_a_field(field_class, options, required):
    assert field_class(**options).required is required


@pytest.mark.parametrize(
    ("model_class", "exception_class"),
    [
        pytest.param(testmodels.BinaryFields, IntegrityError, id="binary_empty"),
        pytest.param(testmodels.BooleanFields, IntegrityError, id="bool_empty"),
        pytest.param(testmodels.CharFields, ValidationError, id="char_empty"),
        pytest.param(testmodels.DecimalFields, ValidationError, id="decimal_empty"),
        pytest.param(testmodels.EnumFields, ValidationError, id="int_enum_empty"),
        pytest.param(testmodels.MinRelation, ValidationError, id="fk_empty"),
        pytest.param(testmodels.Student, ValidationError, id="student__empty"),
        pytest.param(testmodels.FloatFields, IntegrityError, id="float_empty"),
        pytest.param(testmodels.IntFields, ValidationError, id="int_fields_empty"),
        pytest.param(testmodels.SmallIntFields, ValidationError, id="small_int_fields_empty"),
        pytest.param(testmodels.BigIntFields, ValidationError, id="big_int_fields_empty"),
        pytest.param(testmodels.JSONFields, IntegrityError, id="json_empty"),
        pytest.param(testmodels.Principal, ValidationError, id="principal__empty"),
        pytest.param(testmodels.TextFields, IntegrityError, id="text_empty"),
        pytest.param(testmodels.DatetimeFields, IntegrityError, id="empty_datetime_fields"),
        pytest.param(testmodels.DateFields, IntegrityError, id="empty_date_fields"),
        pytest.param(testmodels.TimeDeltaFields, IntegrityError, id="empty_timedelta_fields"),
        pytest.param(testmodels.UUIDFields, IntegrityError, id="uuid_empty"),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_required_value_is_refused(db, model_class, exception_class):
    """A missing required char, decimal or int-family value (an int enum, a relation's "*_id" column)
    fails fast with ValidationError from the field's own MaxLength/MaxDigits/MinValue/MaxValue
    validators, which reject None before the value reaches the DB; any other one is refused by the
    DB's NOT NULL constraint."""
    with pytest.raises(exception_class):
        await model_class.objects.create()


@pytest.mark.parametrize(
    ("model_class", "field_name", "value"),
    [
        # ArrayField.to_db_value converted every element with no check that the value is iterable.
        pytest.param(testmodels_postgres.ArrayFields, "array", 123, id="to_db_value_wraps_non_iterable"),
        # DateField/DatetimeField.to_db_value's own parse_datetime() call.
        pytest.param(
            testmodels.DateFields,
            "date",
            "not-a-date-string-longer-than-4-chars",
            id="date_field_to_db_value_wraps_malformed_string",
        ),
        pytest.param(
            testmodels.DatetimeFields,
            "datetime",
            "not-a-datetime-string",
            id="datetime_field_to_db_value_wraps_malformed_string",
        ),
        # TimeDeltaField.to_db_value read value.days/seconds/microseconds with no type check.
        pytest.param(
            testmodels.TimeDeltaFields,
            "timedelta",
            "not-a-timedelta",
            id="timedelta_field_to_db_value_wraps_wrong_type",
        ),
    ],
)
@pytest.mark.database_independent
def test_to_db_value_wraps_a_value_it_cannot_convert(model_class, field_name, value):
    """A value a field cannot convert raises the framework's own catchable ValidationError, not the
    bare TypeError/AttributeError/parse error of the conversion step."""
    field = model_class._meta.fields_map[field_name]
    with pytest.raises(ValidationError):
        field.to_db_value(value, model_class)


@pytest.mark.parametrize(
    ("model_class", "field_name", "value", "message"),
    [
        # bytes(5) means "5 zero bytes" in Python, not "the bytes representation of the int 5" - a
        # trap the base Field.to_db_value()/from_db_value() generic coercion fell into silently.
        pytest.param(
            testmodels.BinaryFields,
            "binary",
            5,
            "expected bytes-like data",
            id="int_value_raises_instead_of_becoming_n_zero_bytes",
        ),
        # bool("false") is True - any non-empty string is truthy regardless of its content.
        pytest.param(
            testmodels.BooleanFields,
            "boolean",
            "false",
            "expected a bool",
            id="string_value_raises_instead_of_truthy_coercion",
        ),
        # 0 and 1 were taken as False and True - a number given for a flag.
        pytest.param(
            testmodels.BooleanFields, "boolean", 1, "expected a bool, got int", id="an_integer_given_for_a_flag_1"
        ),
        pytest.param(
            testmodels.BooleanFields, "boolean", 0, "expected a bool, got int", id="an_integer_given_for_a_flag_0"
        ),
        # A number was written as its text - a text field takes only a str.
        pytest.param(testmodels.CharFields, "char", 33, "char: expected a str, got int", id="a_number_is_no_text"),
        # int() read "3", "1_000" and "٥" as numbers the caller didn't mean.
        pytest.param(testmodels.IntFields, "intnum", "3", "expected an int, got str", id="a_text_given_for_an_int_3"),
        pytest.param(
            testmodels.IntFields, "intnum", "1_000", "expected an int, got str", id="a_text_given_for_an_int_1_000"
        ),
        pytest.param(
            testmodels.IntFields, "intnum", "٥", "expected an int, got str", id="a_text_given_for_an_int_arabic_5"
        ),
        pytest.param(
            testmodels.BigIntFields, "intnum", "3", "expected an int, got str", id="a_text_given_for_a_big_int"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_value_of_another_type_is_refused(db, model_class, field_name, value, message):
    with pytest.raises(ValidationError, match=message):
        await model_class.objects.create(**{field_name: value})


@pytest.mark.parametrize(
    ("model_class", "field_name", "value"),
    [
        pytest.param(testmodels.CharFields, "char", "moo", id="char_create"),
        pytest.param(testmodels.TextFields, "text", "baaa" * 32000, id="text_create"),
        pytest.param(testmodels.BooleanFields, "boolean", True, id="bool_create"),
        pytest.param(testmodels.BinaryFields, "binary", bytes(range(256)) * 500, id="binary_create"),
        pytest.param(testmodels.FloatFields, "floatnum", 1.23, id="float_create"),
        pytest.param(testmodels.IntFields, "intnum", -2147483648, id="int_fields_min"),
        pytest.param(testmodels.SmallIntFields, "smallintnum", 32767, id="small_int_fields_create"),
        pytest.param(testmodels.SmallIntFields, "smallintnum", -32768, id="small_int_fields_min"),
        pytest.param(testmodels.BigIntFields, "intnum", 9223372036854775807, id="big_int_fields_create"),
        pytest.param(testmodels.BigIntFields, "intnum", -9223372036854775808, id="big_int_fields_min"),
    ],
)
@pytest.mark.asyncio
async def test_a_created_value_reads_back_and_saves_unchanged(db, model_class, field_name, value):
    obj0 = await model_class.objects.create(**{field_name: value})
    obj = await model_class.objects.get(id=obj0.id)
    assert getattr(obj, field_name) == value
    assert type(getattr(obj, field_name)) is type(value)
    assert getattr(obj, f"{field_name}_null") is None
    await obj.save()
    obj2 = await model_class.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.parametrize(
    ("model_class", "field_name", "value", "new_value"),
    [
        pytest.param(testmodels.CharFields, "char", "moo", "ba'a", id="char_update"),
        pytest.param(testmodels.BooleanFields, "boolean", False, False, id="bool_update"),
        pytest.param(testmodels.FloatFields, "floatnum", 1.23, 2.34, id="float_update"),
        pytest.param(testmodels.IntFields, "intnum", 2147483647, 2147483646, id="int_fields_update"),
    ],
)
@pytest.mark.asyncio
async def test_an_updated_value_reads_back(db, model_class, field_name, value, new_value):
    obj0 = await model_class.objects.create(**{field_name: value})
    await model_class.objects.filter(id=obj0.id).update(**{field_name: new_value})
    obj = await model_class.objects.get(id=obj0.id)
    assert getattr(obj, field_name) == new_value
    assert type(getattr(obj, field_name)) is type(new_value)
    assert getattr(obj, f"{field_name}_null") is None


VALUE_CASES = [
    pytest.param(testmodels.CharFields, "char", "moo", id="char"),
    pytest.param(testmodels.TextFields, "text", "baa", id="text"),
    pytest.param(testmodels.BooleanFields, "boolean", True, id="bool"),
    pytest.param(testmodels.FloatFields, "floatnum", 1.23, id="float"),
    pytest.param(testmodels.IntFields, "intnum", 1, id="int_fields"),
    pytest.param(testmodels.SmallIntFields, "smallintnum", 2, id="small_int_fields"),
    pytest.param(testmodels.BigIntFields, "intnum", 1, id="big_int_fields"),
]


@pytest.mark.parametrize(("model_class", "field_name", "value"), VALUE_CASES)
@pytest.mark.asyncio
async def test_a_value_reads_back_through_values(db, model_class, field_name, value):
    obj0 = await model_class.objects.create(**{field_name: value})
    values = await model_class.objects.get(id=obj0.id).values(field_name)
    assert values[field_name] == value
    assert type(values[field_name]) is type(value)


@pytest.mark.parametrize(
    ("model_class", "field_name", "value"),
    [*VALUE_CASES, pytest.param(testmodels.BinaryFields, "binary", bytes(range(256)), id="binary")],
)
@pytest.mark.asyncio
async def test_a_value_reads_back_through_values_list(db, model_class, field_name, value):
    obj0 = await model_class.objects.create(**{field_name: value})
    values = await model_class.objects.get(id=obj0.id).values_list(field_name, flat=True)
    assert values == value
    assert type(values) is type(value)


@pytest.mark.parametrize(
    ("model_class", "field_name", "value", "step", "expected"),
    [
        pytest.param(testmodels.FloatFields, "floatnum", 1.23, 0.01, 1.24, id="float_f_expression"),
        pytest.param(testmodels.IntFields, "intnum", 1, 1, 2, id="int_fields_f_expression"),
        pytest.param(testmodels.SmallIntFields, "smallintnum", 1, 1, 2, id="small_int_fields_f_expression"),
        pytest.param(testmodels.BigIntFields, "intnum", 1, 1, 2, id="big_int_fields_f_expression"),
    ],
)
@pytest.mark.asyncio
async def test_an_f_expression_update_adds_to_the_value(db, model_class, field_name, value, step, expected):
    obj0 = await model_class.objects.create(**{field_name: value})
    await model_class.objects.filter(id=obj0.id).update(**{field_name: F(field_name) + step})
    obj1 = await model_class.objects.get(id=obj0.id)
    assert getattr(obj1, field_name) == expected


@pytest.mark.parametrize(
    ("model_class", "field_name", "value", "message"),
    [
        # A bare str is itself iterable character-by-character - from_db_value used to accept one with
        # no shape check, silently producing a plausible-looking but wrong list.
        pytest.param(
            testmodels_postgres.ArrayFields,
            "array_str",
            "hello",
            None,
            id="array_to_python_value_rejects_a_bare_string",
        ),
        # A stored value that decodes to valid JSON but isn't a dict (schema drift, a manual DB edit, a
        # row written before field_type was added) reached a bare `self.field_type(**data)` call.
        pytest.param(
            testmodels.JSONFields,
            "data_pydantic",
            "5",
            None,
            id="json_pydantic_field_type_non_dict_raises_validation_error_not_typeerror",
        ),
        # A value read straight from the DB (hand-written SQL, a since-removed enum member) went
        # through the same unwrapped enum_type(value) call to_db_value() had.
        pytest.param(
            testmodels.EnumFields,
            "service",
            999,
            "999 is not a valid",
            id="int_enum_to_python_value_invalid_value_raises_validation_error",
        ),
        pytest.param(
            testmodels.EnumFields,
            "currency",
            "XXX",
            "'XXX' is not a valid",
            id="char_enum_to_python_value_invalid_value_raises_validation_error",
        ),
        # The error message was built via value.decode() even for bytes that aren't valid utf-8 -
        # that decode itself raised UnicodeDecodeError; it falls back to repr(value).
        pytest.param(
            testmodels.JSONFields,
            "data",
            b"\xff\xfe not valid utf-8 or json",
            "is invalid json value",
            id="json_invalid_utf8_bytes_raises_validation_error_not_unicode_decode_error",
        ),
    ],
)
@pytest.mark.database_independent
def test_from_db_value_wraps_a_value_it_cannot_read(model_class, field_name, value, message):
    """A stored value a field cannot read raises the framework's own catchable ValidationError,
    not a bare TypeError/ValueError/UnicodeDecodeError."""
    field = model_class._meta.fields_map[field_name]
    with pytest.raises(ValidationError, match=message):
        field.from_db_value(value)
