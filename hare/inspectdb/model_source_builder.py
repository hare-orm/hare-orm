from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.inspectdb.inspected_model_builder import InspectedModelBuilder
from hare.migrations.writer.import_manager import ImportManager
from hare.migrations.writer.migration_writer import MigrationWriter
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.inspectdb.inspected_model import InspectedModel
    from hare.inspectdb.types.table_info import TableInfo
    from hare.migrations.state.project.model_state import ModelState


class ModelSourceBuilder:
    """Renders one table's ``InspectedModel`` as Python model source - each field, index and
    constraint written as the call building it, the way a migration file writes it.

    Args:
        table: The table.
        dialect: The dialect it was read from.
        app_label: The app label relations are qualified with.
        fk_target_overrides: Per target table, the ``"<app>.<Class>"`` a relation points at.
        skip_m2m_through_tables: Whether a ManyToManyField through table gets a note instead of a
            class.
        class_name: The class name - ``ModelNaming.get_class_name()`` by default.
    """

    def __init__(
        self,
        table: TableInfo,
        dialect: str,
        app_label: str = "models",
        fk_target_overrides: dict[str, str] | None = None,
        skip_m2m_through_tables: bool = True,
        class_name: str | None = None,
    ) -> None:
        self.table = table
        self.dialect = dialect
        self.app_label = app_label
        self.fk_target_overrides = fk_target_overrides
        self.skip_m2m_through_tables = skip_m2m_through_tables
        self.class_name = class_name
        self.model_builder = self.get_model_builder(frozenset())
        self.imports = ImportManager()
        #: Whether the last build rendered a class, not a skipped table's note.
        self.has_class = False

    def get_model_builder(self, extra_reserved_attr_names: frozenset[str]) -> InspectedModelBuilder:
        """The builder of the table's inspected model, reserving ``extra_reserved_attr_names``."""
        return InspectedModelBuilder(
            self.table,
            self.dialect,
            self.app_label,
            self.fk_target_overrides,
            self.skip_m2m_through_tables,
            self.class_name,
            extra_reserved_attr_names,
        )

    @property
    def bound_names(self) -> set[str]:
        """Module-level names the rendered source binds through its imports."""
        return {"Model", *self.imports.bound_names()}

    def build(self) -> str:
        """The model's source with its imports.

        Returns:
            The source, or the note a skipped table gets instead.
        """
        body = self.build_body()
        if not self.has_class:
            return body
        return "\n".join([*self.get_import_lines(self.imports), "", "", body])

    def build_body(self) -> str:
        """The model's class source without its imports - which ``self.imports`` holds then.

        Returns:
            The class source, or the note a skipped table gets instead.
        """
        inspected = self.model_builder.build()
        self.has_class = inspected.state is not None
        if inspected.state is None:
            return inspected.skipped_note or ""
        source = self.render(inspected)
        # A class-body attribute named like an import (a column "fields", "OnDelete", ...) would
        # shadow that import for every later line of the class body - rebuilt with those names
        # reserved. Renaming attributes never changes which imports are needed.
        if not self.bound_names & set(inspected.state.fields):
            return source
        self.model_builder = self.get_model_builder(frozenset(self.bound_names))
        self.imports = ImportManager()
        return self.render(self.model_builder.build())

    @staticmethod
    def get_import_lines(imports: ImportManager) -> list[str]:
        """The import lines of a module of models.

        Args:
            imports: What the models import.

        Returns:
            The lines.
        """
        return ["from hare.models import Model", *imports.render()]

    def render_field(self, field: Field[Any]) -> str:
        """A field as the call building it. A relation indexed by default is written as a model
        declares it: without ``db_index=True`` (a migration file writes it, since a replayed
        relation isn't indexed by default), and with ``db_index=False`` when it has no index."""
        path, args, kwargs = field.deconstruct()
        if getattr(type(field), "indexed_by_default", False):
            if kwargs.get("db_index") is True:
                del kwargs["db_index"]
            elif not field.index and not field.pk:
                kwargs["db_index"] = False
        return MigrationWriter.render_call(path, args, kwargs, self.imports)

    def render(self, inspected: InspectedModel) -> str:
        """The class source of an inspected model.

        Args:
            inspected: The inspected model - with a state.

        Returns:
            The source.
        """
        # Only a model with a state reaches here - a skipped through table has none.
        state = cast("ModelState", inspected.state)
        lines = [f"class {inspected.class_name}(Model):"]
        for attr_name, field in state.fields.items():
            todo_reason = inspected.todo_reasons.get(attr_name)
            todo = f"  # TODO: {todo_reason}" if todo_reason else ""
            lines.append(f"    {attr_name} = {self.render_field(field)}{todo}")
        if isinstance(state.pk_field_name, tuple):
            composite_pk = MigrationWriter.render_call(
                "hare.fields.base.CompositePrimaryKey", list(state.pk_field_name), {}, self.imports
            )
            lines.append("")
            lines.append(f"    pk = {composite_pk}")
        if inspected.meta_layout:
            lines.append("")
            lines.append("    class Meta:")
            # A Meta body made only of comment lines would be a SyntaxError.
            if not any(isinstance(entry, ModelOption) for entry in inspected.meta_layout):
                lines.append("        pass")
            for entry in inspected.meta_layout:
                if isinstance(entry, ModelOption):
                    lines.append(
                        f"        {entry} = {MigrationWriter.render_value(state.options[entry], self.imports)}"
                    )
                else:
                    lines.append(f"        {entry}")
        return "\n".join(lines)
