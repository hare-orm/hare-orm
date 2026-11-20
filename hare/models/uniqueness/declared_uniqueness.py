from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


@dataclasses.dataclass(frozen=True)
class DeclaredUniqueness:
    """A set of fields a model declares unique - its primary key, a field of ``unique=True``, a
    ``UniqueConstraint``, an ``Index(unique=True)`` - as the attributes of an instance holding them.

    Attributes:
        attribute_names: The attributes holding the fields' values - a relation's key attribute for a
            relation.
        condition: The condition of a partial uniqueness - the rows it holds for; None for every row.
    """

    attribute_names: tuple[str, ...]
    condition: Any = None

    @classmethod
    def get_declared(cls, binding: ModelBinding) -> list[DeclaredUniqueness]:
        """The sets of fields a model declares unique.

        Args:
            binding: The model.

        Returns:
            Each set, once.
        """
        # A model without a primary key (``Meta.primary_key = None``) declares no uniqueness of it.
        declared = (
            [cls(cls.get_attribute_names(binding, binding.primary_key_attribute_names))]
            if binding.primary_key_attribute_names
            else []
        )
        relation_names = binding.forward_key_relation_names
        for field_name, field in binding.fields_map.items():
            if (
                getattr(field, "unique", False)
                and not getattr(field, "pk", False)
                and (field_name in binding.fields_db_projection or field_name in relation_names)
            ):
                declared.append(cls(cls.get_attribute_names(binding, (field_name,))))
        for constraint in binding.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.fields:
                declared.append(cls(cls.get_attribute_names(binding, constraint.fields), constraint.condition))
        for index in binding.indexes:
            if isinstance(index, Index) and index.unique and index.fields and not index.declared_expressions:
                declared.append(cls(cls.get_attribute_names(binding, index.fields), getattr(index, "condition", None)))
        return list(dict.fromkeys(declared))

    @staticmethod
    def is_declared_by(schema_object: Any) -> bool:
        """Whether a constraint or an index of ``Meta`` declares a uniqueness.

        Args:
            schema_object: The constraint or the index.

        Returns:
            Whether it does.
        """
        return isinstance(schema_object, UniqueConstraint) or (
            isinstance(schema_object, Index) and schema_object.unique
        )

    @staticmethod
    def get_attribute_names(binding: ModelBinding, field_names: Any) -> tuple[str, ...]:
        """The attributes of an instance holding fields' values.

        Args:
            binding: The model.
            field_names: The fields.

        Returns:
            Each field's attribute - a relation's key attributes in its place.
        """
        attribute_names = []
        relation_names = binding.forward_key_relation_names
        for field_name in field_names:
            field_name = field_name.lstrip("-")
            field = binding.fields_map[field_name]
            source_fields = getattr(field, "source_fields", ())
            if field_name in relation_names and source_fields:
                attribute_names.extend(source_fields)
            else:
                attribute_names.append(field_name)
        return tuple(attribute_names)
