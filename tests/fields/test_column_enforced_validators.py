"""The validators a field's own column type enforces - an ``update()`` setting an ``F()`` expression
re-checks in Python only the rest, and needs no RETURNING or transaction when none is left."""

from decimal import Decimal

import pytest

from hare import fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ValidationError
from hare.fields.validators import MinValueValidator
from hare.query.expressions import F
from tests import testmodels

POSTGRESQL = DialectRegistry.get_dialect("postgresql")
SQLITE = DialectRegistry.get_dialect("sqlite")


@pytest.mark.parametrize(
    "field",
    [
        fields.IntField(),
        fields.SmallIntField(),
        fields.BigIntField(),
        fields.DecimalField(max_digits=10, decimal_places=2),
    ],
    ids=["int", "smallint", "bigint", "decimal"],
)
def test_the_column_type_enforces_every_automatic_validator(field):
    assert field.get_validators_not_enforced_by_column(POSTGRESQL) == []
    assert field.get_validators_not_enforced_by_column(SQLITE) == field.validators


@pytest.mark.parametrize(
    "field", [fields.PositiveIntField(), fields.PositiveSmallIntField(), fields.PositiveBigIntField()]
)
def test_a_positive_field_keeps_its_lower_bound(field):
    remaining = field.get_validators_not_enforced_by_column(POSTGRESQL)
    assert len(remaining) == 1
    assert isinstance(remaining[0], MinValueValidator)


def test_a_user_validator_is_never_enforced_by_the_column():
    def reject_odd(value):
        if value % 2:
            raise ValidationError("odd")

    field = fields.IntField(validators=[reject_odd])
    assert field.get_validators_not_enforced_by_column(POSTGRESQL) == [reject_odd]


def test_a_copied_field_keeps_what_its_column_enforces():
    from copy import copy

    field = copy(fields.PositiveIntField())
    remaining = field.get_validators_not_enforced_by_column(POSTGRESQL)
    assert len(remaining) == 1
    assert isinstance(remaining[0], MinValueValidator)


@pytest.mark.asyncio
async def test_expression_update_validates_only_what_the_column_does_not(db):
    queryset = testmodels.IntFields.objects.filter(intnum=1)
    update = queryset.update(intnum=F("intnum") + 1)
    update._apply_connection(update.get_connection(for_write=True))
    update._make_query()
    if update.dialect.features.enforces_numeric_ranges:
        assert update._written_value_check.validated_columns == []
        assert update._written_value_check.converted_columns == []
    else:
        assert [column for column, _field in update._written_value_check.converted_columns] == ["intnum"]


@pytest.mark.asyncio
async def test_expression_update_still_writes_in_range_values(db):
    row = await testmodels.IntFields.objects.create(intnum=1)
    decimal_row = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.5"), decimal_nodec=Decimal(1))
    assert await testmodels.IntFields.objects.filter(id=row.id).update(intnum=F("intnum") + 41) == 1
    assert await testmodels.DecimalFields.objects.filter(id=decimal_row.id).update(decimal=F("decimal") * 2) == 1
    assert (await testmodels.IntFields.objects.get(id=row.id)).intnum == 42
    assert (await testmodels.DecimalFields.objects.get(id=decimal_row.id)).decimal == Decimal("3.0000")
