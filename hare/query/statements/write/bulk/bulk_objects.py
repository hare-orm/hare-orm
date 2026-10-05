from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import IncompleteInstanceError, QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class BulkObjects:
    """The objects ``bulk_create()`` and ``bulk_update()`` write - checked before any SQL."""

    @staticmethod
    def validate(
        model: type[Model], objects: list[Any], method_name: str, field_names: Iterable[str] | None = None
    ) -> None:
        """Checks every object is an instance of ``model`` itself and, when loaded with
        ``.only()``/``.defer()``, has every written field.

        Args:
            model: The model the call writes to.
            objects: The objects to write.
            method_name: Names the call in the raised message.
            field_names: The fields written from each object, or None for every column the model
                inserts.

        Raises:
            QueryError: An object is not an instance of ``model`` - a sibling model or a subclass
                with a table of its own included.
            IncompleteInstanceError: A partially loaded object lacks a written field.
        """
        meta = model._meta
        inserts_every_column = field_names is None
        written_field_names = (
            [field_name for field_name in meta.fields_db_projection if not meta.fields_map[field_name].generated]
            if field_names is None
            else list(field_names)
        )
        for obj in objects:
            if type(obj) is not model:
                raise QueryError(
                    f"{method_name}() on {model.__name__} got a {type(obj).__name__} object - every object "
                    f"must be a {model.__name__} instance"
                )
            if not obj._partial:
                continue
            object_field_names = written_field_names
            if inserts_every_column and obj._custom_generated_pk:
                object_field_names = [*written_field_names, *meta.primary_key_attribute_names]
            missing_field_names = [
                field_name
                for field_name in dict.fromkeys(object_field_names)
                if field_name not in obj._await_when_save and not hasattr(obj, field_name)
            ]
            if missing_field_names:
                raise IncompleteInstanceError(
                    f"{model.__name__} is a partial model, field(s) {missing_field_names} are not loaded - "
                    f"{method_name}() writes them (pk={obj.pk!r})"
                )
