from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function


class PlainToTsQuery(Function):
    """The ``PLAINTO_TSQUERY`` function.

    Args:
        config: Text search configuration name - same first-argument convention as
            ``ToTsVector``.
    """

    def __init__(self, field: Term, config: str | Term | None = None) -> None:
        args = (config, field) if config is not None else (field,)
        super().__init__("PLAINTO_TSQUERY", *args)
