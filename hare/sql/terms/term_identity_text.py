from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class TermIdentityText:
    """The text a term with no SQL in a dialect is written as where the text only names and
    compares it (``SqlContext.names_dialect_terms``) - its class and each setting, so two terms
    differing in any of them differ in it too. Never run."""

    #: The attributes a term's identity leaves out - how it is written, not what it computes.
    IGNORED_ATTRIBUTES = frozenset({"alias", "schema"})

    @staticmethod
    def get_sql(term: Term, sql_context: SqlContext) -> str:
        """The term's identity text.

        Args:
            term: The term.
            sql_context: The context the term is written in.

        Returns:
            ``ClassName(setting=value, ...)``, each nested term by its own SQL.
        """
        settings = [
            f"{name}={text}"
            for name, value in sorted(vars(term).items())
            if not name.startswith("_") and name not in TermIdentityText.IGNORED_ATTRIBUTES
            if (text := TermIdentityText.get_value_text(value, sql_context)) is not None
        ]
        return f"{type(term).__name__}({', '.join(settings)})"

    @staticmethod
    def get_value_text(value: Any, sql_context: SqlContext) -> str | None:
        """A setting's text - a term by its SQL, a sequence by its items, a plain value by its
        ``repr()``.

        Args:
            value: The setting.
            sql_context: The context the term is written in.

        Returns:
            The text, None for an object of no stable text (a field instance).
        """
        if isinstance(value, Enum):
            return repr(value.value)
        if value is None or isinstance(value, (bool, int, float, str)):
            return repr(value)
        if isinstance(value, (list, tuple)):
            item_texts = [TermIdentityText.get_value_text(item, sql_context) for item in value]
            return "[" + ", ".join(text for text in item_texts if text is not None) + "]"
        get_sql = getattr(value, "get_sql", None)
        if callable(get_sql):
            return str(get_sql(sql_context.copy(with_alias=False)))
        return None
