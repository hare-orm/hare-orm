from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.fields import Field

if TYPE_CHECKING:
    from typing import Self

    from hare.models.model_binding import ModelBinding


@dataclasses.dataclass
class ModelDescription:
    """A model's fields grouped by type, each group in declaration order - what the pydantic
    model creator builds a schema from. ``ModelDescription.from_binding(Book)`` describes a model.
    """

    #: The model's primary key, as one Field per component - a single-column PK is a 1-item
    #: list, a composite PK (CompositePrimaryKey) is one entry per component in declared order.
    pk_fields: list[Field[Any]]
    #: Every field holding a plain value, except the primary key - the key columns of forward
    #: relations (``author_id``) included.
    data_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Forward ``ForeignKeyField``s.
    foreign_key_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Backward sides of other models' ``ForeignKeyField``s pointing at this model.
    backward_foreign_key_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Forward ``OneToOneField``s.
    one_to_one_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Backward sides of other models' ``OneToOneField``s pointing at this model.
    backward_one_to_one_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: ``ManyToManyField``s, both the declared and the generated reverse ones.
    many_to_many_fields: list[Field[Any]] = dataclasses.field(default_factory=list)

    @staticmethod
    def _fields_in(binding: ModelBinding, names: Iterable[str]) -> list[Field[Any]]:
        return [field for name, field in binding.fields_map.items() if name in names]

    @classmethod
    def from_binding(cls, binding: ModelBinding) -> Self:
        """Describes a model's fields.

        Args:
            binding: The model; its relations must be initialised (``Hare.init()`` or
                ``Hare.bind_models()``).

        Returns:
            The description.
        """
        # The primary key's fields - one or several - are listed here, not among the data fields.
        primary_key_attribute_names = binding.primary_key_attribute_names
        # A blind index is hare's own column, written with the encrypted field - its HMAC is no data.
        blind_index_field_names = set(binding.blind_index_fields.values())
        return cls(
            pk_fields=[binding.fields_map[name] for name in primary_key_attribute_names],
            data_fields=cls._fields_in(
                binding,
                binding.fields - binding.fetch_fields - set(primary_key_attribute_names) - blind_index_field_names,
            ),
            foreign_key_fields=cls._fields_in(binding, binding.foreign_key_fields),
            backward_foreign_key_fields=cls._fields_in(binding, binding.backward_foreign_key_fields),
            one_to_one_fields=cls._fields_in(binding, binding.one_to_one_fields),
            backward_one_to_one_fields=cls._fields_in(binding, binding.backward_one_to_one_fields),
            many_to_many_fields=cls._fields_in(binding, binding.many_to_many_fields),
        )
