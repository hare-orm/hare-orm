from __future__ import annotations

from typing import Any

from hare.query.functions.text.text_function import TextFunction
from hare.sql.enums import CastType


class LPad(TextFunction):
    """``LPad(text, length, fill=" ")`` - filled on the left to ``length``, a longer text cut to it."""

    function_name = "LPAD"
    arity = (2, 3)
    argument_types = (CastType.INTEGER, CastType.TEXT)

    def __init__(self, *arguments: Any) -> None:
        super().__init__(*arguments, *([" "] if len(arguments) == 2 else []))
