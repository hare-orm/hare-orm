from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class ParametricAnalyticFunction(WindowFrameAnalyticFunction):
    """A ClickHouse aggregate with parameters computed over a window -
    ``name(parameters)(arguments) OVER (...)``.

    Args:
        name: The function.
        parameters: Its parameters - numbers, written into the SQL text.
        *args: Its arguments.
    """

    def __init__(self, name: str, parameters: tuple[Any, ...], *args: Any) -> None:
        super().__init__(name, *args)
        self.parameters = parameters
