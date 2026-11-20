from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


class ColumnFieldNames:
    """The names of a model state's columns and fields mapped onto each other - what an index or a
    constraint the database shows by its columns names in the model."""

    @staticmethod
    def column_to_field_name(binding: ModelBinding, field_names: Iterable[str]) -> dict[str, str]:
        """Each column name to the field owning it (``event_id`` -> ``event``)."""
        fields = {name: binding.fields_map[name] for name in field_names}
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
