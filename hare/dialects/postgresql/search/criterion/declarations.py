from hare.sql.enums import Comparator
from hare.sql.terms.base.infix_operator import InfixOperator


class Comp(Comparator):
    """hare.sql comparator for Postgres full-text search's ``@@`` match operator."""

    search = " @@ "


class TsInfixOperator(InfixOperator):
    """A text search operator between two tsvectors or tsqueries (``||``, ``&&``)."""
