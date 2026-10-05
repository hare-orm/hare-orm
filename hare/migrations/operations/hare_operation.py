from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.log import logger
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.operation import Operation
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class HareOperation(Operation):
    """The base of hare's own operations - each changes the state, and the database on its own."""

    def state_forward(self, app_label: str, state: State) -> None:
        return None

    @staticmethod
    def get_model_state(state: State, app_label: str, model_name: str) -> ModelState:
        model = state.models.get((app_label, model_name))
        if not model:
            raise IncompatibleStateError()

        return model

    @staticmethod
    def is_same_model_name(model_name: str, other_model_name: str) -> bool:
        """Whether two class names name one model of an app - compared case-insensitively, as a
        table name derived from a class name is.

        Args:
            model_name: One name.
            other_model_name: The other.

        Returns:
            Whether they are the same.
        """
        return model_name.lower() == other_model_name.lower()

    @staticmethod
    def get_model_label(model_reference: Any, app_label: str) -> str:
        """A model's lowercase ``app.model`` label - compared case-insensitively, as a table name
        derived from a class name is.

        Args:
            model_reference: A class name, an ``"app.Model"`` string or a model class.
            app_label: The app of a name given without one.

        Returns:
            The label.
        """
        if isinstance(model_reference, type) and issubclass(model_reference, Model):
            return f"{model_reference._meta.app or app_label}.{model_reference.__name__}".lower()
        reference_app_label, _, model_name = str(model_reference).rpartition(".")
        return f"{reference_app_label or app_label}.{model_name}".lower()

    @staticmethod
    def get_models_labels(app_label: str, model_names: list[str], fields: list[Any]) -> frozenset[str] | None:
        """The labels of models of an app and of the models fields relate to - through a target
        or a through model.

        Args:
            app_label: The operation's app.
            model_names: The class names of models of the app.
            fields: The fields.

        Returns:
            The labels; None when a field's target is named by a swappable setting - it can be
            any model.
        """
        labels = {HareOperation.get_model_label(model_name, app_label) for model_name in model_names}
        for field in fields:
            if not isinstance(field, DIRECT_RELATION_FIELDS):
                continue
            targets: list[Any] = [field.model_name]
            if isinstance(field, ManyToManyFieldInstance) and field.through_model is not None:
                targets.append(field.through_model)
            for target in targets:
                if isinstance(target, SwappableModelReference):
                    return None
                labels.add(HareOperation.get_model_label(target, app_label))
        return frozenset(labels)

    @staticmethod
    async def _toggle_schema(schema_name: str, create: bool, state_editor: BaseSchemaEditor | None) -> None:
        if not state_editor:
            return
        if not state_editor.client.features.supports_schemas:
            # No schemas on this dialect - nothing to create or drop.
            return
        if create:
            await state_editor.schemas.create_schema(schema_name)
        else:
            await state_editor.schemas.drop_schema(schema_name)

    @staticmethod
    def _runs_enum_types(state_editor: BaseSchemaEditor | None, statement: str) -> bool:
        """Whether an ``ENUM`` type operation runs on the migration's database - not without an editor,
        nor on a database without ``ENUM`` types: a migration file runs on any database, and such a
        database has no field of one.

        Args:
            state_editor: The migration's schema editor.
            statement: The skipped statement, for the warning.
        """
        if not state_editor:
            return False
        if not state_editor.client.features.supports_enum_types:
            logger.warning("Skipping %s on %s - it has no ENUM types.", statement, state_editor.client.dialect)
            return False
        return True

    @staticmethod
    async def _toggle_extension(extension_name: str, create: bool, state_editor: BaseSchemaEditor | None) -> None:
        if not state_editor:
            return
        if not state_editor.client.features.supports_extensions:
            logger.warning(
                "Skipping %s EXTENSION %r on %s - it has no extensions.",
                "CREATE" if create else "DROP",
                extension_name,
                state_editor.client.dialect,
            )
            return
        if create:
            await state_editor.extensions.create_extension(extension_name)
        else:
            await state_editor.extensions.drop_extension(extension_name)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        return None

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        return None

    async def run(
        self,
        app_label: str,
        state: State,
        dry_run: bool,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        old_state = state.clone() if (not dry_run and state_editor) else None
        self.state_forward(app_label, state)
        if dry_run or not state_editor or self.touches_swapped_model(app_label, state):
            return
        await self.database_forward(app_label, old_state, state, state_editor)  # type: ignore[arg-type]
