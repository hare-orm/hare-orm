from decimal import Decimal
from enum import Enum, IntEnum

import pytest

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields import CharEnumField, IntEnumField
from tests.shared_models import testmodels


class BadIntEnum1(IntEnum):
    python_programming = 32768
    database_design = 2
    system_administration = 3


class BadIntEnum2(IntEnum):
    python_programming = -32769
    database_design = 2
    system_administration = 3


class BadIntEnumIfGenerated(IntEnum):
    python_programming = -1
    database_design = 2
    system_administration = 3


# ============================================================================
# TestIntEnumFields
# ============================================================================


@pytest.mark.asyncio
async def test_int_enum_create(db):
    obj0 = await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration)
    assert isinstance(obj0.service, testmodels.Service)
    obj = await testmodels.EnumFields.objects.get(id=obj0.id)
    assert isinstance(obj.service, testmodels.Service)
    assert obj.service == testmodels.Service.system_administration
    await obj.save()
    obj2 = await testmodels.EnumFields.objects.get(id=obj.id)
    assert obj == obj2

    await obj.delete()
    obj = await testmodels.EnumFields.objects.filter(id=obj0.id).first()
    assert obj is None

    obj3 = await testmodels.EnumFields.objects.create(service=3)
    assert isinstance(obj3.service, testmodels.Service)
    with pytest.raises(ValidationError):
        await testmodels.EnumFields.objects.create(service=4)


@pytest.mark.asyncio
async def test_int_enum_update(db):
    obj0 = await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration)
    await testmodels.EnumFields.objects.filter(id=obj0.id).update(service=testmodels.Service.database_design)
    obj = await testmodels.EnumFields.objects.get(id=obj0.id)
    assert obj.service == testmodels.Service.database_design

    await testmodels.EnumFields.objects.filter(id=obj0.id).update(service=2)
    obj = await testmodels.EnumFields.objects.get(id=obj0.id)
    assert obj.service == testmodels.Service.database_design
    with pytest.raises(ValidationError):
        await testmodels.EnumFields.objects.filter(id=obj0.id).update(service=4)


@pytest.mark.asyncio
async def test_int_enum_values(db):
    obj0 = await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration)
    values = await testmodels.EnumFields.objects.get(id=obj0.id).values("service")
    assert values["service"] == testmodels.Service.system_administration

    obj1 = await testmodels.EnumFields.objects.create(service=3)
    values = await testmodels.EnumFields.objects.get(id=obj1.id).values("service")
    assert values["service"] == testmodels.Service.system_administration


@pytest.mark.asyncio
async def test_int_enum_values_list(db):
    obj0 = await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration)
    values = await testmodels.EnumFields.objects.get(id=obj0.id).values_list("service", flat=True)
    assert values == testmodels.Service.system_administration

    obj1 = await testmodels.EnumFields.objects.create(service=3)
    values = await testmodels.EnumFields.objects.get(id=obj1.id).values_list("service", flat=True)
    assert values == testmodels.Service.system_administration


@pytest.mark.database_independent
def test_int_enum_char_fails():
    with pytest.raises(ConfigurationError, match="IntEnumField takes an enum of integers"):
        IntEnumField(testmodels.Currency)


@pytest.mark.database_independent
def test_int_enum_range1_fails():
    with pytest.raises(ConfigurationError, match="IntEnumField values are -32768 to 32767"):
        IntEnumField(BadIntEnum1)


@pytest.mark.database_independent
def test_int_enum_range2_fails():
    with pytest.raises(ConfigurationError, match="IntEnumField values are -32768 to 32767"):
        IntEnumField(BadIntEnum2)


@pytest.mark.database_independent
def test_int_enum_range3_generated_fails():
    with pytest.raises(ConfigurationError, match="IntEnumField values are 1 to 32767"):
        IntEnumField(BadIntEnumIfGenerated, generated=True)


@pytest.mark.database_independent
def test_int_enum_range3_manual():
    fld = IntEnumField(BadIntEnumIfGenerated)
    assert fld.enum_type is BadIntEnumIfGenerated


@pytest.mark.database_independent
def test_int_enum_auto_description():
    fld = IntEnumField(testmodels.Service)
    assert fld.description == "python_programming: 1\ndatabase_design: 2\nsystem_administration: 3"


@pytest.mark.parametrize(
    ("make_field", "attribute_name", "expected"),
    [
        pytest.param(
            lambda: IntEnumField(testmodels.Service, description="foo"),
            "description",
            "foo",
            id="int_enum_manual_description",
        ),
        pytest.param(
            lambda: CharEnumField(testmodels.Currency, max_length=5), "max_length", 5, id="char_enum_defined_maxlen"
        ),
        pytest.param(
            lambda: CharEnumField(testmodels.Currency, description="baa"),
            "description",
            "baa",
            id="char_enum_manual_description",
        ),
    ],
)
@pytest.mark.database_independent
def test_an_enum_field_keeps_a_given_option(make_field, attribute_name, expected):
    assert getattr(make_field(), attribute_name) == expected


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("field_name", "value", "error_match"),
    [
        # An int not belonging to the enum's members used to raise a raw ValueError straight from
        # the enum_type(value) constructor call in to_db_value() - the same way a bare
        # `Service(999)` would - instead of the framework's own ValidationError every other field
        # validation failure raises. Wraps the conversion so it's a normal, catchable
        # ValidationError like everything else, consistent with CharEnumFieldInstance's own now-fixed
        # behavior right below.
        pytest.param("service", 999, "999 is not a valid", id="int_enum_invalid_value_raises_validation_error"),
        # A string that isn't one of the enum's members but happens to be a VALID length (so it
        # passes validate()'s inherited CharField max_length check, unlike an over-length string)
        # used to raise a raw ValueError straight from the enum_type(value) constructor call in
        # to_db_value() - the same underlying bug as IntEnumFieldInstance above, just masked there
        # more often by the length check happening to catch most invalid inputs first. "XXX" is 3
        # chars, the same length as every real Currency member (HUF/EUR/USD), so it passes length
        # validation and reaches the enum conversion.
        pytest.param("currency", "XXX", "'XXX' is not a valid", id="char_enum_invalid_value_raises_validation_error"),
        # CharEnumFieldInstance.to_db_value() calls self.validate(value) on the RAW value first,
        # before its own isinstance(value, self.enum_type)/Enum/str branches run - unlike plain
        # CharField/TextField, which always coerce to str(value) before validate() ever sees it (see
        # Field.to_db_value()). An int value therefore reaches MaxLengthValidator (wired in via
        # CharField.__init__) completely unconverted. MaxLengthValidator/MinLengthValidator used to
        # call len(value) with no type check first, unlike NumericValidator._validate_type(), so this
        # raised a raw, uncaught TypeError instead of the framework's own catchable ValidationError
        # every other validation failure raises.
        pytest.param(
            "currency", 123, "123 is not a valid Currency", id="char_enum_non_string_value_raises_validation_error"
        ),
    ],
)
def test_enum_value_outside_the_enum_raises_validation_error(field_name, value, error_match):
    field = testmodels.EnumFields._meta.fields_map[field_name]
    with pytest.raises(ValidationError, match=error_match):
        field.to_db_value(value, testmodels.EnumFields)


# ============================================================================
# TestCharEnumFields
# ============================================================================


@pytest.mark.asyncio
async def test_char_enum_create(db):
    obj0 = await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration)
    assert isinstance(obj0.currency, testmodels.Currency)
    obj = await testmodels.EnumFields.objects.get(id=obj0.id)
    assert isinstance(obj.currency, testmodels.Currency)
    assert obj.currency == testmodels.Currency.HUF
    await obj.save()
    obj2 = await testmodels.EnumFields.objects.get(id=obj.id)
    assert obj == obj2

    await obj.delete()
    obj = await testmodels.EnumFields.objects.filter(id=obj0.id).first()
    assert obj is None

    obj0 = await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration, currency="USD")
    assert isinstance(obj0.currency, testmodels.Currency)
    with pytest.raises(ValidationError):
        await testmodels.EnumFields.objects.create(service=testmodels.Service.system_administration, currency="XXX")


@pytest.mark.asyncio
async def test_char_enum_update(db):
    obj0 = await testmodels.EnumFields.objects.create(
        service=testmodels.Service.system_administration, currency=testmodels.Currency.HUF
    )
    await testmodels.EnumFields.objects.filter(id=obj0.id).update(currency=testmodels.Currency.EUR)
    obj = await testmodels.EnumFields.objects.get(id=obj0.id)
    assert obj.currency == testmodels.Currency.EUR

    await testmodels.EnumFields.objects.filter(id=obj0.id).update(currency="USD")
    obj = await testmodels.EnumFields.objects.get(id=obj0.id)
    assert obj.currency == testmodels.Currency.USD
    with pytest.raises(ValidationError):
        await testmodels.EnumFields.objects.filter(id=obj0.id).update(currency="XXX")


@pytest.mark.asyncio
async def test_char_enum_values(db):
    obj0 = await testmodels.EnumFields.objects.create(
        service=testmodels.Service.system_administration, currency=testmodels.Currency.EUR
    )
    values = await testmodels.EnumFields.objects.get(id=obj0.id).values("currency")
    assert values["currency"] == testmodels.Currency.EUR

    obj1 = await testmodels.EnumFields.objects.create(service=3, currency="EUR")
    values = await testmodels.EnumFields.objects.get(id=obj1.id).values("currency")
    assert values["currency"] == testmodels.Currency.EUR


@pytest.mark.asyncio
async def test_char_enum_values_list(db):
    obj0 = await testmodels.EnumFields.objects.create(
        service=testmodels.Service.system_administration, currency=testmodels.Currency.EUR
    )
    values = await testmodels.EnumFields.objects.get(id=obj0.id).values_list("currency", flat=True)
    assert values == testmodels.Currency.EUR

    obj1 = await testmodels.EnumFields.objects.create(service=3, currency="EUR")
    values = await testmodels.EnumFields.objects.get(id=obj1.id).values_list("currency", flat=True)
    assert values == testmodels.Currency.EUR


@pytest.mark.database_independent
def test_char_enum_auto_maxlen():
    fld = CharEnumField(testmodels.Currency)
    assert fld.max_length == 3


@pytest.mark.database_independent
def test_char_enum_auto_description():
    fld = CharEnumField(testmodels.Currency)
    assert fld.description == "HUF: HUF\nEUR: EUR\nUSD: USD"


class UnrelatedStrEnum(Enum):
    #: Same length as every real Currency member (HUF/EUR/USD) - passes validate()'s inherited
    #: CharField max_length check, unlike a longer value, which validate() already catches on its
    #: own (see the length-mismatch case this deliberately avoids).
    NOT_A_CURRENCY = "XXX"


@pytest.mark.database_independent
def test_char_enum_foreign_enum_member_silently_accepted_now_raises():
    """to_db_value() checked `isinstance(value, Enum)` - true for a member of ANY Enum class, not
    just this field's own enum_type - and wrote `value.value` straight to the DB with no
    membership check at all, unlike IntEnumFieldInstance.to_db_value's own explicit
    isinstance(value, self.enum_type) vs isinstance(value, IntEnum) distinction just above it.
    self.validate() doesn't catch this either - Field.validate()'s own Enum handling only checks
    value.value's STRING LENGTH via MaxLengthValidator, never enum-type membership. A member of
    an unrelated Enum class used to be silently written to the DB with no error at the point of
    the actual mistake, only failing later - confusingly - on the next read."""
    field = testmodels.EnumFields._meta.fields_map["currency"]
    with pytest.raises(ValidationError, match="'XXX' is not a valid"):
        field.to_db_value(UnrelatedStrEnum.NOT_A_CURRENCY, testmodels.EnumFields)


class IntValuedEnum(Enum):
    ONE = 1
    TWO = 2


@pytest.mark.database_independent
def test_char_enum_with_non_string_enum_values_stores_their_text():
    """A CharEnumField over an Enum with int values stores str(member.value) and reads it back
    by that text - the member itself used to fail MaxLengthValidator on save."""
    field = CharEnumField(IntValuedEnum)
    field.model_field_name = "number"

    assert field.to_db_value(IntValuedEnum.ONE, testmodels.EnumFields) == "1"
    assert field.to_db_value(2, testmodels.EnumFields) == "2"
    assert field.to_db_value("2", testmodels.EnumFields) == "2"
    assert field.from_db_value("1") is IntValuedEnum.ONE
    with pytest.raises(ValidationError):
        field.to_db_value("3", testmodels.EnumFields)


@pytest.mark.database_independent
def test_an_enum_field_of_no_enum_is_refused():
    """A class that is no enum was iterated, raising a TypeError far from the declaration."""
    with pytest.raises(ConfigurationError, match="CharEnumField takes an enum class, got <class 'int'>"):
        CharEnumField(int)
    with pytest.raises(ConfigurationError, match="IntEnumField takes an enum class, got 'colors'"):
        IntEnumField("colors")


@pytest.mark.database_independent
def test_a_char_enum_field_too_short_for_its_values_is_refused():
    """The column was made of the given length, and the longer members failed at the first write."""
    with pytest.raises(ConfigurationError, match="max_length=2 is shorter than the longest value of UnrelatedStrEnum"):
        CharEnumField(UnrelatedStrEnum, max_length=2)
    longest = max(len(member.value) for member in UnrelatedStrEnum)
    assert CharEnumField(UnrelatedStrEnum, max_length=longest).max_length == longest


@pytest.mark.database_independent
def test_a_char_enum_field_default_of_no_member_is_refused():
    member = next(iter(UnrelatedStrEnum))
    with pytest.raises(ConfigurationError, match="default 'no such value' is no member or value of UnrelatedStrEnum"):
        CharEnumField(UnrelatedStrEnum, default="no such value")
    assert CharEnumField(UnrelatedStrEnum, default=member).default is member
    assert CharEnumField(UnrelatedStrEnum, default=member.value).default == member.value


@pytest.mark.parametrize("value", [1.5, Decimal("2.5"), float("inf"), Decimal("NaN")])
@pytest.mark.asyncio
async def test_a_number_of_no_member_is_refused_in_a_write_and_a_filter(db, value):
    """1.5 was written to the integer column, which rounds it; ClickHouse's filter failed with
    the enum's own ValueError while PostgreSQL's matched nothing."""
    with pytest.raises(ValidationError, match="expected an int"):
        await testmodels.EnumFields.objects.filter(service=value).count()
    with pytest.raises(ValidationError, match="expected an int"):
        await testmodels.EnumFields.objects.create(service=value)


@pytest.mark.database_independent
def test_a_value_of_no_number_names_its_type():
    """The message read "Value must be a numeric value and is required" for a text given."""
    with pytest.raises(ValidationError, match="expected an int, got str"):
        IntEnumField(testmodels.Service).to_db_value("1", testmodels.EnumFields)
