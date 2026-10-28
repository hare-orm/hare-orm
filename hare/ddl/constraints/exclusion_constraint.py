from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.declared_names import DeclaredNames
from hare.ddl.enums import ExclusionConstraintUsing
from hare.ddl.expressions.declared_expressions import DeclaredExpressions
from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.query.expressions import Expression, Q


@dataclass(frozen=True)
class ExclusionConstraint:
    """An exclusion constraint - rejects a row whose values overlap an existing row's
    under the given operators: ``ExclusionConstraint(name=..., expressions=(("resource", "="),
    ("during", "&&")), using="gist")`` rejects two bookings of one resource with overlapping ranges.
    Where the dialect writes exclusion constraints - the others refuse it. Not tenant-aware: add the tenant column to
    ``expressions`` (with ``"="``) to check per tenant.

    Args:
        expressions: ``(expression, operator)`` pairs - a field name (a relation means its key
            column), an expression of hare over the model's fields (``Lower("code")``, ``F("start") + 1``)
            written in the database's own SQL and renamed with the fields it reads, or a ``RawSQLTerm``
            used as written.
        using: The index access method - see ExclusionConstraintUsing.
        condition: Makes it partial - a ``Q`` over the model's fields or a ``RawSQLTerm`` predicate.
        include: Fields stored in the index as non-key columns (``INCLUDE``).
        deferrable: Check at transaction end (or after ``SET CONSTRAINTS ... DEFERRED``) instead of
            after each statement.
        initially_deferred: Only with ``deferrable=True`` - every transaction starts deferred.

    Raises:
        ConfigurationError: ``initially_deferred=True`` without ``deferrable=True``, the
            condition is neither a ``Q``, a ``RawSQLTerm`` nor an ``ExclusiveArcCondition``, or a hash
            constraint has another than one ``=`` expression, or ``include``.
    """

    name: str
    expressions: tuple[tuple[str | Expression | RawSQLTerm, str], ...]
    using: ExclusionConstraintUsing = ExclusionConstraintUsing.GIST
    condition: Q | RawSQLTerm | None = None
    include: tuple[str, ...] = ()
    deferrable: bool = False
    initially_deferred: bool = False

    def __post_init__(self) -> None:
        DeclaredNames.raise_if_not_name(self.name, "ExclusionConstraint.name")
        if not isinstance(self.expressions, tuple | list) or not all(
            isinstance(element, tuple | list)
            and len(element) == 2
            and DeclaredExpressions.is_declared_expression(element[0])
            and isinstance(element[1], str)
            and element[1]
            for element in self.expressions
        ):
            raise ConfigurationError(
                f"ExclusionConstraint {self.name!r}.expressions takes (expression, operator) pairs - a field name, "
                "an expression of hare or a RawSQLTerm(...), and an operator"
            )
        object.__setattr__(self, "expressions", tuple(tuple(element) for element in self.expressions))
        object.__setattr__(
            self, "include", tuple(DeclaredNames.get_checked_names(self.include, "ExclusionConstraint.include") or ())
        )
        if self.initially_deferred and not self.deferrable:
            raise ConfigurationError("ExclusionConstraint.initially_deferred requires deferrable=True")
        if self.condition is not None:
            ConstraintCondition.raise_if_not_condition(self.condition, "ExclusionConstraint.condition")
        if self.using == ExclusionConstraintUsing.HASH and (
            len(self.expressions) != 1 or self.expressions[0][1] != "=" or self.include
        ):
            raise ConfigurationError(
                f"ExclusionConstraint {self.name!r} with using='hash' takes one expression with the '=' operator "
                "and no include - a hash index has a single key and no INCLUDE columns"
            )

    def get_read_field_names(self) -> set[str]:
        """The fields the constraint's expressions read - a raw SQL expression's aren't read out of it.

        Returns:
            The field names.
        """
        return {
            name
            for expression, _operator in self.expressions
            for name in (
                {expression}
                if isinstance(expression, str)
                else ExpressionFieldRenaming.get_all_read_field_names(expression)
            )
        }

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """The declaration as a migration writes it.

        Returns:
            The import path of its class, its positional arguments and the keyword arguments it was given
            other than by default.
        """
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
