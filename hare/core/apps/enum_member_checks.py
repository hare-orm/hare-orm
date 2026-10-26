from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.enum_members_condition import EnumMembersCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
from hare.fields.data.choices.constants import ENUM_MEMBERS_CHECK_SUFFIX
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance

if TYPE_CHECKING:
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.models import Model


class EnumMemberChecks:
    """The ``CHECK`` of each ``IntEnumField`` and ``CharEnumField`` of a model bound to a database whose
    column holds any integer or text - it keeps the column to the values the members are stored as, as a
    native enum column does on its own. A model of a migration state takes its checks from the
    migrations."""

    @staticmethod
    def add_to_models(models: Iterable[type[Model]], connections: ConnectionHandler) -> None:
        """Sets the enum checks of each model, bound to its connection - the ones set by an earlier
        binding are replaced.

        Args:
            models: The models.
            connections: The connections they are bound to - their settings, none opened.
        """
        for model in models:
            meta = model._meta
            if meta.abstract or meta.swapped is not None:
                continue
            declared_constraints = [
                constraint for constraint in meta.constraints if not EnumMemberChecks.is_enum_check(constraint)
            ]
            enum_checks: list[CheckConstraint] = []
            if (
                meta.default_connection is not None
                and not connections.get_dialect(meta.default_connection).features.keeps_enum_columns
            ):
                declared_names = {getattr(constraint, "name", None) for constraint in declared_constraints}
                for field_name, field in meta.fields_map.items():
                    check = EnumMemberChecks.get_enum_check(field_name, field)
                    if check is not None and check.name not in declared_names:
                        enum_checks.append(check)
            meta.constraints = (*declared_constraints, *enum_checks)

    @staticmethod
    def get_enum_check(field_name: str, field: Any) -> CheckConstraint | None:
        """The check of an enum field's column.

        Args:
            field_name: The field's name.
            field: The field.

        Returns:
            The check, None for a field of no enum column of its own - a native enum type's holds the
            members itself.
        """
        if not isinstance(field, (IntEnumFieldInstance, CharEnumFieldInstance)) or not field.has_db_field:
            return None
        if isinstance(field, CharEnumFieldInstance) and field.has_native_enum_type:
            return None
        return CheckConstraint(
            check=EnumMembersCondition(field_name, field.get_stored_member_values()),
            name=f"{field_name}{ENUM_MEMBERS_CHECK_SUFFIX}",
        )

    @staticmethod
    def is_enum_check(constraint: Any) -> bool:
        """Whether a constraint is the check of an enum field.

        Args:
            constraint: One of the model's constraints.

        Returns:
            Whether it is.
        """
        return (
            isinstance(constraint, CheckConstraint)
            and isinstance(constraint.check, EnumMembersCondition)
            and constraint.name == f"{constraint.check.field}{ENUM_MEMBERS_CHECK_SUFFIX}"
        )
