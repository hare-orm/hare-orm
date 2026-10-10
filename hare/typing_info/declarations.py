"""What the typing tools know of a value - declarations only."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.query.enums import LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class ValueType:
    """The type of a value a query takes or gives, described apart from any type checker - the mypy
    plugin makes a mypy type of it, ``hare stubs`` the text of a stub.

    Attributes:
        classes: The classes of the value, a union - empty for any value. A tuple among them is the
            tuple of a composite key's values.
        declared_in: The model class and its attribute declaring the value's type - a checker
            reading the class takes the declared type (``JSONField[MyDict]``) over ``classes``;
            None for none.
        accepts_enum_values: Whether an enum field takes the value of a member too.
        models: Models an instance of which is taken too - the related model of a relation.
        literals: Strings the value is one of - the branch names of a generic relation; when given,
            the type is their union alone.
        nullable: Whether None is taken or given.
        shape: One value, an iterable or a two-item range of them.
        accepts_expressions: Whether an expression, a term or a subquery is taken too.
        is_any: Whether any value is taken - every other attribute is then ignored.
    """

    classes: tuple[Any, ...] = ()
    declared_in: tuple[type[Model], str] | None = None
    accepts_enum_values: bool = False
    models: tuple[type[Model], ...] = ()
    literals: tuple[str, ...] = ()
    nullable: bool = False
    shape: LookupValueShape = LookupValueShape.VALUE
    accepts_expressions: bool = False
    is_any: bool = False
