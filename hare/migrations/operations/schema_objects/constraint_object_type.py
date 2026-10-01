from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.schema_objects.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor
    from hare.migrations.state.project.model_state import ModelState
    from hare.models import Model


class ConstraintObjectType(SchemaObjectType):
    """The constraints of ``Meta.constraints`` - unique, check and exclusion ones. An unnamed
    unique constraint is found by its fields."""

    option: ClassVar[ModelOption] = ModelOption.CONSTRAINTS
    noun: ClassVar[str] = "constraint"

    @classmethod
    def get_objects(cls, model_state: ModelState) -> list[Any]:
        return [
            constraint
            for constraint in super().get_objects(model_state)
            if isinstance(constraint, (UniqueConstraint, CheckConstraint, ExclusionConstraint))
        ]

    @classmethod
    def find(
        cls,
        model_state: ModelState,
        get_model: Callable[[], type[Model]],
        *,
        name: str | None = None,
        fields: list[str] | None = None,
    ) -> Any:
        constraints = cls.get_objects(model_state)
        if name:
            for constraint in constraints:
                if constraint.name == name:
                    return constraint
        if fields:
            for constraint in constraints:
                if (
                    isinstance(constraint, UniqueConstraint)
                    and constraint.name is None
                    and tuple(constraint.fields) == tuple(fields)
                ):
                    return constraint
        raise IncompatibleStateError(f"Constraint {name or fields} is not present on {model_state.name}")

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Any:
        return replace(schema_object, name=new_name)

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        if options.get("not_valid"):
            await editor.add_check_constraint_not_valid(model, cast("CheckConstraint", schema_object))
        else:
            await editor.add_constraint(model, schema_object)

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        await editor.remove_constraint(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        await editor.rename_constraint(model, old_object, new_object)
