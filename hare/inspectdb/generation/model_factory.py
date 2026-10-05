from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.inspectdb.constants import FACTORY_MODEL_MODULE_NAME
from hare.inspectdb.generation.declarations import ModelGenerationOptions
from hare.inspectdb.generation.inspected_model_builder import InspectedModelBuilder
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.inspectdb.generation.inspected_model import InspectedModel
    from hare.inspectdb.introspection.table_info import TableInfo
    from hare.migrations.state.model_state import ModelState
from hare.inspectdb.exceptions import ManyToManyThroughTableSkippedError


class ModelFactory:
    """Builds Model classes from introspected tables."""

    @staticmethod
    def from_table_info(
        table_info: TableInfo,
        dialect: str,
        *,
        app_label: str,
        foreign_key_target_overrides: dict[str, str] | None = None,
        skip_many_to_many_through_tables: bool = True,
    ) -> type[Model]:
        """Builds an unregistered Model class for one table.

        Args:
            table_info: The introspected table.
            dialect: The backend dialect, for type mapping.
            app_label: App label FK/composite-FK target references are qualified with
                (``"<app_label>.<TargetClass>"``) - normally the label the model is then
                registered under.
            foreign_key_target_overrides: Per-target-table override of the whole ``"<app_label>.
                <TargetClass>"`` reference, keyed by the target's raw table name - for pointing an
                FK at an already-registered model.
            skip_many_to_many_through_tables: Whether a table shaped exactly like a ManyToManyField through
                table is refused (the default) instead of built.

        Returns:
            The new model class, not yet registered anywhere.

        Raises:
            ManyToManyThroughTableSkippedError: The table is a ManyToManyField through table and
                ``skip_many_to_many_through_tables`` is True.
        """
        inspected = InspectedModelBuilder(
            table_info,
            ModelGenerationOptions(dialect, app_label, foreign_key_target_overrides, skip_many_to_many_through_tables),
        ).build()
        if inspected.state is None:
            raise ManyToManyThroughTableSkippedError(table_info.name)
        return ModelFactory.build_model_class(inspected)

    @staticmethod
    def build_model_class(inspected: InspectedModel) -> type[Model]:
        """Builds the class of an inspected model.

        Args:
            inspected: The inspected model - with a state.

        Returns:
            The new model class.
        """
        # Only a model with a state reaches here - a skipped through table has none.
        state = cast("ModelState", inspected.state)
        attributes: dict[str, Any] = {"__module__": FACTORY_MODEL_MODULE_NAME, "__qualname__": inspected.class_name}
        attributes.update({name: copy(field) for name, field in state.fields.items()})
        if isinstance(state.pk_field_name, tuple):
            attributes["pk"] = CompositePrimaryKey(*state.pk_field_name)
        meta_options = {
            str(entry): state.options[entry] for entry in inspected.meta_layout if isinstance(entry, ModelOption)
        }
        if meta_options:
            attributes["Meta"] = type("Meta", (), meta_options)
        return cast("type[Model]", type(inspected.class_name, (Model,), attributes))
