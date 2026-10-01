from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function


class TsQueryFunction(Function):
    """A tsquery-building function call (``PLAINTO_TSQUERY(config, value)`` etc.), remembering its
    text search configuration.

    Args:
        name: The SQL function name.
        value: The query text term.
        config_term: The configuration term, when given.
    """

    def __init__(self, name: str, value: Term, config_term: Term | None = None) -> None:
        args = (config_term, value) if config_term is not None else (value,)
        super().__init__(name, *args)
        self.config_term = config_term
