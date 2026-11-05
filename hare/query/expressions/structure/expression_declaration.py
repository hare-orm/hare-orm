from __future__ import annotations

from typing import Any, ClassVar, Self

from hare.classes.class_path import ClassPath
from hare.query.expressions.structure.declarations import ExpressionChild
from hare.query.expressions.structure.expression_structure import ExpressionStructure


class ExpressionDeclaration:
    """Constructor-backed syntax shared by scalar and window expressions.

    The declaration records semantic constructor inputs. Rebuilding invokes the
    constructor again, so cached or normalized children never outlive their inputs.
    This protocol does not make a window function independently executable SQL.
    """

    _constructor_arguments: tuple[tuple[Any, ...], dict[str, Any]]
    structure: ClassVar[ExpressionStructure] = ExpressionStructure()

    def get_expression_children(self) -> tuple[ExpressionChild, ...]:
        """Direct constructor inputs and their field/literal/scope roles."""
        return self.structure.get_children(self)

    def rebuild_expression(self, children: tuple[ExpressionChild, ...]) -> Self:
        """Build an independent declaration from transformed inputs."""
        return self.structure.rebuild(self, children)

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        """Creates the expression and keeps the arguments it was constructed with, for
        ``deconstruct()``.

        Args:
            *args: The constructor's positional arguments.
            **kwargs: The constructor's keyword arguments.

        Returns:
            The new expression.
        """
        expression = super().__new__(cls)
        expression._constructor_arguments = (args, kwargs)
        return expression

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Returns how a migration file rebuilds the expression - its class path and the arguments
        it was constructed with - so a migration keeps the expression itself, and the database a
        migration runs on renders its own SQL for it.

        Returns:
            The path, positional arguments and keyword arguments.
        """
        args, kwargs = self.__dict__.get("_constructor_arguments", ((), {}))
        return ClassPath.get(type(self)), list(args), dict(kwargs)

    def __repr__(self) -> str:
        args, kwargs = self.__dict__.get("_constructor_arguments", ((), {}))
        arguments = [*map(repr, args), *(f"{name}={value!r}" for name, value in kwargs.items())]
        return f"{type(self).__name__}({', '.join(arguments)})"
