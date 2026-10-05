from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.models import Model
    from hare.query.expressions import Q


@dataclass(frozen=True)
class UniqueConstraint:
    """A named uniqueness constraint on a set of fields. Not tenant-aware: add the tenant column to
    ``fields`` to check per tenant.

    Args:
        deferrable: Check at transaction end (or after ``SET CONSTRAINTS ... DEFERRED``) - needs
            ``Features.supports_deferrable_constraints``.
        initially_deferred: Only with ``deferrable=True`` - every transaction starts deferred.
        include: Fields stored in the index as non-key columns - left out where the dialect has
            none.
        condition: Makes it a partial unique index - a ``Q`` over the model's fields or a
            ``RawSQLTerm`` predicate.
        nulls_distinct: Needs ``Features.supports_nulls_distinct`` - False makes rows with NULLs
            collide (``NULLS NOT DISTINCT``), True states the default, None leaves it to the
            database.
        without_overlaps: The last field is a range two rows may not overlap in while the other
            fields are equal (``WITHOUT OVERLAPS``) - needs ``Features.supports_without_overlaps``.

    Raises:
        ConfigurationError: ``initially_deferred=True`` without ``deferrable=True``, the condition
            is neither a ``Q`` nor a ``RawSQLTerm``, or ``without_overlaps=True`` with fewer than two
            fields or with a condition.
    """

    fields: tuple[str, ...]
    name: str | None = None
    condition: Q | RawSQLTerm | None = None
    deferrable: bool = False
    initially_deferred: bool = False
    include: tuple[str, ...] = ()
    nulls_distinct: bool | None = None
    without_overlaps: bool = False

    def __post_init__(self) -> None:
        if self.initially_deferred and not self.deferrable:
            raise ConfigurationError("UniqueConstraint.initially_deferred requires deferrable=True")
        if type(self.without_overlaps) is not bool:
            raise ConfigurationError(
                f"UniqueConstraint.without_overlaps must be a bool, got {self.without_overlaps!r}"
            )
        if self.without_overlaps and len(self.fields) < 2:
            raise ConfigurationError(
                "UniqueConstraint(without_overlaps=True) needs two fields or more - the range last, the fields "
                "whose equal values may not have overlapping ranges first"
            )
        if self.without_overlaps and self.condition is not None:
            raise ConfigurationError("UniqueConstraint.without_overlaps is not supported together with condition")
        if self.condition is not None:
            ConstraintCondition.raise_if_not_condition(self.condition, "UniqueConstraint.condition")
        # A constraint replayed from a migration file is declared with lists - it equals the one
        # the model declares with tuples.
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "include", tuple(self.include))

    def raise_if_unsupported(self, features: Features, dialect: Dialect) -> None:
        """Rejects what the database can't enforce.

        Args:
            features: The features of the connection the DDL runs on.
            dialect: The dialect of the database the DDL runs on.

        Raises:
            UnSupportedError: ``nulls_distinct`` is set on a database without
                ``NULLS [NOT] DISTINCT`` (``Features.supports_nulls_distinct``).
        """
        if self.nulls_distinct is not None and not features.supports_nulls_distinct:
            raise UnSupportedError(
                f"UniqueConstraint.nulls_distinct is not supported by the {dialect} server of this connection"
            )
        if self.without_overlaps and not features.supports_without_overlaps:
            raise UnSupportedError(
                f"UniqueConstraint.without_overlaps is not supported by the {dialect} server of this connection"
            )

    def get_nulls_sql(self, dialect: Dialect) -> str:
        """The clause stating whether NULLs collide, or ``""``.

        Args:
            dialect: The dialect of the database the DDL runs on.

        Returns:
            The clause.
        """
        if self.nulls_distinct is None:
            return ""
        return dialect.schema_editor_class.constraint_statements_class.get_nulls_distinct_sql(self.nulls_distinct)

    def get_include_sql(self, model: type[Model], dialect: Dialect, quote: Callable[[str], str]) -> str:
        """The clause of the non-key columns - none where the dialect has no non-key index
        columns, as they only make the index cover more queries.

        Args:
            model: The constrained model.
            dialect: The dialect of the database the DDL runs on.
            quote: Quotes an identifier.

        Returns:
            The clause, or ``""``.
        """
        if not self.include:
            return ""
        return dialect.schema_editor_class.index_statements_class.get_index_include_sql(
            [quote(column) for column in model._meta.get_column_names(self.include)]
        )

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        kwargs: dict[str, Any] = {"fields": list(self.fields)}
        if self.name:
            kwargs["name"] = self.name
        if self.condition is not None:
            kwargs["condition"] = self.condition
        if self.deferrable:
            kwargs["deferrable"] = self.deferrable
        if self.initially_deferred:
            kwargs["initially_deferred"] = self.initially_deferred
        if self.include:
            kwargs["include"] = list(self.include)
        if self.nulls_distinct is not None:
            kwargs["nulls_distinct"] = self.nulls_distinct
        if self.without_overlaps:
            kwargs["without_overlaps"] = True
        return path, [], kwargs
