from typing import Any

from hare.query.functions.text.text_function import TextFunction


class Replace(TextFunction):
    """``Replace(text, old, new="")`` - every occurrence of ``old`` replaced."""

    function_name = "REPLACE"
    arity = (2, 3)

    def __init__(self, *arguments: Any) -> None:
        super().__init__(*arguments, *([""] if len(arguments) == 2 else []))
