from __future__ import annotations

from typing import Any, ClassVar

from hare.sql.terms.functions.function import Function


class ContainerFunction(Function):
    """A function of container values with no standard SQL - each dialect writes it
    (``TermRenderers``), a dialect without a renderer for it raises ``UnSupportedError``.

    Args:
        args: The function's arguments - the container first.
        alias: The alias.
    """

    requires_dialect_renderer: ClassVar[bool] = True
    #: The function's name in hare - a renderer is found by the term's class, not by this name.
    function_name: ClassVar[str]

    def __init__(self, *args: Any, alias: str | None = None) -> None:
        super().__init__(self.function_name, *args, alias=alias)
