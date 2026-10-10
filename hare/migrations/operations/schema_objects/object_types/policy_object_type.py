from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class PolicyObjectType(SchemaObjectType):
    """The row level security policies of ``Meta.policies`` - always named; one is changed in
    place. A policy's raw SQL condition may read any table."""

    option: ClassVar[ModelOption] = ModelOption.POLICIES
    noun: ClassVar[str] = "policy"
    plural_noun: ClassVar[str] = "row level security policies"
    feature: ClassVar[str | None] = "supports_row_level_security"
    reaches_other_tables: ClassVar[bool] = True

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Any:
        return dataclasses.replace(schema_object, name=new_name)

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.row_level_security_policies.create_policy(model, schema_object)

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.row_level_security_policies.drop_policy(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.row_level_security_policies.rename_policy(model, old_object, new_object)

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.row_level_security_policies.alter_policy(model, old_object, new_object)
