from __future__ import annotations

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ToTsVector(Function):
    """The ``TO_TSVECTOR`` function.

    Args:
        term: The document.
        config: Text search configuration name (e.g. "english") - Postgres's own
            ``to_tsvector([ config regconfig, ] document text)`` signature takes it as the FIRST
            argument when given, ahead of the document. Omitted (Postgres falls back to
            ``default_text_search_config``) unless explicitly passed.
    """

    def __init__(self, term: Term, config: str | Term | None = None) -> None:
        args = (config, term) if config is not None else (term,)
        super().__init__("TO_TSVECTOR", *args)
