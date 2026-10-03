from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.ddl.conditions import ConstraintCondition
from hare.ddl.enums import ExclusionConstraintUsing
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError
from hare.utils.class_path import ClassPath

if TYPE_CHECKING:
    from hare.query.expressions import Q


@dataclass(frozen=True)
class ExclusionConstraint:
    """Postgres ``EXCLUDE USING <using> (...)`` - rejects a row whose values overlap an existing row's
    under the given operators: ``ExclusionConstraint(name=..., expressions=(("resource", "="),
    ("during", "&&")), using="gist")`` rejects two bookings of one resource with overlapping ranges.
    Needs ``Dialect.supports_exclusion_constraints``. Not tenant-aware: add the tenant column to
    ``expressions`` (with ``"="``) to check per tenant.

    Args:
        expressions: ``(expression, operator)`` pairs - a field name (a relation means its key
            column) or a ``RawSQLTerm`` used as written.
        using: The index access method - see ExclusionConstraintUsing.
        condition: Makes it partial - a ``Q`` over the model's fields or a ``RawSQLTerm`` predicate.
        include: Fields stored in the index as non-key columns (``INCLUDE``).
        deferrable: Check at transaction end (or after ``SET CONSTRAINTS ... DEFERRED``) instead of
            after each statement.
        initially_deferred: Only with ``deferrable=True`` - every transaction starts deferred.

    Raises:
        ConfigurationError: ``initially_deferred=True`` without ``deferrable=True``, or the
            condition is neither a ``Q`` nor a ``RawSQLTerm``.
    """

    name: str
    expressions: tuple[tuple[str | RawSQLTerm, str], ...]
    using: ExclusionConstraintUsing = ExclusionConstraintUsing.GIST
    condition: Q | RawSQLTerm | None = None
    include: tuple[str, ...] = ()
    deferrable: bool = False
    initially_deferred: bool = False

    def __post_init__(self) -> None:
        if self.initially_deferred and not self.deferrable:
            raise ConfigurationError("ExclusionConstraint.initially_deferred requires deferrable=True")
        if self.condition is not None:
            ConstraintCondition.raise_if_not_condition(self.condition, "ExclusionConstraint.condition")
        object.__setattr__(self, "include", tuple(self.include))

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        kwargs: dict[str, Any] = {"name": self.name, "expressions": self.expressions, "using": self.using}
        if self.condition is not None:
            kwargs["condition"] = self.condition
        if self.include:
            kwargs["include"] = list(self.include)
        if self.deferrable:
            kwargs["deferrable"] = self.deferrable
        if self.initially_deferred:
            kwargs["initially_deferred"] = self.initially_deferred
        return path, [], kwargs
