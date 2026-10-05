"""A literal compared with or written to a column through ``Value(...)``, or combined with a
numeric column in arithmetic, is converted and validated by that column's field - a malformed
one fails as a ``ValidationError`` hiding a sensitive value, never as a driver error echoing it."""

from decimal import Decimal

import pytest

from hare.exceptions import ValidationError
from hare.query.expressions import F, Value
from tests.fields.models_write_paths import WritePathRecord

SECRET = "ECHOSECRET42"


@pytest.mark.asyncio
async def test_value_literal_is_converted_by_the_filtered_and_updated_field(db_write_paths):
    record = await WritePathRecord.objects.create(secret_number=5, number=7)
    assert await WritePathRecord.objects.filter(secret_number=Value("5")).count() == 1
    assert await WritePathRecord.objects.filter(number__gt=Value(Decimal("6"))).count() == 1
    await WritePathRecord.objects.filter(id=record.id).update(secret_number=Value("9"))
    assert await WritePathRecord.objects.filter(id=record.id).values_list("secret_number", flat=True) == [9]
    assert await WritePathRecord.objects.filter(number=Value(None)).count() == 0


@pytest.mark.asyncio
async def test_text_literal_in_arithmetic_is_converted_by_the_numeric_column(db_write_paths):
    record = await WritePathRecord.objects.create(secret_number=5, number=7)
    await WritePathRecord.objects.filter(id=record.id).update(secret_number=F("secret_number") + "2")
    assert await WritePathRecord.objects.filter(id=record.id).values_list("secret_number", flat=True) == [7]
    assert await WritePathRecord.objects.filter(number=Value("3") + F("secret_number") - 3).count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_query",
    [
        lambda: WritePathRecord.objects.filter(secret_number=Value(SECRET)).first(),
        lambda: WritePathRecord.objects.filter(secret_number__gte=Value(SECRET)).first(),
        lambda: WritePathRecord.objects.all().update(secret_number=Value(SECRET)),
        lambda: WritePathRecord.objects.filter(secret_number=F("secret_number") + Value(SECRET)).first(),
        lambda: WritePathRecord.objects.all().update(secret_number=F("secret_number") + SECRET),
        lambda: WritePathRecord.objects.all().update(secret_number=SECRET - F("secret_number")),
    ],
)
async def test_malformed_literal_for_a_sensitive_column_is_a_masked_validation_error(db_write_paths, make_query):
    await WritePathRecord.objects.create(secret_number=5)
    with pytest.raises(ValidationError, match="secret_number: .*<hidden>") as error_info:
        await make_query()
    assert SECRET not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None
