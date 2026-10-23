from __future__ import annotations

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class PlainToTsQuery(Function):
    """The ``PLAINTO_TSQUERY`` function.

    Args:
        term: The text of the query.
        config: Text search configuration name - same first-argument convention as
            ``ToTsVector``.
    """

    def __init__(self, term: Term, config: str | Term | None = None) -> None:
        args = (config, term) if config is not None else (term,)
        super().__init__("PLAINTO_TSQUERY", *args)
