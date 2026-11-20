from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.enums import GrantTarget
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State
    from hare.models.model_binding import ModelBinding


class ViewObjectType(SchemaObjectType):
    """The views of ``Meta.views`` - always named; one is replaced in place. A view's query may read
    any table."""

    option: ClassVar[ModelOption] = ModelOption.VIEWS
    noun: ClassVar[str] = "view"
    plural_noun: ClassVar[str] = "views"
    feature: ClassVar[str | None] = "supports_views"
    reaches_other_tables: ClassVar[bool] = True

    @classmethod
    def get_state_entry(cls, schema_object: Any, *, state: State, app_label: str, model_name: str) -> Any:
        """The view with its query as SQL text."""
        client = state.environment.get_schema_client(app_label, state.default_connections)
        binding = state.apps.get_binding(f"{app_label}.{model_name}")
        return schema_object.for_binding(binding).with_sql_query(client)

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: ModelBinding, schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        grants = editor.grants.get_grant_again_sqls(model, GrantTarget.VIEW, schema_object.name)
        await editor.views.create_view(model, schema_object)
        await editor.run_sqls(grants)

    @classmethod
    async def remove(
        cls,
        editor: BaseSchemaEditor,
        model: ModelBinding,
        remaining_model: ModelBinding,
        schema_object: Any,
        **options: Any,
    ) -> None:
        cls.raise_if_unsupported(editor)
        await editor.views.drop_view(model, schema_object)

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: ModelBinding, old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.views.rename_view(model, old_object, new_object)

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: ModelBinding, old_object: Any, new_object: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.views.alter_view(model, old_object, new_object)
