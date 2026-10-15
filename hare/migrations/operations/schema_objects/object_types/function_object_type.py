from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.enums import GrantTarget
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class FunctionObjectType(SchemaObjectType):
    """The functions of ``Meta.functions`` - always named; one is replaced in place. A function's
    body may read and change any table."""

    option: ClassVar[ModelOption] = ModelOption.FUNCTIONS
    noun: ClassVar[str] = "function"
    plural_noun: ClassVar[str] = "database functions"
    feature: ClassVar[str | None] = "supports_database_functions"
    reaches_other_tables: ClassVar[bool] = True

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Any:
        return dataclasses.replace(schema_object, name=new_name)

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.database_functions.create_database_function(model, schema_object)
        await editor.grants.grant_again(model, GrantTarget.FUNCTION, schema_object.name)

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.database_functions.drop_database_function(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.database_functions.rename_database_function(model, old_object, new_object)

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.database_functions.alter_database_function(model, old_object, new_object)
