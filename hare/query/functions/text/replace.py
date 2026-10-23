from __future__ import annotations

from typing import Any

from hare.query.functions.text.text_function import TextFunction
from hare.sql.enums import CastType


class Replace(TextFunction):
    """``Replace(text, old, new="")`` - every occurrence of ``old`` replaced."""

    function_name = "REPLACE"
    arity = (2, 3)
    argument_types = (CastType.TEXT, CastType.TEXT)

    def __init__(self, *arguments: Any) -> None:
        super().__init__(*arguments, *([""] if len(arguments) == 2 else []))
