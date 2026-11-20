from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from hare.fields.relations.declarations import RelationProperty
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance

if TYPE_CHECKING:
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding


class LazyRelationFields:
    """The attributes a model's relations get once the models are linked: a property per forward,
    backward and many-to-many relation reading the related rows when first read, and the key columns
    of each relation."""

    @staticmethod
    def generate_lazy_forward_relation_fields(meta: ModelBinding, keys: Iterable[str]) -> None:
        """Creates the lazy get/set/del properties of forward FK/O2O fields.

        Args:
            meta: The model's meta.
            keys: The names of the fields.
        """
        # Deferred import: the Model module imports this one.
        from hare.fields.relations.relation_accessors import RelationAccessors

        for key in keys:
            _key = f"_{key}"
            field_object: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any] = meta.fields_map[key]  # type: ignore[assignment]
            relation_fields = field_object.source_fields
            to_fields = tuple(to_field.model_field_name for to_field in field_object.to_field_instances)
            for relation_field in relation_fields:
                meta.foreign_key_shadow_columns[relation_field] = _key
            LazyRelationFields.set_relation_property(
                meta,
                key,
                RelationAccessors.make_foreign_key_property(
                    _key, field_object.related_model, relation_fields, to_fields
                ),
            )

    @staticmethod
    def generate_lazy_backward_relation_fields(
        meta: ModelBinding,
        keys: Iterable[str],
        make_property: Callable[[str, type[Model], tuple[str, ...], tuple[str, ...]], RelationProperty],
    ) -> None:
        """Creates the lazy get-only properties of backward FK/O2O fields - a ReverseRelation for the
        former, a single cached instance for the latter.

        Args:
            meta: The model's meta.
            keys: The names of the fields.
            make_property: Makes a field's property from the instance attribute holding its value, the
                related model, its relation fields and the fields of this model they reference.
        """
        for key in keys:
            _key = f"_{key}"
            field_object: BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any] = meta.fields_map[key]  # type: ignore[assignment]
            LazyRelationFields.set_relation_property(
                meta,
                key,
                make_property(
                    _key,
                    field_object.related_model,
                    field_object.relation_fields,
                    tuple(to_field.model_field_name for to_field in field_object.to_field_instances),
                ),
            )

    @staticmethod
    def generate_lazy_foreign_key_and_many_to_many_fields(meta: ModelBinding) -> None:
        """Puts the accessors of every relation on the model class - forward, backward and many-to-many.

        Args:
            meta: The model's meta.
        """
        # See LazyRelationFields.generate_lazy_forward_relation_fields() for why this import is deferred.
        from hare.fields.relations.relation_accessors import RelationAccessors

        LazyRelationFields.generate_lazy_forward_relation_fields(meta, meta.foreign_key_fields)
        LazyRelationFields.generate_lazy_backward_relation_fields(
            meta, meta.backward_foreign_key_fields, RelationAccessors.make_reverse_relation_property
        )
        LazyRelationFields.generate_lazy_forward_relation_fields(meta, meta.one_to_one_fields)
        LazyRelationFields.generate_lazy_backward_relation_fields(
            meta, meta.backward_one_to_one_fields, RelationAccessors.make_reverse_one_to_one_property
        )

        # Create lazy M2M fields on model.
        for key in meta.many_to_many_fields:
            _key = f"_{key}"
            field_object: ManyToManyFieldInstance[Any] = meta.fields_map[key]  # type: ignore[assignment]
            LazyRelationFields.set_relation_property(
                meta, key, RelationAccessors.make_many_to_many_property(_key, field_object)
            )

    @staticmethod
    def set_relation_property(meta: ModelBinding, key: str, relation_property: RelationProperty) -> None:
        """Keep an accessor on its structural binding, without changing model classes."""
        meta.relation_accessors[key] = relation_property
