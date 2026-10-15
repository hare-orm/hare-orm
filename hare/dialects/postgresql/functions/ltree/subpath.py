from __future__ import annotations

from typing import Any

from hare.exceptions import ValidationError
from hare.query.expressions import (
    F,
    Function as HareFunction,
)
from hare.sql.terms.term import Term


class Subpath(HareFunction):
    """``subpath(path, offset[, length])`` - a part of an ``LtreeField`` path, read as a path: the
    labels from 0-based ``offset`` (negative from the end), ``length`` of them or up to the end
    (a negative ``length`` leaves that many off the end).

    Example: ``Category.objects.annotate(top=Subpath("path", 0, 1)).values("top")``

    Args:
        field: The ltree field - a name, ``F()`` or an expression.
        offset: The first label's position.
        length: How many labels, None for up to the end.

    Raises:
        ValidationError: ``offset`` or ``length`` isn't an ``int``.
    """

    function_name = "subpath"
    populate_field_object = True

    def __init__(self, field: str | F | HareFunction | Term, offset: int, length: int | None = None) -> None:
        arguments: list[Any] = [self.get_validated_int("offset", offset)]
        if length is not None:
            arguments.append(self.get_validated_int("length", length))
        super().__init__(field, *arguments)

    @staticmethod
    def get_validated_int(name: str, value: Any) -> int:
        """An ``int`` argument.

        Args:
            name: The argument's name, for the error message.
            value: The argument.

        Returns:
            The argument.

        Raises:
            ValidationError: ``value`` isn't an ``int``.
        """
        if type(value) is not int:
            raise ValidationError(f"Subpath {name} must be an int, got {value!r}")
        return value
