from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.enums import GrantTarget
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class MaterializedViewObjectType(SchemaObjectType):
    """The materialized views of ``Meta.materialized_views`` - always named; one is replaced in
    place. A view's query may read any table."""

    option: ClassVar[ModelOption] = ModelOption.MATERIALIZED_VIEWS
    noun: ClassVar[str] = "materialized view"
    plural_noun: ClassVar[str] = "materialized views"
    feature: ClassVar[str | None] = "supports_materialized_views"
    reaches_other_tables: ClassVar[bool] = True

    @classmethod
    def get_state_entry(cls, schema_object: Any) -> Any:
        """The view with its query as SQL text."""
        return schema_object.with_sql_query()

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        grants = editor.grants.get_grant_again_sqls(model, GrantTarget.MATERIALIZED_VIEW, schema_object.name)
        if options.get("replacing"):
            await editor.materialized_views.create_materialized_view_again(model, schema_object)
        else:
            await editor.materialized_views.create_materialized_view(model, schema_object)
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
        await editor.materialized_views.drop_materialized_view(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.materialized_views.rename_materialized_view(model, old_object, new_object)

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.materialized_views.alter_materialized_view(model, old_object, new_object)
