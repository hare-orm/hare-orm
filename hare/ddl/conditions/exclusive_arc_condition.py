from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


@dataclass(frozen=True)
class ExclusiveArcCondition:
    """The condition of a ``CheckConstraint`` keeping an exclusive arc of relations - the branches of
    a ``GenericForeignKeyField``: exactly one of them is set, at most one with ``allow_none``. A
    branch to a composite key is set when all its columns are and unset when none is; any mix of
    the two fails.

    Args:
        fields: The relations of the arc.
        allow_none: Whether a row may have none of them set.

    Raises:
        ConfigurationError: ``fields`` holds anything but relation names, or fewer than one.
    """

    fields: tuple[str, ...]
    allow_none: bool = False

    def __post_init__(self) -> None:
        # Any: a declaration may pass anything.
        fields: Any = self.fields
        if isinstance(fields, str) or not all(isinstance(name, str) and name for name in fields):
            raise ConfigurationError(f"ExclusiveArcCondition.fields takes the names of relations, got {self.fields!r}")
        if not self.fields:
            raise ConfigurationError("ExclusiveArcCondition.fields takes at least one relation")
        object.__setattr__(self, "fields", tuple(self.fields))

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": self.fields}
        if self.allow_none:
            kwargs["allow_none"] = True
        return ClassPath.get(self.__class__), [], kwargs

    def get_referenced_field_names(self) -> set[str]:
        """The relations the condition reads."""
        return set(self.fields)

    def with_renamed_field(self, old_name: str, new_name: str) -> ExclusiveArcCondition:
        """The condition with a relation renamed.

        Args:
            old_name: The relation's name.
            new_name: Its new name.

        Returns:
            The condition.
        """
        return replace(self, fields=tuple(new_name if name == old_name else name for name in self.fields))

    def get_condition_sql(self, model: type[Model], client: DatabaseClient) -> str:
        """The predicate written into a table of ``model``.

        Args:
            model: The model the constraint belongs to.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate, made by the dialect's schema editor.
        """
        quote_identifier = client.dialect.literals.quote_identifier
        quoted_column_groups = [
            [quote_identifier(column_name) for column_name in model._meta.get_column_names((name,))]
            for name in self.fields
        ]
        return client.dialect.schema_editor_class.tenant_conditions_class.get_exclusive_arc_check_sql(
            quoted_column_groups, self.allow_none
        )
