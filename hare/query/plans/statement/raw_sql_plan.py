from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.value import Value
from hare.sql.terms.parameters.parameterizer import Parameterizer


class RawSQLPlan:
    """The statement of a ``.raw()`` query of one SQL text and one set of parameter types on one
    connection: the text with the connection's placeholders, and for each placeholder the parameter
    it binds - another call with the same text and types binds its parameters into it.

    Args:
        sql: The text, rendered.
        parameter_indexes: The index of the parameter each placeholder binds, in placeholder order.
    """

    __slots__ = ("sql", "parameter_indexes", "parameterizer")

    def __init__(self, sql: str, parameter_indexes: tuple[int, ...]) -> None:
        self.sql = sql
        self.parameter_indexes = parameter_indexes
        #: Used for its should_parameterize() - the same decision rendering makes.
        self.parameterizer = Parameterizer()

    def bind(self, parameters: Sequence[Any]) -> list[Any] | None:
        """The values of the placeholders for a call's parameters.

        Args:
            parameters: The call's parameters.

        Returns:
            The values, None for a parameter the text would hold as a literal instead.
        """
        values: list[Any] = []
        conditionally_parameterized_types = Parameterizer.conditionally_parameterized_types
        for index in self.parameter_indexes:
            value = RawSQL.get_parameter_value(parameters[index])
            if isinstance(value, conditionally_parameterized_types) and not self.parameterizer.should_parameterize(
                value
            ):
                return None
            values.append(Value.get_bound_value(value))
        return values
