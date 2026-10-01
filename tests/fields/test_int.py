import pytest

from hare.contrib.test import requires_features
from hare.exceptions import ValidationError
from hare.fields.constants import INT64_MAX
from hare.query.expressions import F
from tests import testmodels

# ============================================================================
# TestIntFields
# ============================================================================


@pytest.mark.asyncio
async def test_int_fields_empty(db):
    """A missing required value now fails fast with ValidationError (MinValueValidator/
    MaxValueValidator, wired for every IntField-family field), matching CharField's own
    established test_empty - not IntegrityError from the DB's NOT NULL constraint."""
    with pytest.raises(ValidationError):
        await testmodels.IntFields.objects.create()


@pytest.mark.asyncio
async def test_int_fields_create(db):
    obj0 = await testmodels.IntFields.objects.create(intnum=2147483647)
    obj = await testmodels.IntFields.objects.get(id=obj0.id)
    assert obj.intnum == 2147483647
    assert obj.intnum_null is None

    obj2 = await testmodels.IntFields.objects.get(id=obj.id)
    assert obj == obj2

    await obj.delete()
    obj = await testmodels.IntFields.objects.filter(id=obj0.id).first()
    assert obj is None


@pytest.mark.asyncio
async def test_int_fields_update(db):
    obj0 = await testmodels.IntFields.objects.create(intnum=2147483647)
    await testmodels.IntFields.objects.filter(id=obj0.id).update(intnum=2147483646)
    obj = await testmodels.IntFields.objects.get(id=obj0.id)
    assert obj.intnum == 2147483646
    assert obj.intnum_null is None


@pytest.mark.asyncio
async def test_int_fields_min(db):
    obj0 = await testmodels.IntFields.objects.create(intnum=-2147483648)
    obj = await testmodels.IntFields.objects.get(id=obj0.id)
    assert obj.intnum == -2147483648
    assert obj.intnum_null is None

    obj2 = await testmodels.IntFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_int_fields_cast(db):
    obj0 = await testmodels.IntFields.objects.create(intnum="3")
    obj = await testmodels.IntFields.objects.get(id=obj0.id)
    assert obj.intnum == 3


@pytest.mark.asyncio
async def test_int_fields_values(db):
    obj0 = await testmodels.IntFields.objects.create(intnum=1)
    values = await testmodels.IntFields.objects.get(id=obj0.id).values("intnum")
    assert values["intnum"] == 1


@pytest.mark.asyncio
async def test_int_fields_values_list(db):
    obj0 = await testmodels.IntFields.objects.create(intnum=1)
    values = await testmodels.IntFields.objects.get(id=obj0.id).values_list("intnum", flat=True)
    assert values == 1


@pytest.mark.asyncio
async def test_int_fields_f_expression(db):
    obj0 = await testmodels.IntFields.objects.create(intnum=1)
    await type(obj0).objects.filter(id=obj0.id).update(intnum=F("intnum") + 1)
    obj1 = await testmodels.IntFields.objects.get(id=obj0.id)
    assert obj1.intnum == 2


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_int_fields_f_expression_out_of_range_raises_validation_error(db):
    """SQLite has no fixed-width integer column type, so an F()-expression's arithmetic used to
    write a value beyond IntField's 32-bit bound silently instead of raising - unlike Postgres,
    where the column's own INTEGER type already rejects it (just with a raw driver error, not
    this ValidationError)."""
    obj0 = await testmodels.IntFields.objects.create(intnum=100)
    with pytest.raises(ValidationError):
        await testmodels.IntFields.objects.filter(id=obj0.id).update(intnum=F("intnum") + 2**31)
    obj1 = await testmodels.IntFields.objects.get(id=obj0.id)
    assert obj1.intnum == 100


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
async def test_int_fields_save_with_f_expression_out_of_range_raises_validation_error(db):
    """Same gap as the QuerySet.update() case above, for the single-row Model.save() path
    (instance.field = F(...); await instance.save())."""
    obj = await testmodels.IntFields.objects.create(intnum=100)
    obj.intnum = F("intnum") + 2**31
    with pytest.raises(ValidationError):
        await obj.save()
    fresh = await testmodels.IntFields.objects.get(id=obj.id)
    assert fresh.intnum == 100


# ============================================================================
# TestSmallIntFields
# ============================================================================


@pytest.mark.asyncio
async def test_small_int_fields_empty(db):
    with pytest.raises(ValidationError):
        await testmodels.SmallIntFields.objects.create()


@pytest.mark.asyncio
async def test_small_int_fields_create(db):
    obj0 = await testmodels.SmallIntFields.objects.create(smallintnum=32767)
    obj = await testmodels.SmallIntFields.objects.get(id=obj0.id)
    assert obj.smallintnum == 32767
    assert obj.smallintnum_null is None
    await obj.save()
    obj2 = await testmodels.SmallIntFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_small_int_fields_min(db):
    obj0 = await testmodels.SmallIntFields.objects.create(smallintnum=-32768)
    obj = await testmodels.SmallIntFields.objects.get(id=obj0.id)
    assert obj.smallintnum == -32768
    assert obj.smallintnum_null is None
    await obj.save()
    obj2 = await testmodels.SmallIntFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_small_int_fields_values(db):
    obj0 = await testmodels.SmallIntFields.objects.create(smallintnum=2)
    values = await testmodels.SmallIntFields.objects.get(id=obj0.id).values("smallintnum")
    assert values["smallintnum"] == 2


@pytest.mark.asyncio
async def test_small_int_fields_values_list(db):
    obj0 = await testmodels.SmallIntFields.objects.create(smallintnum=2)
    values = await testmodels.SmallIntFields.objects.get(id=obj0.id).values_list("smallintnum", flat=True)
    assert values == 2


@pytest.mark.asyncio
async def test_small_int_fields_f_expression(db):
    obj0 = await testmodels.SmallIntFields.objects.create(smallintnum=1)
    await type(obj0).objects.filter(id=obj0.id).update(smallintnum=F("smallintnum") + 1)
    obj1 = await testmodels.SmallIntFields.objects.get(id=obj0.id)
    assert obj1.smallintnum == 2


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_small_int_fields_f_expression_out_of_range_raises_validation_error(db):
    """SQLite has no fixed-width integer column type, so an F()-expression's arithmetic used to
    write an out-of-range value silently instead of raising - unlike Postgres, where the
    column's own SMALLINT type already rejects it (just with a raw driver error, not this
    ValidationError)."""
    obj0 = await testmodels.SmallIntFields.objects.create(smallintnum=100)
    with pytest.raises(ValidationError):
        await testmodels.SmallIntFields.objects.filter(id=obj0.id).update(smallintnum=F("smallintnum") + 40000)
    obj1 = await testmodels.SmallIntFields.objects.get(id=obj0.id)
    assert obj1.smallintnum == 100


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_small_int_fields_f_expression_out_of_range_rolls_back_whole_update(db):
    """A single `.update()` can match several rows in one statement - Postgres rejects the whole
    statement (no partial write) the moment ANY row's computed value overflows the column's own
    SMALLINT type, so this must too, not just the one row that actually overflows."""
    in_range = await testmodels.SmallIntFields.objects.create(smallintnum=1)
    overflowing = await testmodels.SmallIntFields.objects.create(smallintnum=32767)
    with pytest.raises(ValidationError):
        await testmodels.SmallIntFields.objects.filter(id__in=[in_range.id, overflowing.id]).update(
            smallintnum=F("smallintnum") + 1
        )
    refreshed_in_range = await testmodels.SmallIntFields.objects.get(id=in_range.id)
    refreshed_overflowing = await testmodels.SmallIntFields.objects.get(id=overflowing.id)
    assert refreshed_in_range.smallintnum == 1
    assert refreshed_overflowing.smallintnum == 32767


# ============================================================================
# TestBigIntFields
# ============================================================================


@pytest.mark.asyncio
async def test_big_int_fields_empty(db):
    with pytest.raises(ValidationError):
        await testmodels.BigIntFields.objects.create()


@pytest.mark.asyncio
async def test_big_int_fields_create(db):
    obj0 = await testmodels.BigIntFields.objects.create(intnum=9223372036854775807)
    obj = await testmodels.BigIntFields.objects.get(id=obj0.id)
    assert obj.intnum == 9223372036854775807
    assert obj.intnum_null is None
    await obj.save()
    obj2 = await testmodels.BigIntFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_big_int_fields_min(db):
    obj0 = await testmodels.BigIntFields.objects.create(intnum=-9223372036854775808)
    obj = await testmodels.BigIntFields.objects.get(id=obj0.id)
    assert obj.intnum == -9223372036854775808
    assert obj.intnum_null is None
    await obj.save()
    obj2 = await testmodels.BigIntFields.objects.get(id=obj.id)
    assert obj == obj2


@pytest.mark.asyncio
async def test_big_int_fields_cast(db):
    obj0 = await testmodels.BigIntFields.objects.create(intnum="3")
    obj = await testmodels.BigIntFields.objects.get(id=obj0.id)
    assert obj.intnum == 3


@pytest.mark.asyncio
async def test_big_int_fields_values(db):
    obj0 = await testmodels.BigIntFields.objects.create(intnum=1)
    values = await testmodels.BigIntFields.objects.get(id=obj0.id).values("intnum")
    assert values["intnum"] == 1


@pytest.mark.asyncio
async def test_big_int_fields_values_list(db):
    obj0 = await testmodels.BigIntFields.objects.create(intnum=1)
    values = await testmodels.BigIntFields.objects.get(id=obj0.id).values_list("intnum", flat=True)
    assert values == 1


@pytest.mark.asyncio
async def test_big_int_fields_f_expression(db):
    obj0 = await testmodels.BigIntFields.objects.create(intnum=1)
    await type(obj0).objects.filter(id=obj0.id).update(intnum=F("intnum") + 1)
    obj1 = await testmodels.BigIntFields.objects.get(id=obj0.id)
    assert obj1.intnum == 2


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_big_int_fields_f_expression_out_of_range_raises_validation_error(db):
    """SQLite silently promotes 64-bit integer arithmetic overflow to a lossy float instead of
    raising - an F()-expression used to write that float straight back into the column instead
    of raising, unlike Postgres, where the column's own BIGINT type already rejects it."""
    obj0 = await testmodels.BigIntFields.objects.create(intnum=INT64_MAX - 10)
    with pytest.raises(ValidationError):
        await testmodels.BigIntFields.objects.filter(id=obj0.id).update(intnum=F("intnum") + 100)
    obj1 = await testmodels.BigIntFields.objects.get(id=obj0.id)
    assert obj1.intnum == INT64_MAX - 10


# ============================================================================
# Positive*IntField - constraints (ge/le) enforcement
# ============================================================================


def test_positive_int_field_rejects_negative_value():
    """Positive*IntField's `constraints` property (ge=0) is purely declarative metadata consumed
    by the pydantic/OpenAPI layer - the underlying SQL type doesn't enforce "positive" on its
    own, so without its own Validator (the same pattern CharField already uses for
    constraints.max_length via MaxLengthValidator), a negative value was silently accepted."""
    from hare import fields

    with pytest.raises(ValidationError):
        fields.PositiveIntField().to_db_value(-1, None)
    fields.PositiveIntField().to_db_value(0, None)  # boundary: 0 is still valid


def test_positive_small_int_field_rejects_negative_value():
    from hare import fields

    with pytest.raises(ValidationError):
        fields.PositiveSmallIntField().to_db_value(-1, None)


def test_positive_big_int_field_rejects_negative_value():
    from hare import fields

    with pytest.raises(ValidationError):
        fields.PositiveBigIntField().to_db_value(-1, None)


def test_int_field_rejects_out_of_range_value():
    """IntField itself (not just the Positive* variants) declares ge=INT32_MIN/le=INT32_MAX in
    its own constraints property - same gap, now fixed the same way."""
    from hare import fields

    with pytest.raises(ValidationError):
        fields.IntField().to_db_value(2**31, None)
    with pytest.raises(ValidationError):
        fields.IntField().to_db_value(-(2**31) - 1, None)
    fields.IntField().to_db_value(2**31 - 1, None)  # boundary: INT32_MAX is still valid


def test_base_field_to_db_value_wraps_type_coercion_failure():
    """The base Field.to_db_value's `value = self.field_type(value)` coercion call (used as-is
    by IntField/CharField/FloatField/BooleanField/BinaryField/DecimalField - every field that
    doesn't override to_db_value) used to let a raw exception straight from the constructor
    escape unwrapped (e.g. int("not-a-number") -> a bare ValueError) instead of the framework's
    own catchable ValidationError every other validation failure raises - the same bug class
    already fixed for IntEnumFieldInstance/CharEnumFieldInstance/UUIDField's own constructor
    calls."""
    from hare import fields

    with pytest.raises(ValidationError, match="invalid literal for int"):
        fields.IntField().to_db_value("not-a-number", None)


def test_bool_value_to_db_value_becomes_a_real_int():
    """bool is a subclass of int, so base Field.to_db_value()'s isinstance check used to treat
    a Python bool as already the right type and leave it untouched - SQLite/asyncpg happen to
    accept a raw bool as an INTEGER parameter, masking this on those two backends, but rust_pg's
    stricter binding rejected it outright with a raw type-mismatch error (\"column ... is of type
    integer but expression is of type boolean\"). Checked at the to_db_value() level (not a full
    DB round-trip) because SQLite's own driver re-types the value back to a plain int on read
    regardless of what was actually bound, which would hide a regression here."""
    field = testmodels.IntFields._meta.fields_map["intnum"]
    inst = testmodels.IntFields(intnum=1, intnum_null=None)
    result = field.to_db_value(True, inst)
    assert result == 1
    assert type(result) is int


@pytest.mark.asyncio
async def test_bool_value_round_trips(db):
    obj = await testmodels.IntFields.objects.create(intnum=True, intnum_null=False)
    reread = await testmodels.IntFields.objects.get(pk=obj.pk)
    assert reread.intnum == 1
    assert reread.intnum_null == 0
