from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.sql.exceptions import FunctionException
from hare.sql.terms.functions.function import Function


class CustomFunction:
    """A SQL function by its name and its parameters - calling it gives the function term."""

    def __init__(self, name: str, parameters: Sequence[Any] | None = None) -> None:
        self.name = name
        self.parameters = parameters

    def __call__(self, *args: Any, **kwargs: Any) -> Function:
        if self.parameters is not None and len(args) != len(self.parameters):
            raise FunctionException(
                "Function {name} require these arguments ({parameters}), ({args}) passed".format(
                    name=self.name,
                    parameters=", ".join(str(parameter) for parameter in self.parameters),
                    args=", ".join(str(parameter) for parameter in args),
                )
            )

        return Function(self.name, *args, alias=kwargs.get("alias"))
