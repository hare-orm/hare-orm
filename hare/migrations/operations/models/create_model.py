from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.ddl.indexes.index import Index
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.swappable import SwappableModelReference
from hare.migrations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.base.hare_operation import HareOperation
from hare.migrations.operations.fields.field_like import FieldLike
from hare.migrations.operations.models.alter_model_options import AlterModelOptions
from hare.migrations.operations.models.model_table_operation import ModelTableOperation
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.state.project.state import State
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class CreateModel(ModelTableOperation, HareOperation):
    reads_old_state = False

    def __init__(
        self,
        name: str,
        fields: list[tuple[str, FieldLike]],
        options: dict[str, Any] | None = None,
        bases: list[str] | None = None,
        state_only: bool = False,
    ) -> None:
        """
        Args:
            name: The model's class name.
            fields: (name, field) pairs of the model's fields.
            options: The model's Meta options.
            bases: The model's base class names.
            state_only: Only records the model in the migration state - its table already exists,
                e.g. for a model moved here from another app.
        """
        self.options = options
        self.fields = fields
        self.name = name
        self.bases = bases
        self.state_only = state_only
        self._model: type[Model] | None = None

    def pop_field(self, field_name: str) -> tuple[FieldLike, list[Index]]:
        """Removes a field and the Meta.indexes entries over it, to be added back by a later
        AddField and AddIndex operations.

        Args:
            field_name: The field's name.

        Returns:
            The removed field and the removed indexes.
        """
        field = next(field for name, field in self.fields if name == field_name)
        self.fields = [pair for pair in self.fields if pair[0] != field_name]
        options = dict(self.options or {})
        kept_indexes: list[Any] = []
        removed_indexes: list[Index] = []
        for entry in options.get(ModelOption.INDEXES, ()):
            index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if field_name in (index.fields or ()) or field_name in index.include:
                removed_indexes.append(index)
            else:
                kept_indexes.append(entry)
        if removed_indexes:
            if kept_indexes:
                options[ModelOption.INDEXES] = tuple(kept_indexes)
            else:
                options.pop(ModelOption.INDEXES, None)
            self.options = options
        return field, removed_indexes

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def describe(self) -> str:
        if self.state_only:
            return f"Create model {self.name} (state only, its table already exists)"
        return f"Create model {self.name}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        # A relation's shadow key column belongs to the relation - written as the relation alone.
        source_fields = {
            field.source_field or f"{name}_id"
            for name, field in self.fields
            if field is not None and isinstance(field, ForeignKeyFieldInstance)
        }
        path, args, kwargs = super().deconstruct()
        kwargs["fields"] = [
            (name, field) for name, field in self.fields if field is not None and name not in source_fields
        ]
        if self.options:
            kwargs["options"] = AlterModelOptions.get_written_options(self.options)
        else:
            kwargs.pop("options", None)
        if not self.bases:
            kwargs.pop("bases", None)
        return path, args, kwargs

    @property
    def model(self) -> type[Model]:
        if not self._model:
            meta_class = type("Meta", (), self.options or {})

            attributes: dict[str, Any] = dict(self.fields)
            attributes["Meta"] = meta_class
            attributes["_no_comments"] = True

            # A composite primary key's fields don't carry primary_key=True - its marker is put
            # back, or the metaclass would add an "id".
            pk_attr = (self.options or {}).get("pk_attr")
            if isinstance(pk_attr, tuple):
                attributes["__migration_composite_pk__"] = CompositePrimaryKey(*pk_attr)

            self._model = cast("type[Model]", type(self.name, (Model,), attributes))

        return self._model

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = ModelState.make_from_model(app_label, self.model)
        replaces_a_model = (app_label, self.name) in state.models
        state.models[(app_label, self.name)] = model_state
        if not replaces_a_model:
            # Only the new model is rendered, linked to the rendered ones in place.
            state.add_rendered_model(app_label, self.name)
            return

        models_to_reload = {(app_label, self.name)}

        for field in model_state.fields.values():
            if not isinstance(field, DIRECT_RELATION_FIELDS):
                continue

            related_key = state.apps.split_reference(field.model_name)
            if related_key in state.models:
                models_to_reload.add(related_key)

        # Also find existing models that reference the newly created model.
        # This handles the case where a model with a FK was created before its
        # target (e.g. alphabetical ordering: Alert before Warehouse).
        new_model_ref = f"{app_label}.{self.name}"
        for key, existing_state in state.models.items():
            if key == (app_label, self.name):
                continue
            for field in existing_state.fields.values():
                if (
                    isinstance(field, DIRECT_RELATION_FIELDS)
                    and SwappableModelReference.get_model_reference(field.model_name) == new_model_ref
                ):
                    models_to_reload.add(key)
                    break

        state.reload_models(models_to_reload)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._create_model_table(app_label, new_state, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._delete_model_table(app_label, old_state, state_editor)
