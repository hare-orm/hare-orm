"""Assignment shortcuts preserve canonical built-ins without suppressing custom converters."""

import datetime
import ipaddress
from typing import Any
from uuid import UUID

import pytest

from hare import Model, fields
from hare.dialects.clickhouse.fields.low_cardinality_field import LowCardinalityField
from tests.utils.model_bindings import ModelBindings


class IncrementingIntField(fields.IntField):
    """A visible non-idempotent assignment step, separate from database decoding."""

    def to_python(self, value: Any) -> Any:
        value = super().to_python(value)
        return None if value is None else value + 1

    def from_db_value(self, value: Any) -> Any:
        return None if value is None else int(value)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "declaration,value",
    [
        (fields.IntField(), 7),
        (fields.FloatField(), 1.25),
        (fields.BooleanField(), True),
        (fields.BinaryField(), b"text"),
        (fields.CharField(max_length=30), "text"),
        (fields.TextField(), "text"),
        (fields.DateField(), datetime.date(2024, 1, 2)),
        (fields.TimeDeltaField(), datetime.timedelta(seconds=7)),
        (fields.UUIDField(), UUID(int=1)),
        (fields.IPAddressField(), ipaddress.IPv4Address("192.0.2.1")),
    ],
)
def test_declared_assignment_shortcuts_keep_canonical_values(declaration, value):
    assert type(value) in declaration.get_assign_normalized_types()
    assert declaration.to_python(value) is value


@pytest.mark.database_independent
@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "low-cardinality"])
def test_assignment_defaults_mapping_and_clone_do_not_require_an_idempotent_converter(wrapped):
    declaration = IncrementingIntField(default=3)
    if wrapped:
        declaration = LowCardinalityField(declaration, default=3)

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)
        value = declaration
        callable_value = IncrementingIntField(default=lambda: 3)

    prepared_models = ModelBindings.prepare(Book)

    explicit = prepared_models.new(Book, number=1, value=7)
    assert explicit.value == 8
    assert explicit.callable_value == 4
    explicit.update_from_dict({"value": 9})
    assert explicit.value == 10
    copied = explicit.clone(pk=2)
    assert copied.value == 10
    assert copied.callable_value == 4
    defaulted = prepared_models.new(Book, number=3)
    assert defaulted.value == 4
    assert defaulted.callable_value == 4
