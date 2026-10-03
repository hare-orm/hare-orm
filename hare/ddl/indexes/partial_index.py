from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions import ConstraintCondition
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.query.expressions import Expression, Ordering, Q
from hare.sql.terms.base.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
from hare.ddl.indexes.index import Index


class PartialIndex(Index):
    """An index that only covers rows matching a condition (``CREATE INDEX ... WHERE``).

    Args:
        expressions: The expressions on which the index is desired.
        fields: A tuple of names of the fields on which the index is desired.
        name: The name of the index.
        condition: The rows the index covers - a ``Q`` over the model's own fields, or
            ``RawSQLTerm`` of a raw SQL predicate.

    Raises:
        ConfigurationError: The condition is neither a ``Q`` nor a ``RawSQLTerm``.
    """

    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        condition: Q | RawSQLTerm | None = None,
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        super().__init__(*expressions, fields=fields, name=name, opclasses=opclasses, unique=unique, include=include)
        if condition is not None:
            ConstraintCondition.raise_if_not_condition(condition, f"{type(self).__name__}.condition")
        self.condition = condition
        # A Q is rendered against the model, by get_extra().
        self.extra = f" WHERE ({condition.sql})" if isinstance(condition, RawSQLTerm) else ""

    def get_extra(self, model: type[Model], client: DatabaseClient) -> str:
        extra = super().get_extra(model, client)
        if not isinstance(self.condition, Q):
            return extra
        return f"{extra} WHERE ({ConstraintCondition.get_sql(self.condition, model, client)})"

    def get_name_parts(self) -> tuple[str, ...]:
        name_parts = super().get_name_parts()
        return (*name_parts, repr(self.condition)) if isinstance(self.condition, Q) else name_parts

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.condition is not None:
            kwargs["condition"] = self.condition
        return path, args, kwargs
