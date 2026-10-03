from __future__ import annotations

from typing import TYPE_CHECKING

from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations.base.hare_operation import HareOperation
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class TableRenamingOperation(HareOperation):
    """Base for an operation that renames a model's table and/or class - both change the
    auto-derived names of every automatic M2M through table touching that model."""

    async def _apply_table_rename(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None,
        name_in_old_state: str,
        name_in_new_state: str,
    ) -> None:
        """Renames the model's table together with the indexes/unique constraints named after it,
        then every automatic M2M through table/key column whose derived name changed with it.

        Args:
            app_label: The migration's app label.
            old_state: The state the database currently matches.
            new_state: The state the database is moved to.
            state_editor: The schema editor, or None for a state-only run.
            name_in_old_state: The model's name in ``old_state``.
            name_in_new_state: The model's name in ``new_state``.
        """
        if not state_editor:
            return
        old_model = old_state.apps.get_model(f"{app_label}.{name_in_old_state}")
        new_model = new_state.apps.get_model(f"{app_label}.{name_in_new_state}")
        old_table = old_model._meta.db_table
        new_table = new_model._meta.db_table
        if old_table != new_table:
            await state_editor.rename_table(new_model, old_table, new_table)
            await state_editor.rename_generated_index_names(old_model, new_model)
        await self._rename_auto_m2m_through_tables(
            app_label, old_state, new_state, state_editor, name_in_old_state, name_in_new_state
        )

    @staticmethod
    async def _rename_auto_m2m_through_tables(
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor,
        name_in_old_state: str,
        name_in_new_state: str,
    ) -> None:
        """Renames the automatic through table and key columns of every M2M field declared on,
        or pointing at, the renamed model.

        Args:
            app_label: The migration's app label.
            old_state: The state the database currently matches.
            new_state: The state the database is moved to.
            state_editor: The schema editor.
            name_in_old_state: The renamed model's name in ``old_state``.
            name_in_new_state: The renamed model's name in ``new_state``.
        """
        renamed_model = new_state.apps.get_model(f"{app_label}.{name_in_new_state}")
        for (model_app_label, model_name), model_state in new_state.models.items():
            is_renamed_model = model_app_label == app_label and model_name == name_in_new_state
            old_model_name = name_in_old_state if is_renamed_model else model_name
            if (model_app_label, old_model_name) not in old_state.models:
                continue
            old_owner_model = old_state.apps.get_model(f"{model_app_label}.{old_model_name}")
            new_owner_model = new_state.apps.get_model(f"{model_app_label}.{model_name}")
            for field_name, declared_field in model_state.fields.items():
                if not isinstance(declared_field, ManyToManyFieldInstance):
                    continue
                old_field = old_owner_model._meta.fields_map.get(field_name)
                new_field = new_owner_model._meta.fields_map.get(field_name)
                if not isinstance(old_field, ManyToManyFieldInstance) or not isinstance(
                    new_field, ManyToManyFieldInstance
                ):
                    continue
                if not is_renamed_model and new_field.related_model is not renamed_model:
                    continue
                if (
                    old_field.through == new_field.through
                    and old_field.forward_keys == new_field.forward_keys
                    and old_field.backward_keys == new_field.backward_keys
                ):
                    continue
                await state_editor._alter_m2m_field(new_owner_model, old_field, new_field)
