from __future__ import annotations

from typing import Any

from hare.fields.field import Field
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField


class ColumnFieldNames:
    """The names of a model state's columns and fields mapped onto each other - what an index or a
    constraint the database shows by its columns names in the model."""

    @staticmethod
    def column_to_field_name(fields: dict[str, Field[Any]]) -> dict[str, str]:
        """Each column name to the field owning it (``event_id`` -> ``event``)."""
        mapping: dict[str, str] = {}
        for name, field in fields.items():
            if isinstance(field, ManyToManyFieldInstance):
                continue
            if isinstance(field, RelationalField):
                for db_column_name in field.db_column_names:
                    mapping[db_column_name] = name
                continue
            mapping[field.source_field or name] = name
        return mapping

    @staticmethod
    def field_to_column_names(fields: dict[str, Field[Any]]) -> dict[str, tuple[str, ...]]:
        """Maps each field name to the real database column(s) it owns - the inverse of
        column_to_field_name(), skipping a many-to-many (it owns no column on this table)."""
        column_names_by_field_name: dict[str, tuple[str, ...]] = {}
        for name, field in fields.items():
            if isinstance(field, ManyToManyFieldInstance):
                continue
            if isinstance(field, RelationalField):
                column_names_by_field_name[name] = tuple(field.db_column_names)
                continue
            column_names_by_field_name[name] = (field.source_field or name,)
        return column_names_by_field_name
