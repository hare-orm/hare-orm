from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.indexes.index import Index
from hare.migrations.constants import MODEL_OPTIONS_WITH_OWN_OPERATIONS
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.operations.models.delete_model import DeleteModel
from hare.migrations.operations.operation import Operation
from hare.migrations.state.state import State
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterModelOptions(HareOperation):
    """Sets the options of a model's Meta that have no operation of their own - the table's comment and
    storage follow them."""

    def __init__(self, name: str, options: dict[str, Any]):
        self.name = name
        self.options = options

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The model whose options change."""
        return frozenset({self.get_model_label(self.name, app_label)})

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """Later options of the same model - its whole set of them - or its deletion take this
        one's place."""
        if isinstance(other, (AlterModelOptions, DeleteModel)) and self.is_same_model_name(self.name, other.name):
            return [other]
        return super().reduce(other, app_label)

    def get_table_model_names(self) -> tuple[str, ...]:
        return (self.name,)

    def describe(self) -> str:
        return f"Alter options for {self.name}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        kwargs["options"] = AlterModelOptions.get_written_options(self.options)
        return path, args, kwargs

    @staticmethod
    def get_written_options(options: dict[str, Any]) -> dict[str, Any]:
        """Model options as a migration file writes them: plain string keys, and every
        ``Meta.indexes`` entry an ``Index`` - a tuple of field names stands for a plain one.

        Args:
            options: The options.

        Returns:
            The options to write.
        """
        written_options: dict[str, Any] = {}
        for key, value in options.items():
            if key == ModelOption.INDEXES:
                value = [entry if isinstance(entry, Index) else Index(fields=tuple(entry)) for entry in value]
            elif key == ModelOption.CONSTRAINTS:
                value = list(value)
            written_options[str(key)] = value
        return written_options

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.name)

        # `options` is the model's whole set of these options - one left out of it was removed.
        for key in list(model_state.options):
            if key not in MODEL_OPTIONS_WITH_OWN_OPERATIONS and key not in self.options:
                del model_state.options[key]
        model_state.options.update(self.options)
        model_state.description = model_state.options.get(ModelOption.TABLE_DESCRIPTION, "")
        state.reload_model(app_label, self.name)

    async def _apply_table_changes(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None,
    ) -> None:
        """Applies what an options change alters in the table of ``new_state``'s model: its comment
        when ``table_description`` changed, its storage when the ``Meta.table_options`` entry of the
        connection's dialect changed.

        Args:
            app_label: The migration's app label.
            old_state: The state the database currently matches.
            new_state: The state the database is moved to.
            state_editor: The schema editor, or None for a state-only run.
        """
        if not state_editor:
            return
        old_model = old_state.apps.get_model(f"{app_label}.{self.name}")
        new_model = new_state.apps.get_model(f"{app_label}.{self.name}")
        if old_model._meta.managed is False or new_model._meta.managed is False:
            return
        if (old_model._meta.table_description or "") != (new_model._meta.table_description or ""):
            await state_editor.table_comments.alter_table_comment(new_model)
        dialect = state_editor.client.dialect
        old_table_options = old_model._meta.get_table_options(dialect)
        new_table_options = new_model._meta.get_table_options(dialect)
        if old_table_options != new_table_options:
            await state_editor.table_partitions.alter_table_options(new_model, old_table_options, new_table_options)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_table_changes(app_label, old_state, new_state, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._apply_table_changes(app_label, old_state, new_state, state_editor)
