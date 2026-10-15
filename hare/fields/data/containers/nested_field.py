from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.containers.tuple_field import TupleField
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.term import Term


class NestedField(ArrayField):
    """Rows of named columns held in one value - a list of dicts, each with every column::

        items = NestedField({"sku": fields.CharField(max_length=20), "quantity": fields.IntField()})
        # [{"sku": "a-1", "quantity": 2}, {"sku": "b-7", "quantity": 1}]

    An array of named tuples: ``items__0`` reads a row, ``items__sku`` the column of every row as an
    array, ``items__len`` the number of rows; the array lookups compare it.

    Args:
        element_fields: The columns' fields by name - any field, a container too.

    Raises:
        ConfigurationError: ``element_fields`` isn't a non-empty dict of fields by identifier.
    """

    def __init__(self, element_fields: Mapping[str, Field[Any]], **kwargs: Any) -> None:
        if not isinstance(element_fields, Mapping):
            raise ConfigurationError(f"NestedField takes a dict of fields by name, got {element_fields!r}")
        self.element_fields = element_fields
        super().__init__(TupleField(element_fields), **kwargs)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """A column of every row as an array (``items__sku``), else what an array reads."""
        # Local import: hare.sql's terms import the fields package.
        from hare.sql.terms.containers import TupleElementTerm

        row_field = self.base_field
        if isinstance(row_field, TupleField) and row_field.element_names and segment in row_field.element_names:
            position = row_field.element_names.index(segment)
            return partial(TupleElementTerm, index=position), ArrayField(row_field.fields_in_order[position])
        return super().get_path_transform(segment)
