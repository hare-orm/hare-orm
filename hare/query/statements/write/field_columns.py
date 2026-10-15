from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import FieldError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model


class FieldColumns:
    """The columns a write names its fields by - a concrete field's own, a forward relation's key."""

    @staticmethod
    def get_column_field(model: type[Model], name: str, method_name: str) -> tuple[str, Field[object]]:
        """The column a written field is stored in, with the field the column holds - a forward
        relation's key field.

        Args:
            model: The written model.
            name: The field.
            method_name: The write, named in the error.

        Returns:
            The column and its field.

        Raises:
            FieldError: The field is unknown, not stored in the table, generated, or a relation to a
                composite key.
        """
        meta = model._meta
        field = meta.fields_map.get(name) if isinstance(name, str) else None
        if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
            if len(field.source_fields) > 1:
                raise FieldError(
                    f"{method_name} writes {name!r}, a relation to a composite key - name its key fields "
                    f"({', '.join(field.source_fields)}) instead"
                )
            name = field.source_fields[0]
            field = meta.fields_map[name]
        if field is None or name not in meta.fields_db_projection:
            raise FieldError(f"{method_name} writes the {model.__name__} fields stored in its table, got {name!r}")
        if field.generated and not field.pk:
            raise FieldError(f"{method_name} can't write {name!r} - it is generated")
        return meta.fields_db_projection[name], field
