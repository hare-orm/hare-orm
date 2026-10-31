from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.core.registries import Registries
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions.expression import Expression


class DialectExpressions:
    """The expressions of one database its dialect registered (``Dialect.install()``) - a function only that
    database has. A query on a dialect that didn't register one refuses it before any SQL; an expression no
    dialect registered runs on every one."""

    #: The dialect classes that registered each expression class.
    registered: ClassVar[dict[type, frozenset[type[Dialect]]]] = {}
    #: The dialect classes each expression class has through its bases, None for an expression of every
    #: dialect - kept in a bucket of this class.
    found_dialect_classes: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)
    #: The buckets of the caches this class reads (``Cache.get_owner_bucket()``).
    cache_buckets: ClassVar[dict[int, Any]] = {}
    #: The bucket of ``found_dialect_classes`` - read on every resolution of a function; taken on first use.
    dialect_classes_by_expression_class: ClassVar[dict[type, frozenset[type[Dialect]] | None] | None] = None

    @classmethod
    def register(cls, dialect_class: type[Dialect], expression_classes: Iterable[type[Expression]]) -> None:
        """Registers the expressions only a dialect's database has - their subclasses too.

        Args:
            dialect_class: The dialect - each of its objects alike.
            expression_classes: The expression classes.
        """
        for expression_class in expression_classes:
            cls.registered[expression_class] = cls.registered.get(expression_class, frozenset()) | {dialect_class}
        # A plan built before may hold the expression - and what was found of its dialects.
        Registries.changed()

    @classmethod
    def get_dialect_classes(cls, expression_class: type) -> frozenset[type[Dialect]] | None:
        """The dialects whose database has an expression - through the class or a base of it.

        Args:
            expression_class: The expression's class.

        Returns:
            The dialect classes, None for an expression of every dialect.
        """
        found = cls.dialect_classes_by_expression_class
        if found is None:
            # Emptied in place whenever the cache drops its entries - kept for good.
            found = cls.dialect_classes_by_expression_class = cls.found_dialect_classes.get_owner_bucket(cls)
        try:
            return found[expression_class]
        except KeyError:
            pass
        dialect_classes = next(
            (cls.registered[base] for base in expression_class.__mro__ if base in cls.registered), None
        )
        found[expression_class] = dialect_classes
        return dialect_classes

    @classmethod
    def raise_if_dialect_lacks(cls, expression: Expression, dialect: Dialect) -> None:
        """Refuses an expression of another database's dialect before any SQL.

        Args:
            expression: The expression resolved.
            dialect: The dialect of the query.

        Raises:
            UnSupportedError: The dialect registered no such expression, another dialect did.
        """
        dialect_classes = cls.get_dialect_classes(type(expression))
        if dialect_classes is not None and not isinstance(dialect, tuple(dialect_classes)):
            dialect_names = ", ".join(sorted(str(dialect_class.name) for dialect_class in dialect_classes))
            raise UnSupportedError(
                f"{type(expression).__name__}() is a function of the {dialect_names} database - the query runs on "
                f"{dialect.name}"
            )
