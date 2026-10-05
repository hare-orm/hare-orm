from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.functions.distinct_option_function import DistinctOptionFunction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext


class ParametricAggregateFunction(DistinctOptionFunction):
    """A ClickHouse aggregate with parameters - ``name(parameters)(arguments)``, as
    ``quantile(0.5)(price)``; without parameters ``name(arguments)``.

    Args:
        name: The function.
        parameters: Its parameters - numbers, written into the SQL text.
        *args: Its arguments.
        alias: The alias.
    """

    def __init__(self, name: str, parameters: tuple[Any, ...], *args: Any, alias: str | None = None) -> None:
        super().__init__(name, *args, alias=alias)
        self.parameters = parameters

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return self.get_parametric_sql(self, super().get_function_sql(sql_context), sql_context)

    @staticmethod
    def get_parametric_sql(function: Any, function_sql: str, sql_context: SqlContext) -> str:
        """A function's SQL with its parameters after its name.

        Args:
            function: The function - its ``parameters`` written.
            function_sql: Its SQL without them - ``name(arguments)...``.
            sql_context: The context it renders in.

        Returns:
            ``name(parameters)(arguments)...``; ``function_sql`` for no parameters.
        """
        if not function.parameters:
            return function_sql
        name_length = len(function.get_dialect_special_name(sql_context) or function.name)
        parameters_sql = ",".join(repr(parameter) for parameter in function.parameters)
        return f"{function_sql[:name_length]}({parameters_sql}){function_sql[name_length:]}"
