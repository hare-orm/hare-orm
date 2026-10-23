import pytest

from hare import fields
from hare.exceptions import ValidationError
from tests.shared_models import testmodels

# ============================================================================
# TestIntFields
# ============================================================================


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


# ============================================================================
# Positive*IntField - constraints (ge/le) enforcement
# ============================================================================


@pytest.mark.parametrize(
    "field_class",
    [
        pytest.param(fields.PositiveIntField, id="positive_int_field_rejects_negative_value"),
        pytest.param(fields.PositiveSmallIntField, id="positive_small_int_field_rejects_negative_value"),
        pytest.param(fields.PositiveBigIntField, id="positive_big_int_field_rejects_negative_value"),
    ],
)
@pytest.mark.database_independent
def test_a_positive_int_field_rejects_a_negative_value(field_class):
    """Positive*IntField's `constraints` property (ge=0) is purely declarative metadata consumed
    by the pydantic/OpenAPI layer - the underlying SQL type doesn't enforce "positive" on its
    own, so without its own Validator (the same pattern CharField already uses for
    constraints.max_length via MaxLengthValidator), a negative value was silently accepted."""
    with pytest.raises(ValidationError):
        field_class().to_db_value(-1, None)
    field_class().to_db_value(0, None)  # boundary: 0 is still valid


@pytest.mark.database_independent
def test_int_field_rejects_out_of_range_value():
    """IntField itself (not just the Positive* variants) declares ge=INT32_MIN/le=INT32_MAX in
    its own constraints property - same gap, now fixed the same way."""
    from hare import fields

    with pytest.raises(ValidationError):
        fields.IntField().to_db_value(2**31, None)
    with pytest.raises(ValidationError):
        fields.IntField().to_db_value(-(2**31) - 1, None)
    fields.IntField().to_db_value(2**31 - 1, None)  # boundary: INT32_MAX is still valid


@pytest.mark.database_independent
def test_a_text_read_from_the_database_that_is_no_int_raises_a_validation_error():
    """A numeric text a SQLite column holds is read as it is; one of no number raised a bare
    ValueError from int()."""
    from hare import fields

    assert fields.IntField().from_db_value("12") == 12
    with pytest.raises(ValidationError, match="invalid literal for int"):
        fields.IntField().from_db_value("not-a-number")


@pytest.mark.database_independent
def test_a_bool_given_for_an_int_is_refused():
    """bool is a subclass of int - a flag given for a number reached rust_pg as a boolean
    parameter of an integer column."""
    field = testmodels.IntFields._meta.fields_map["intnum"]
    inst = testmodels.IntFields(intnum=1, intnum_null=None)
    with pytest.raises(ValidationError, match="expected an int, got bool"):
        field.to_db_value(True, inst)


@pytest.mark.asyncio
async def test_a_bool_given_for_an_int_is_refused_on_create(db):
    with pytest.raises(ValidationError, match="expected an int, got bool"):
        await testmodels.IntFields.objects.create(intnum=True, intnum_null=False)


@pytest.mark.asyncio
async def test_an_integer_expression_reads_the_databases_own_result(db):
    """A sum of 64-bit integers divided by an integer is the database's own value - PostgreSQL's sum of
    a BIGINT is a NUMERIC, and its quotient keeps the fraction; SQLite divides integers whole."""
    from decimal import Decimal

    from hare.query.functions import Sum

    for value in (2, 3):
        await testmodels.BigIntFields.objects.create(intnum=value)
    result = await testmodels.BigIntFields.objects.aggregate(half=Sum("intnum") / 2)
    if testmodels.BigIntFields._meta.connection.dialect.name == "postgresql":
        assert result["half"] == Decimal("2.5")
    else:
        assert result["half"] == 2
