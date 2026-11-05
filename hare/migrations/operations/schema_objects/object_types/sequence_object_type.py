from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.enums import GrantTarget
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:
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
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        grants = editor.grants.get_grant_again_sqls(model, GrantTarget.SEQUENCE, schema_object.name)
        await editor.sequences.create_sequence(model, schema_object)
        await editor.run_sqls(grants)

    @classmethod
    async def remove(
        cls,
        editor: BaseSchemaEditor,
        model: type[Model],
        remaining_model: type[Model],
        schema_object: Any,
        **options: Any,
    ) -> None:
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
