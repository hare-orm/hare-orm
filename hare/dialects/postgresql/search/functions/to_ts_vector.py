from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function


class ToTsVector(Function):
    """The ``TO_TSVECTOR`` function.

    Args:
        config: Text search configuration name (e.g. "english") - Postgres's own
            ``to_tsvector([ config regconfig, ] document text)`` signature takes it as the FIRST
            argument when given, ahead of the document. Omitted (Postgres falls back to
            ``default_text_search_config``) unless explicitly passed.
    """

    def __init__(self, field: Term, config: str | Term | None = None) -> None:
        args = (config, field) if config is not None else (field,)
        super().__init__("TO_TSVECTOR", *args)
