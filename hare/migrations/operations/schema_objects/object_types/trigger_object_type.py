from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class TriggerObjectType(SchemaObjectType):
    """The triggers of ``Meta.triggers`` - always named; one can be changed in place."""

    option: ClassVar[ModelOption] = ModelOption.TRIGGERS
    noun: ClassVar[str] = "trigger"
    reaches_other_tables: ClassVar[bool] = True

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Any:
        return dataclasses.replace(schema_object, name=new_name)

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        await editor.trigger_statements.add_trigger(model, schema_object)

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        await editor.trigger_statements.remove_trigger(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        await editor.trigger_statements.rename_trigger(model, old_object, new_object)

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        await editor.trigger_statements.alter_trigger(model, old_object, new_object)
