from typing import Any

from hare.query.functions.text.text_function import TextFunction


class RPad(TextFunction):
    """``RPad(text, length, fill=" ")`` - filled on the right to ``length``, a longer text cut to it."""

    function_name = "RPAD"
    arity = (2, 3)

    def __init__(self, *arguments: Any) -> None:
        super().__init__(*arguments, *([" "] if len(arguments) == 2 else []))
