from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.dialect_condition import DialectCondition
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


@dataclass(frozen=True)
class EnumMembersCondition(DialectCondition):
    """The condition of a ``CheckConstraint`` keeping an enum field's column to the values its members
    are stored as - ``IntEnumField`` and ``CharEnumField`` add it where the column holds any integer or
    text. A NULL passes.

    Args:
        field: The enum field.
        values: The values its members are stored as.

    Raises:
        ConfigurationError: ``field`` isn't a field name, or ``values`` is empty.
    """

    field: str
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field:
            raise ConfigurationError(f"EnumMembersCondition.field takes the name of a field, got {self.field!r}")
        # Any: a declaration may pass anything.
        values: Any = self.values
        if not isinstance(values, (list, tuple)) or not values:
            raise ConfigurationError(f"EnumMembersCondition.values takes the stored values, got {self.values!r}")
        object.__setattr__(self, "values", tuple(values))

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(self.__class__), [], {"field": self.field, "values": self.values}

    def get_referenced_field_names(self) -> set[str]:
        """The field the condition reads."""
        return {self.field}

    def with_renamed_field(self, old_name: str, new_name: str) -> EnumMembersCondition:
        """The condition with its field renamed.

        Args:
            old_name: The field's name.
            new_name: Its new name.

        Returns:
            The condition.
        """
        return replace(self, field=new_name) if self.field == old_name else self

    def get_condition_sql(self, model: type[Model], client: DatabaseClient) -> str:
        """The predicate written into a table of ``model``.

        Args:
            model: The model the constraint belongs to.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate, made by the dialect's schema editor.
        """
        literals = client.dialect.literals
        [column_name] = model._meta.get_column_names((self.field,))
        return client.dialect.schema_editor_class.constraint_statements_class.get_enum_members_check_sql(
            literals.quote_identifier(column_name), [literals.get_literal_sql(value) for value in self.values]
        )
