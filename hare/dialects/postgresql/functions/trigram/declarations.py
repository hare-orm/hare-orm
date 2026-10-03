from __future__ import annotations

from hare.dialects.postgresql.functions.trigram.trigram_function import TrigramFunction
from hare.dialects.postgresql.functions.trigram.trigram_word_similarity import TrigramWordSimilarity
from hare.utils.declared_subclass import DeclaredSubclass

TrigramSimilarity = DeclaredSubclass.make(
    TrigramFunction,
    "TrigramSimilarity",
    __package__,
    """``similarity(expression, string)`` - how similar the two texts are.""",
    function_name="similarity",
)


TrigramDistance = DeclaredSubclass.make(
    TrigramFunction,
    "TrigramDistance",
    __package__,
    """``expression <-> string`` - one minus their similarity.""",
    function_name="<->",
    is_operator=True,
)


TrigramWordDistance = DeclaredSubclass.make(
    TrigramWordSimilarity,
    "TrigramWordDistance",
    __package__,
    """``string <<-> expression`` - one minus the word similarity.""",
    function_name="<<->",
    is_operator=True,
)


TrigramStrictWordSimilarity = DeclaredSubclass.make(
    TrigramWordSimilarity,
    "TrigramStrictWordSimilarity",
    __package__,
    """``strict_word_similarity(string, expression)`` - how similar the string is to whole words of the
    text.""",
    function_name="strict_word_similarity",
)


TrigramStrictWordDistance = DeclaredSubclass.make(
    TrigramWordSimilarity,
    "TrigramStrictWordDistance",
    __package__,
    """``string <<<-> expression`` - one minus the strict word similarity.""",
    function_name="<<<->",
    is_operator=True,
)
