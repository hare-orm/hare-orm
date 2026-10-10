from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from hare.sql.exceptions import FunctionException
from hare.sql.terms.functions.function import Function


class CustomFunction:
    def __init__(self, name: str, parameters: Sequence[Any] | None = None) -> None:
        self.name = name
        self.parameters = parameters

    def __call__(self, *args: Any, **kwargs: Any) -> Function:
        if not self._has_parameters():
            # self.parameters is only ever an OPTIONAL arity-validation spec (see
            # _is_valid_function_call below) - omitting it must not also drop the call's
            # actual arguments, or CustomFunction("MYFUNC")(1, 2, 3) silently renders "MYFUNC()".
            return Function(self.name, *args, alias=kwargs.get("alias"))

        if not self._is_valid_function_call(*args):
            raise FunctionException(
                "Function {name} require these arguments ({parameters}), ({args}) passed".format(
                    name=self.name,
                    parameters=", ".join(str(parameter) for parameter in cast("Sequence[Any]", self.parameters)),
                    args=", ".join(str(parameter) for parameter in args),
                )
            )

        return Function(self.name, *args, alias=kwargs.get("alias"))

    def _has_parameters(self) -> bool:
        return self.parameters is not None

    def _is_valid_function_call(self, *args: Any) -> bool:
        return len(args) == len(cast("Sequence[Any]", self.parameters))
