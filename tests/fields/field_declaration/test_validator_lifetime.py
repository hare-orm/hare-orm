"""Field validators remain effective after mutation and copying."""

import copy

import pytest

from hare import fields
from hare.fields.validators.limits.max_value_validator import MaxValueValidator

pytestmark = pytest.mark.database_independent


def test_a_validator_added_later_is_checked():
    field = fields.IntField()
    field.model_field_name = "late"
    field.validate(5)
    field.validators.append(MaxValueValidator(4))
    with pytest.raises(ValueError, match="late"):
        field.validate(5)
    field.validate(4)


@pytest.mark.parametrize("copy_field", [copy.copy, copy.deepcopy], ids=["copy", "deepcopy"])
def test_a_validated_field_keeps_its_validators_after_copying(copy_field):
    field = fields.CharField(max_length=3)
    field.model_field_name = "copied"
    field.validate("abc")
    copied = copy_field(field)
    assert copied is not field
    copied.validate("abc")
    with pytest.raises(ValueError, match="copied"):
        copied.validate("abcd")
