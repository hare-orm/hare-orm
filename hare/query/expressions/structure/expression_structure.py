from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from hare.query.expressions.structure.declarations import ExpressionChild
from hare.query.expressions.structure.enums import ArgumentRole

if TYPE_CHECKING:
    from hare.query.expressions.structure.expression_declaration import ExpressionDeclaration


TExpression = TypeVar("TExpression", bound="ExpressionDeclaration")


@dataclass(frozen=True, slots=True)
class ExpressionStructure:
    """Constructor positions with semantic roles, independent of parameter or SQL values.

    Unlisted arguments may contain child expressions; plain strings there remain literals.
    A field role marks strings as references, also inside a collection. Literal expressions
    are opaque. Query boundaries explicitly separate their inner source scope.
    """

    positions: tuple[tuple[int, ArgumentRole], ...] = ()
    keywords: tuple[tuple[str, ArgumentRole], ...] = ()
    positional_role: ArgumentRole = ArgumentRole.VALUE
    keyword_role: ArgumentRole = ArgumentRole.VALUE
    opaque: bool = False
    query_scope: bool = False

    def get_children(self, expression: TExpression) -> tuple[ExpressionChild, ...]:
        if self.opaque:
            return ()
        _path, args, kwargs = expression.deconstruct()
        positions, keywords = dict(self.positions), dict(self.keywords)
        return (
            *(
                ExpressionChild(index, value, positions.get(index, self.positional_role))
                for index, value in enumerate(args)
            ),
            *(ExpressionChild(name, value, keywords.get(name, self.keyword_role)) for name, value in kwargs.items()),
        )

    @staticmethod
    def rebuild(expression: TExpression, children: tuple[ExpressionChild, ...]) -> TExpression:
        """Reconstruct through the public constructor so normalized derived attributes agree."""
        _path, args, kwargs = expression.deconstruct()
        args, kwargs = list(args), dict(kwargs)
        for child in children:
            if isinstance(child.argument, int):
                args[child.argument] = child.value
            else:
                kwargs[child.argument] = child.value
        return type(expression)(*args, **kwargs)
