"""``Field.validate()`` of a value of exactly the field's type checks it natively with the same
outcome and error as its validators."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.fields.field import Field
from hare.fields.validators.limits.max_length_validator import MaxLengthValidator
from hare.fields.validators.limits.max_value_validator import MaxValueValidator
from hare.fields.validators.limits.min_length_validator import MinLengthValidator
from hare.fields.validators.limits.min_value_validator import MinValueValidator
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

pytestmark = pytest.mark.skipif(HydrateAccelerator.module is None, reason="rust.native isn't built")


def make_field(field: Field[Any], name: str) -> Field[Any]:
    field.model_field_name = name
    return field


def validate(field: Field[Any], value: Any, *, native: bool) -> Exception | None:
    field.value_checks = None if native else (field.validators, len(field.validators), None)
    try:
        field.validate(value)
    except Exception as error:  # noqa: BLE001 - the error is what is compared.
        return error
    finally:
        field.value_checks = None
    return None


CASES: list[tuple[Field[Any], list[Any]]] = [
    (make_field(fields.IntField(), "count"), [0, 1, -(2**31), 2**31 - 1, -(2**31) - 1, 2**31, 2**70]),
    (make_field(fields.SmallIntField(), "small"), [0, 32767, 32768, -32769]),
    (
        make_field(fields.IntField(validators=[MinValueValidator(3), MaxValueValidator(9)]), "bounded"),
        [2, 3, 9, 10],
    ),
    (
        make_field(fields.FloatField(validators=[MinValueValidator(0.5)]), "ratio"),
        [0.4, 0.5, float("inf"), float("nan")],
    ),
    (make_field(fields.CharField(max_length=3), "code"), ["", "abc", "abcd", "ab\x00"]),
    (
        make_field(fields.TextField(validators=[MinLengthValidator(2), MaxLengthValidator(4)]), "word"),
        ["a", "ab", "abcd", "abcde"],
    ),
]


@pytest.mark.parametrize(("field", "values"), CASES, ids=[field.model_field_name for field, _ in CASES])
def test_native_checks_match_the_validators(field: Field[Any], values: list[Any]) -> None:
    for value in values:
        native = validate(field, value, native=True)
        reference = validate(field, value, native=False)
        assert type(native) is type(reference), value
        assert str(native) == str(reference), value
        if reference is not None:
            assert str(native.__cause__) == str(reference.__cause__), value


def test_the_checks_are_native() -> None:
    field = make_field(fields.CharField(max_length=3), "native")
    field.validate("abc")
    assert field.value_checks is not None
    assert field.value_checks[2] is not None


def test_a_validator_added_later_is_checked() -> None:
    field = make_field(fields.IntField(), "late")
    assert validate(field, 5, native=True) is None
    field.validators.append(MaxValueValidator(4))
    assert str(validate(field, 5, native=True)) == str(validate(field, 5, native=False))
    assert validate(field, 5, native=True) is not None
