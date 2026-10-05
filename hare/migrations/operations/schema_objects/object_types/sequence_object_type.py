from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.enums import GrantTarget
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class SequenceObjectType(SchemaObjectType):
    """The sequences of ``Meta.sequences`` - always named; one is changed in place, keeping its
    current number."""

    option: ClassVar[ModelOption] = ModelOption.SEQUENCES
    noun: ClassVar[str] = "sequence"
    plural_noun: ClassVar[str] = "sequences"
    feature: ClassVar[str | None] = "supports_sequences"

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Any:
        return dataclasses.replace(schema_object, name=new_name)

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.sequences.create_sequence(model, schema_object)
        await editor.grants.grant_again(model, GrantTarget.SEQUENCE, schema_object.name)

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.sequences.drop_sequence(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.sequences.rename_sequence(model, old_object, new_object)

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.sequences.alter_sequence(model, old_object, new_object)
