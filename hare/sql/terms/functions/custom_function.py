from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.sql.exceptions import FunctionException

if TYPE_CHECKING:
    pass
from hare.sql.terms.functions.function import Function


class CustomFunction:
    def __init__(self, name: str, params: Sequence[Any] | None = None) -> None:
        self.name = name
        self.params = params

    def __call__(self, *args: Any, **kwargs: Any) -> Function:
        if not self._has_params():
            # self.params is only ever an OPTIONAL arity-validation spec (see
            # _is_valid_function_call below) - omitting it must not also drop the call's
            # actual arguments, or CustomFunction("MYFUNC")(1, 2, 3) silently renders "MYFUNC()".
            return Function(self.name, *args, alias=kwargs.get("alias"))

        if not self._is_valid_function_call(*args):
            raise FunctionException(
                "Function {name} require these arguments ({params}), ({args}) passed".format(
                    name=self.name,
                    params=", ".join(str(p) for p in cast("Sequence[Any]", self.params)),
                    args=", ".join(str(p) for p in args),
                )
            )

        return Function(self.name, *args, alias=kwargs.get("alias"))

    def _has_params(self) -> bool:
        return self.params is not None

    def _is_valid_function_call(self, *args: Any) -> bool:
        return len(args) == len(cast("Sequence[Any]", self.params))
