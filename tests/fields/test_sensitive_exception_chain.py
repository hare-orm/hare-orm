"""A ValidationError for a ``sensitive=True`` field keeps the value out of its message and out of
its exception chain - the original exception (whose text names the value) is neither
``__cause__`` nor ``__context__``, so logging/Sentry/OpenTelemetry tracebacks don't show it."""

import traceback
from collections.abc import Callable
from enum import IntEnum, StrEnum
from typing import Any

import pytest
from pydantic import BaseModel

from hare import fields
from hare.dialects.postgresql.fields.ranges import IntRangeField
from hare.exceptions import ValidationError
from hare.fields import ArrayField, Field
from hare.fields.validators import MaxLengthValidator

SECRET = "SECRET4111"
SECRET_NUMBER = 4111


class Color(StrEnum):
    RED = "red"


class Level(IntEnum):
    LOW = 1


class Card(BaseModel):
    number: int


def named_field(field: Field[Any]) -> Field[Any]:
    field.model_field_name = "secret"
    return field


def get_error_chain(error: BaseException) -> list[BaseException]:
    chain = []
    pending: BaseException | None = error
    while pending is not None and pending not in chain:
        chain.append(pending)
        pending = pending.__cause__ or pending.__context__
    return chain


SENSITIVE_CONVERSIONS: list[tuple[str, Callable[[], Any]]] = [
    ("int", lambda: named_field(fields.IntField(sensitive=True)).to_db_value(SECRET, None)),
    ("int-read", lambda: named_field(fields.IntField(sensitive=True)).from_db_value(SECRET)),
    ("char-length", lambda: named_field(fields.CharField(max_length=4, sensitive=True)).to_db_value(SECRET, None)),
    (
        "validator",
        lambda: named_field(fields.TextField(sensitive=True, validators=[MaxLengthValidator(4)])).to_db_value(
            SECRET, None
        ),
    ),
    ("datetime", lambda: named_field(fields.DatetimeField(sensitive=True)).from_db_value(SECRET)),
    ("date", lambda: named_field(fields.DateField(sensitive=True)).from_db_value(f"2020-01-01{SECRET}")),
    ("char-enum", lambda: named_field(fields.CharEnumField(Color, sensitive=True)).to_db_value(SECRET, None)),
    ("char-enum-read", lambda: named_field(fields.CharEnumField(Color, sensitive=True)).from_db_value(SECRET)),
    ("int-enum", lambda: named_field(fields.IntEnumField(Level, sensitive=True)).to_db_value(SECRET_NUMBER, None)),
    ("int-enum-read", lambda: named_field(fields.IntEnumField(Level, sensitive=True)).from_db_value(SECRET_NUMBER)),
    ("json-read", lambda: named_field(fields.JSONField(sensitive=True)).from_db_value(f'{{"card": "{SECRET}"')),
    (
        "json-declared-type",
        lambda: named_field(fields.JSONField(field_type=Card, sensitive=True)).to_db_value({"number": SECRET}, None),
    ),
    ("timedelta", lambda: named_field(fields.TimeDeltaField(sensitive=True)).get_timedelta(SECRET_NUMBER * 10**20)),
    (
        "array-element",
        lambda: named_field(ArrayField(fields.CharField(max_length=4), sensitive=True)).to_db_value([SECRET], None),
    ),
    ("range-bound", lambda: named_field(IntRangeField(sensitive=True)).to_db_value((SECRET, 5), None)),
    ("range-text", lambda: named_field(IntRangeField(sensitive=True)).from_db_value(f"[{SECRET},5)")),
]


@pytest.mark.parametrize(("case", "convert"), SENSITIVE_CONVERSIONS, ids=[case for case, _ in SENSITIVE_CONVERSIONS])
def test_sensitive_validation_error_has_no_chained_exception(case, convert):
    with pytest.raises(ValidationError) as error_info:
        convert()
    error = error_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "4111" not in "".join(traceback.format_exception(error))
    assert all("4111" not in str(chained) for chained in get_error_chain(error))


def test_non_sensitive_validation_error_keeps_its_cause():
    with pytest.raises(ValidationError) as error_info:
        named_field(fields.IntField()).to_db_value(SECRET, None)
    assert isinstance(error_info.value.__cause__, ValueError)
    assert SECRET in str(error_info.value)
