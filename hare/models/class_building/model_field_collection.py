from __future__ import annotations

from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.class_building.model_managers import ModelManagers


class ModelFieldCollection:
    """The fields a model class gets: collected from its bases - a derived class winning, a diamond
    resolved in MRO order - and from its own body, sorted into data fields, relations and the rest,
    with names that clash with each other or with reserved attributes rejected."""

    #: () -> the names a model field may not use (``get_reserved_field_names()``).
    reserved_field_names: ClassVar[Cache[frozenset[str]]] = Cache(Cache.max_size_from_env())

    @staticmethod
    def snapshot_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
        """Separate class declarations from fields shared with other model bodies."""
        return {
            name: value.snapshot() if isinstance(value, (Field, GenericForeignKeyFieldInstance)) else value
            for name, value in attributes.items()
        }

    @staticmethod
    def is_declared_field(field: Field[Any]) -> bool:
        """Exclude generated relation sides and key columns when inheriting declarations."""
        return (
            not isinstance(field, BackwardForeignKeyRelation)
            and field.reference is None
            and getattr(field, "generic_relation", None) is None
            and not (isinstance(field, ManyToManyFieldInstance) and field._generated)
        )

    @staticmethod
    def search_for_field_attributes(base: type, attributes: dict[str, Any], name: str) -> None:
        """Collects the field attributes of ``base`` and its ancestors into ``attributes`` - a name already
        there is kept, so a derived class wins; several bases are read left to right.

        A model base retains its original fields in ``_declaration.fields_map`` - they are
        taken before its ancestors are walked. The walk sets the field order; a diamond's value is
        corrected by ``ModelFieldCollection.apply_diamond_field_precedence()``.

        ``name`` is the class being built, for an error message.
        """
        if meta := getattr(base, "_declaration", None):
            # Inherit declarations; relation binding creates key columns and reverse sides again.
            for key, value in meta.fields_map.items():
                if key not in attributes and ModelFieldCollection.is_declared_field(value):
                    attributes[key] = value.snapshot()
            # A composite primary key's marker is consumed when the abstract base is built - made
            # anew for each concrete subclass.
            declared_primary_key = meta.primary_key_attribute
            if isinstance(declared_primary_key, tuple) and declared_primary_key:
                existing = next(
                    (value for value in attributes.values() if isinstance(value, CompositePrimaryKey)), None
                )
                if existing is not None and existing.field_names != declared_primary_key:
                    # Another base contributed a different composite primary key. The same one
                    # reached through two branches of a diamond is no conflict.
                    raise ConfigurationError(
                        f"Can't create model {name} with two CompositePrimaryKey declarations "
                        "inherited from different abstract base classes"
                    )
                if existing is None:
                    attributes["__inherited_composite_pk__"] = CompositePrimaryKey(
                        *declared_primary_key, without_overlaps=meta.pk_without_overlaps
                    )
            GenericForeignKeys.copy_inherited(base, attributes)
            # Every manager attribute of a Model base, copied with its constructor state so each
            # subclass gets its own instance to bind.
            ModelManagers.copy_manager_attributes(base, attributes)
            for parent in base.__mro__[1:]:
                # Searching for Field attributes in the class hierarchy - see this method's own
                # docstring for why this runs AFTER base's own contribution above, not before.
                ModelFieldCollection.search_for_field_attributes(parent, attributes, name)
        else:
            for parent in base.__mro__[1:]:
                # Searching for Field attributes in the class hierarchy
                ModelFieldCollection.search_for_field_attributes(parent, attributes, name)
            # A mixin declaration is independent in each concrete model too.
            for key, value in base.__dict__.items():
                if isinstance(value, Field) and key not in attributes:
                    attributes[key] = value.snapshot()
            GenericForeignKeys.copy_inherited(base, attributes)
            # A manager declared on a plain mixin is bound to each concrete model the same way a
            # manager inherited from an abstract Model base is - otherwise it stays unbound
            # (model=None) and every query through it crashes.
            ModelManagers.copy_manager_attributes(base, attributes)

    @staticmethod
    def apply_diamond_field_precedence(canonical_mro: tuple[type, ...], attributes: dict[str, Any]) -> None:
        """Corrects, in place, the value of a field name the per-base walk took from the wrong ancestor
        of a diamond - a base overriding it wins over one passing the common ancestor's value
        through. The field order stays.

        Args:
            canonical_mro: Every ancestor of the class being built, nearest first.
            attributes: The field attributes collected, corrected in place.
        """
        winning_value_by_key: dict[str, Field[Any]] = {}
        for ancestor in canonical_mro:
            if meta := getattr(ancestor, "_declaration", None):
                # Only the ancestor's own declared fields count - what it inherited belongs to its
                # own ancestors, which get their turn.
                for key in meta.declared_field_names:
                    winning_value_by_key.setdefault(key, meta.fields_map[key])
            else:
                # A plain mixin (no model declaration) has no "own vs inherited" split to make - a
                # Field instance in its own __dict__ is unambiguously its own declaration.
                for key, value in ancestor.__dict__.items():
                    if isinstance(value, Field):
                        winning_value_by_key.setdefault(key, value)
        for key, value in winning_value_by_key.items():
            if key in attributes:
                attributes[key] = value.snapshot()

    @staticmethod
    def get_declared_fields(attributes: dict[str, Any]) -> dict[str, Field[Any]]:
        """Collect and name the fields of a model declaration."""
        fields: dict[str, Field[Any]] = {}
        for name, value in attributes.items():
            if isinstance(value, Field):
                value.model_field_name = name
                fields[name] = value
        return fields

    @staticmethod
    def dispatch_fields(
        fields: dict[str, Field[Any]], fields_db_projection: dict[str, str]
    ) -> tuple[set[str], set[str], set[str]]:
        """Classify the bound relations and fill the projection for ordinary columns."""
        foreign_key_fields: set[str] = set()
        many_to_many_fields: set[str] = set()
        one_to_one_fields: set[str] = set()
        for name, field in fields.items():
            field.model_field_name = name
            if isinstance(field, OneToOneFieldInstance):
                one_to_one_fields.add(name)
            elif isinstance(field, ForeignKeyFieldInstance):
                foreign_key_fields.add(name)
            elif isinstance(field, ManyToManyFieldInstance):
                many_to_many_fields.add(name)
            else:
                fields_db_projection[name] = field.source_field or name
        return foreign_key_fields, many_to_many_fields, one_to_one_fields

    @staticmethod
    def check_field_name_conflicts(fields_map: dict[str, Field[Any]], name: str) -> None:
        reserved_names = ModelFieldCollection.get_reserved_field_names()
        conflicts = sorted(set(fields_map).intersection(reserved_names))
        if conflicts:
            conflict_list = ", ".join(conflicts)
            raise ConfigurationError(
                f"Model {name} has field name(s) that conflict with default Model attributes: {conflict_list}"
            )

    @staticmethod
    def get_reserved_field_names() -> frozenset[str]:
        """Names a model field may not use because they would shadow a Model or ModelMeta attribute -
        worked out once, not for every model class.

        Returns:
            Every non-dunder attribute name on Model's full MRO and on ModelMeta.
        """
        # Local import: the metaclass module imports this module.
        from hare.models.model_meta import ModelMeta

        reserved_names = ModelFieldCollection.reserved_field_names.get(())
        if reserved_names is not None:
            return reserved_names
        # Imported here: hare.models.model imports the metaclass, which imports this module.
        from hare.models.model import Model

        # The whole MRO: Model's methods live on the classes it is composed from, not in
        # Model.__dict__.
        reserved_names = frozenset(
            {
                key
                for base in Model.__mro__
                for key in (*base.__dict__, *base.__dict__.get("__annotations__", {}))
                if not key.startswith("__")
            }
            | {key for key in ModelMeta.__dict__ if not key.startswith("__")}
        )
        ModelFieldCollection.reserved_field_names[()] = reserved_names
        return reserved_names
