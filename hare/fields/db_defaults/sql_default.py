from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.constants import SQL_DIALECT

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class SqlDefault:
    """A raw SQL expression as a ``db_default`` - written into ``generate_schemas()`` and migrations as
    is; a dialect writing it differently registers a renderer. Never build it from untrusted input.
    Prefer ``Now``/``RandomHex`` for the common expressions.

    Example::

        class MyModel(Model):
            counter = fields.IntField(db_default=SqlDefault("0"))
    """

    def __init__(self, sql: str) -> None:
        self.sql = sql

    def get_sql(self, dialect: Dialect = SQL_DIALECT) -> str:
        """Returns the default's SQL for a dialect's ``DEFAULT`` clause.

        Args:
            dialect: The dialect of the database the DDL runs on; plain SQL when not given.

        Returns:
            The SQL - the dialect's renderer's for this default's class, the standard SQL
            (``get_standard_sql``) when the dialect has none.
        """
        renderer = dialect.renderers.get(type(self))
        if renderer is None:
            return self.get_standard_sql()
        return renderer(self, dialect.sql_context)

    def get_standard_sql(self) -> str:
        """Returns the default's SQL on a dialect that writes it as standard SQL.

        Returns:
            The expression as given.
        """
        return self.sql

    @staticmethod
    def is_parenthesized(sql: str) -> bool:
        """Whether one pair of parentheses encloses the whole expression.

        Args:
            sql: The expression.

        Returns:
            True for ``(a + b)``, False for ``(a) + (b)``.
        """
        text = sql.strip()
        if not text.startswith("("):
            return False
        depth = 0
        quote = None
        for index, character in enumerate(text):
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in "'\"":
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return index == len(text) - 1
        return False

    def __repr__(self) -> str:
        return f"SqlDefault({self.sql!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SqlDefault) and self.sql == other.sql

    def __hash__(self) -> int:
        return hash(self.sql)
