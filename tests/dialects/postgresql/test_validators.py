"""KeysValidator and RangeMinValueValidator / RangeMaxValueValidator."""

import datetime

import pytest

from hare.dialects.postgresql.fields.hstore import HStoreField
from hare.dialects.postgresql.fields.ranges import DateRangeField, IntRangeField, Range
from hare.dialects.postgresql.validators import KeysValidator, RangeMaxValueValidator, RangeMinValueValidator
from hare.exceptions import ValidationError


def test_keys_validator():
    validator = KeysValidator(["color", "size"])
    validator({"color": "red", "size": "L", "extra": "x"})
    with pytest.raises(ValidationError, match="Some keys were missing: size"):
        validator({"color": "red"})
    strict = KeysValidator(["color"], strict=True)
    strict({"color": "red"})
    with pytest.raises(ValidationError, match="Some unknown keys were provided: extra"):
        strict({"color": "red", "extra": "x"})
    with pytest.raises(ValidationError, match="custom"):
        KeysValidator(["a"], message="custom")({})
    with pytest.raises(ValidationError, match="mapping"):
        validator(["color"])


def test_range_bound_validators():
    at_most_ten = RangeMaxValueValidator(10)
    at_most_ten(Range(1, 10))
    at_most_ten((1, 5))
    at_most_ten(Range(is_empty=True))
    with pytest.raises(ValidationError, match="upper bound of the range is not greater than 10"):
        at_most_ten(Range(1, 11))
    with pytest.raises(ValidationError):
        at_most_ten(Range(1, None))

    from_two = RangeMinValueValidator(2)
    from_two(Range(2, 5))
    with pytest.raises(ValidationError, match="lower bound of the range is not less than 2"):
        from_two(Range(1, 5))
    with pytest.raises(ValidationError):
        from_two(Range(None, 5))
    with pytest.raises(ValidationError, match="Range"):
        from_two("1-5")


def test_validators_run_on_the_field_value():
    attributes = HStoreField(validators=[KeysValidator(["color"])])
    attributes.model_field_name = "attributes"
    attributes.to_db_value({"color": "red"}, None)
    with pytest.raises(ValidationError, match="missing: color"):
        attributes.to_db_value({"size": "L"}, None)

    span = IntRangeField(validators=[RangeMaxValueValidator(10)])
    span.model_field_name = "span"
    span.validate(Range(1, 5))
    with pytest.raises(ValidationError, match="not greater than 10"):
        span.validate(Range(1, 20))

    days = DateRangeField(validators=[RangeMinValueValidator(datetime.date(2024, 1, 1))])
    days.model_field_name = "days"
    days.validate(Range(datetime.date(2024, 1, 2), datetime.date(2024, 2, 1)))
    with pytest.raises(ValidationError):
        days.validate(Range(datetime.date(2023, 12, 31), datetime.date(2024, 2, 1)))
