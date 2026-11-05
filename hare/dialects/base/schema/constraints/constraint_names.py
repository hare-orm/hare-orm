from __future__ import annotations

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class ConstraintNames(SchemaEditorPart):
    """The names of constraints - generated from the model and its columns, never read from the
    database."""

    __slots__ = ()

    def constraint_name_for_model(self, model: type[Model], constraint: UniqueConstraint) -> str:
        """The declared name or a name generated from the table and constraint columns.

        The declaration contains model field names; metadata maps them to database columns
        once. The same name identifies a table constraint and a partial unique index.

        Args:
            model: The constrained model.
            constraint: The unique constraint.

        Returns:
            The constraint or backing index name.
        """
        if constraint.name:
            return constraint.name
        return GeneratedNames.get_index_name(
            GeneratedNamePrefix.UNIQUE_CONSTRAINT, model, model._meta.get_column_names(constraint.fields)
        )
