from __future__ import annotations

import operator
from functools import partial
from typing import TYPE_CHECKING

from hare.query.filters.lookups.containers.container_lookups import ContainerLookups
from hare.query.filters.lookups.containers.container_value_encoders import ContainerValueEncoders
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from hare.query.filters.lookups.value_encoders import ValueEncoders

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.containers.array_field import ArrayField


class ContainerFieldLookups:
    """The lookups of container fields: equality of the whole value and ``isnull`` for every
    container, containment, length and ``item`` of an array, key checks of a map."""

    @staticmethod
    def get_whole_value_lookups() -> dict[str, FieldLookup]:
        """The lookups of any container - equality of the whole value, and ``isnull``.

        Returns:
            The lookups by suffix.
        """
        return {
            "": FieldLookup(operator.eq, ContainerValueEncoders.encode_container),
            "not": FieldLookup(Lookups.not_equal, ContainerValueEncoders.encode_container),
            "isnull": FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            "not_isnull": FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
        }

    @staticmethod
    def get_array_lookups(field: ArrayField) -> dict[str, FieldLookup]:
        """The lookups of an array.

        Args:
            field: The array field.

        Returns:
            The lookups by suffix.
        """
        return {
            **ContainerFieldLookups.get_whole_value_lookups(),
            # binds_by_rebuild: a plan builds the test of a later value again - the value is the
            # ValueWrapper of its ContainerLiteral.
            "contains": FieldLookup(
                ContainerLookups.contains, ContainerValueEncoders.encode_container, binds_by_rebuild=True
            ),
            "contained_by": FieldLookup(
                ContainerLookups.contained_by, ContainerValueEncoders.encode_container, binds_by_rebuild=True
            ),
            "overlap": FieldLookup(
                ContainerLookups.overlap, ContainerValueEncoders.encode_container, binds_by_rebuild=True
            ),
            "len": FieldLookup(ContainerLookups.length, ValueEncoders.encode_int),
            "item": FieldLookup(
                partial(ContainerLookups.item, element_field=field.base_field),
                ContainerValueEncoders.encode_array_item,
            ),
        }

    @staticmethod
    def get_map_lookups() -> dict[str, FieldLookup]:
        """The lookups of a map - its keys checked, its size compared.

        Args:

        Returns:
            The lookups by suffix.
        """
        return {
            **ContainerFieldLookups.get_whole_value_lookups(),
            "has_key": FieldLookup(ContainerLookups.has_key, ContainerValueEncoders.encode_map_key),
            "has_keys": FieldLookup(ContainerLookups.has_keys, ContainerValueEncoders.encode_map_keys),
            "has_any_keys": FieldLookup(ContainerLookups.has_any_keys, ContainerValueEncoders.encode_map_keys),
            "len": FieldLookup(ContainerLookups.length, ValueEncoders.encode_int),
        }
