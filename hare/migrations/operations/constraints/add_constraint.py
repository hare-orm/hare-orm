from __future__ import annotations

from typing import Any, ClassVar

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.constraint_object_type import ConstraintObjectType


class AddConstraint(AddSchemaObject):
    """Adds a constraint to a model.

    Args:
        model_name: The model.
        constraint: The constraint.
        not_valid: For a check constraint: the existing rows needn't pass it yet (Postgres ``NOT
            VALID``) - only new and updated rows are checked, so adding it doesn't scan and lock
            the table; a later ``ValidateConstraint`` checks the rest. SQLite adds it the plain way.

    Raises:
        ConfigurationError: ``not_valid`` for a constraint other than a check one.
    """

    object_type: ClassVar[type[ConstraintObjectType]] = ConstraintObjectType

    def __init__(
        self,
        model_name: str,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        *,
        not_valid: bool = False,
    ) -> None:
        if not_valid and not isinstance(constraint, CheckConstraint):
            raise ConfigurationError(f"not_valid=True takes a CheckConstraint, got {type(constraint).__name__}")
        super().__init__(model_name)
        self.constraint = constraint
        self.not_valid = not_valid

    @property
    def schema_object(self) -> Any:
        return self.constraint

    def get_type_options(self) -> dict[str, Any]:
        return {"not_valid": self.not_valid}

    def get_description_action(self) -> str:
        return "Add not-valid" if self.not_valid else "Add"
