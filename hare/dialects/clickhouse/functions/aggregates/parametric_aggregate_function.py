from __future__ import annotations

from typing import Any

from hare.sql.functions.distinct_option_function import DistinctOptionFunction


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
