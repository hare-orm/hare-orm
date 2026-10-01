from enum import Enum, IntEnum

import pytest

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields import CharEnumField, IntEnumField
from tests import testmodels


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
async def test_int_enum_empty(db):
    """A missing required int-family value now fails fast with ValidationError (Field.__init__
    wires MinValueValidator/MaxValueValidator for every IntField-family field), matching
    CharField's own established test_empty (MaxLengthValidator already rejects None the same
    way) - not IntegrityError from the DB's NOT NULL constraint, reached only once it was too
    late to give a clear error."""
    with pytest.raises(ValidationError):
        await testmodels.EnumFields.objects.create()


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


def test_int_enum_char_fails():
    with pytest.raises(ConfigurationError, match="IntEnumField only supports integer enums!"):
        IntEnumField(testmodels.Currency)


def test_int_enum_range1_fails():
    with pytest.raises(ConfigurationError, match="The valid range of IntEnumField's values is -32768..32767!"):
        IntEnumField(BadIntEnum1)


def test_int_enum_range2_fails():
    with pytest.raises(ConfigurationError, match="The valid range of IntEnumField's values is -32768..32767!"):
        IntEnumField(BadIntEnum2)


def test_int_enum_range3_generated_fails():
    with pytest.raises(ConfigurationError, match="The valid range of IntEnumField's values is 1..32767!"):
        IntEnumField(BadIntEnumIfGenerated, generated=True)


def test_int_enum_range3_manual():
    fld = IntEnumField(BadIntEnumIfGenerated)
    assert fld.enum_type is BadIntEnumIfGenerated


def test_int_enum_auto_description():
    fld = IntEnumField(testmodels.Service)
    assert fld.description == "python_programming: 1\ndatabase_design: 2\nsystem_administration: 3"


def test_int_enum_manual_description():
    fld = IntEnumField(testmodels.Service, description="foo")
    assert fld.description == "foo"


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


def test_int_enum_to_python_value_invalid_value_raises_validation_error():
    """from_db_value() - the path a value read straight from the DB (bypassing the ORM's own
    writes, e.g. hand-written SQL or a since-removed enum member) goes through - had the same
    unwrapped enum_type(value) call as to_db_value() above, but was never fixed alongside it."""
    field = testmodels.EnumFields._meta.fields_map["service"]
    with pytest.raises(ValidationError, match="999 is not a valid"):
        field.from_db_value(999)


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


def test_char_enum_auto_maxlen():
    fld = CharEnumField(testmodels.Currency)
    assert fld.max_length == 3


def test_char_enum_defined_maxlen():
    fld = CharEnumField(testmodels.Currency, max_length=5)
    assert fld.max_length == 5


def test_char_enum_auto_description():
    fld = CharEnumField(testmodels.Currency)
    assert fld.description == "HUF: HUF\nEUR: EUR\nUSD: USD"


def test_char_enum_manual_description():
    fld = CharEnumField(testmodels.Currency, description="baa")
    assert fld.description == "baa"


class UnrelatedStrEnum(Enum):
    #: Same length as every real Currency member (HUF/EUR/USD) - passes validate()'s inherited
    #: CharField max_length check, unlike a longer value, which validate() already catches on its
    #: own (see the length-mismatch case this deliberately avoids).
    NOT_A_CURRENCY = "XXX"


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


def test_char_enum_to_python_value_invalid_value_raises_validation_error():
    """from_db_value() had the same unwrapped enum_type(value) call as to_db_value() above,
    but was never fixed alongside it - see test_int_enum_to_python_value_invalid_value_raises_
    validation_error's own docstring for the read-path scenario this covers."""
    field = testmodels.EnumFields._meta.fields_map["currency"]
    with pytest.raises(ValidationError, match="'XXX' is not a valid"):
        field.from_db_value("XXX")


class IntValuedEnum(Enum):
    ONE = 1
    TWO = 2


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
