from decimal import Decimal

import pytest

from hare.exceptions import ValidationError
from hare.fields.validators import MaxLengthValidator, MaxValueValidator, MinLengthValidator, MinValueValidator


class CountedMinValueValidator(MinValueValidator):
    """Counts the ``_validate_type`` calls of a value check."""

    def __init__(self, min_value: int) -> None:
        self.type_checks = 0
        super().__init__(min_value)
        self.type_checks = 0

    def _validate_type(self, value: object) -> None:
        self.type_checks += 1
        super()._validate_type(value)


class CountedMaxLengthValidator(MaxLengthValidator):
    """Counts the ``_validate_type`` calls of a length check."""

    def __init__(self, max_length: int) -> None:
        self.type_checks = 0
        super().__init__(max_length)
        self.type_checks = 0

    def _validate_type(self, value: object) -> None:
        self.type_checks += 1
        super()._validate_type(value)


class LabelledInt(int):
    pass


class LabelledText(str):
    pass


@pytest.mark.database_independent
@pytest.mark.parametrize("value", [5, 5.0, Decimal(5)])
def test_value_validator_takes_exact_number_types_without_the_type_check(value):
    validator = CountedMinValueValidator(1)

    validator(value)

    assert validator.type_checks == 0


@pytest.mark.database_independent
@pytest.mark.parametrize("value", [True, LabelledInt(5)])
def test_value_validator_checks_the_type_of_a_number_subclass(value):
    validator = CountedMinValueValidator(0)

    validator(value)

    assert validator.type_checks == 1


@pytest.mark.database_independent
@pytest.mark.parametrize("validator", [MinValueValidator(1), MaxValueValidator(10)])
def test_value_validator_rejects_none_and_non_numbers_with_unchanged_messages(validator):
    with pytest.raises(ValidationError, match="Value must not be None"):
        validator(None)
    with pytest.raises(ValidationError, match="Value must be a number, got a str"):
        validator("5")


@pytest.mark.database_independent
@pytest.mark.parametrize("value", ["abc", ["a"], ("a",), b"abc"])
def test_length_validator_takes_exact_sized_types_without_the_type_check(value):
    validator = CountedMaxLengthValidator(5)

    validator(value)

    assert validator.type_checks == 0


@pytest.mark.database_independent
def test_length_validator_checks_the_type_of_a_text_subclass():
    validator = CountedMaxLengthValidator(5)

    validator(LabelledText("abc"))

    assert validator.type_checks == 1


@pytest.mark.database_independent
@pytest.mark.parametrize("validator", [MinLengthValidator(1), MaxLengthValidator(10)])
def test_length_validator_rejects_none_and_unsized_values_with_unchanged_messages(validator):
    with pytest.raises(ValidationError, match="Value must not be None"):
        validator(None)
    with pytest.raises(ValidationError, match="Value must be a string, list, tuple, or bytes"):
        validator(5)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("validator", "value", "passes"),
    [
        (MinValueValidator(0), 2**70, True),
        (MinValueValidator(0), -(2**70), False),
        (MaxValueValidator(2**70), 2**70, True),
        (MaxValueValidator(2**70), 2**70 + 1, False),
        (MinValueValidator(Decimal("0.5")), 1, True),
        (MinValueValidator(Decimal("0.5")), 0, False),
        (MaxValueValidator(1.5), Decimal("1.5"), True),
        (MinValueValidator(0), float("nan"), True),
    ],
)
def test_value_validator_compares_as_python_compares(validator, value, passes):
    if passes:
        validator(value)
    else:
        with pytest.raises(ValidationError):
            validator(value)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (Decimal("123.45"), None),
        (Decimal("0"), None),
        (Decimal("1E+2"), None),
        (Decimal("0.001"), "has 3 decimal places, more than decimal_places=2"),
        (Decimal("1234.56"), "has 6 digits, more than max_digits=5"),
        (Decimal("1234"), "has 4 digits before the decimal point, more than 3 allowed"),
        (Decimal("NaN"), "is not a finite decimal"),
    ],
)
def test_digits_validator_counts_digits_natively(value, message):
    from hare.fields.validators import MaxDigitsValidator

    validator = MaxDigitsValidator(5, 2)
    if message is None:
        validator(value)
    else:
        with pytest.raises(ValidationError, match=message):
            validator(value)


@pytest.mark.database_independent
def test_pattern_validators_match_through_the_compiled_pattern():
    from hare.fields.validators import RegexValidator, SlugValidator

    RegexValidator(r"^(?!-)[a-z-]+", 0)("abc")
    with pytest.raises(ValidationError, match="does not match regex"):
        RegexValidator(r"^(?!-)[a-z-]+", 0)("-abc")
    with pytest.raises(TypeError):
        RegexValidator(r"^a", 0)(5)
    SlugValidator()("a-slug")
    with pytest.raises(ValidationError):
        SlugValidator()(5)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "validator",
    [MinValueValidator(1), MaxLengthValidator(3), MinLengthValidator(1)],
)
def test_validators_with_native_checks_copy_and_pickle(validator):
    import copy
    import pickle

    for copied in (copy.deepcopy(validator), pickle.loads(pickle.dumps(validator))):
        assert copied.check is not validator.check
        with pytest.raises(ValidationError):
            copied(
                0
                if isinstance(validator, MinValueValidator)
                else ("long" if isinstance(validator, MaxLengthValidator) else "")
            )
