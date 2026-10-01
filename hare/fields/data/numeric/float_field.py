from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.base.field import Field
from hare.fields.enums import NativeWriteCheck
from hare.sql import functions
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TFloat = TypeVar("TFloat", float, float | None)


class FloatField(Field[TFloat], float):
    """
    Float (double) field.
    """

    SQL_TYPE = "DOUBLE PRECISION"

    def get_like_text_function(self) -> Callable[[Term], Term] | None:
        # A float's text is PostgreSQL's own ``::text`` on every dialect - SQLite would write
        # ``2.0`` for a float PostgreSQL writes as ``2``.
        return functions.FloatAsText

    @overload
    def __init__(self: FloatField[float], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: FloatField[float | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    native_write_check = NativeWriteCheck.NOT_NAN

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        # SQLite binds NaN as NULL - rejected on every backend.
        if isinstance(value, float) and math.isnan(value):
            raise ValidationError(f"{self.model_field_name}: value is NaN, which SQLite silently stores as NULL")
        return value
