from __future__ import annotations

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class ConstraintNames(SchemaEditorPart):
    """The names of constraints - generated from the model and its columns, never read from the
    database."""

    __slots__ = ()

    def constraint_name_for_model(self, model: type[Model], constraint: UniqueConstraint) -> str:
        """The ``uid_<table>_<field>_<hash>`` name of a unique constraint added to an existing table -
        made from the table and field names alone, so it is the same wherever the constraint is
        added, removed or renamed.
        """
        if constraint.name:
            return constraint.name
        return self.editor.table_creation.get_unique_constraint_name(model, list(constraint.fields))

    def get_fields_to_columns(self, model: type[Model], field_names: tuple[str, ...] | list[str]) -> list[str]:
        """Returns the database column names of model field names - a relation's key column
        (``organization`` -> ``organization_id``). A name that isn't a field of the model is
        returned as is.
        """
        return model._meta.get_column_names(field_names)

    def get_partial_unique_index_name(self, model: type[Model], constraint: UniqueConstraint) -> str:
        """Returns the name of the unique index a unique constraint with a condition is created as.

        Args:
            model: The constrained model.
            constraint: The unique constraint.

        Returns:
            The index name.
        """
        column_constraint = UniqueConstraint(
            fields=tuple(self.get_fields_to_columns(model, constraint.fields)),
            name=constraint.name,
            condition=constraint.condition,
        )
        return self.constraint_name_for_model(model, column_constraint)
