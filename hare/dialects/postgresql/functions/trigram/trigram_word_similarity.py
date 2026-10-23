from __future__ import annotations

from hare.dialects.postgresql.functions.trigram.trigram_function import TrigramFunction
from hare.query.expressions import Expression


class TrigramWordSimilarity(TrigramFunction):
    """``word_similarity(string, expression)`` - how similar the string is to a part of the text.

    Args:
        string: The string compared with the text.
        expression: The field name or text expression.
    """

    function_name = "word_similarity"
    string_first = True

    def __init__(self, string: str, expression: str | Expression) -> None:
        super().__init__(expression, string)
