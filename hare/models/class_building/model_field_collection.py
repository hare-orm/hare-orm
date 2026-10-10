from __future__ import annotations

from copy import deepcopy
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
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
    def search_for_field_attributes(base: type, attributes: dict[str, Any], name: str) -> None:
        """Collects the field attributes of ``base`` and its ancestors into ``attrs`` - a name already
        there is kept, so a derived class wins; several bases are read left to right.

        A base that is a model already has its fields resolved (``_meta.fields_map``) - they are
        taken before its ancestors are walked. The walk sets the field order; a diamond's value is
        corrected by ``ModelFieldCollection.apply_diamond_field_precedence()``.

        ``name`` is the class being built, for an error message.
        """
        if meta := getattr(base, "_meta", None):
            # Deep-copied: concrete subclasses of one abstract base must not share Field instances -
            # each is bound to its model.
            for key, value in meta.fields_map.items():
                if key not in attributes:
                    attributes[key] = deepcopy(value)
            # A composite primary key's marker is consumed when the abstract base is built - made
            # anew for each concrete subclass.
            if meta.has_composite_primary_key and meta.primary_key_attribute:
                existing = next(
                    (value for value in attributes.values() if isinstance(value, CompositePrimaryKey)), None
                )
                if existing is not None and existing.field_names != meta.primary_key_attribute:
                    # Another base contributed a different composite primary key. The same one
                    # reached through two branches of a diamond is no conflict.
                    raise ConfigurationError(
                        f"Can't create model {name} with two CompositePrimaryKey declarations "
                        "inherited from different abstract base classes"
                    )
                if existing is None:
                    attributes["__inherited_composite_pk__"] = CompositePrimaryKey(
                        *meta.primary_key_attribute, without_overlaps=meta.pk_without_overlaps
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
            # For mixin classes. deepcopy for the same reason as the abstract-base branch above -
            # two concrete models sharing the same mixin would otherwise share the literal same
            # Field instances too.
            for key, value in base.__dict__.items():
                if isinstance(value, Field) and key not in attributes:
                    attributes[key] = deepcopy(value)
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
            if meta := getattr(ancestor, "_meta", None):
                # Only the ancestor's own declared fields count - what it inherited belongs to its
                # own ancestors, which get their turn.
                for key in meta.own_field_names:
                    winning_value_by_key.setdefault(key, meta.fields_map[key])
            else:
                # A plain mixin (no _meta at all) has no "own vs inherited" split to make - a
                # Field instance in its own __dict__ is unambiguously its own declaration.
                for key, value in ancestor.__dict__.items():
                    if isinstance(value, Field):
                        winning_value_by_key.setdefault(key, value)
        for key, value in winning_value_by_key.items():
            if key in attributes:
                attributes[key] = deepcopy(value)

    @staticmethod
    def dispatch_fields(
        attributes: dict[str, Any], fields_db_projection: dict[str, str], is_abstract: bool
    ) -> tuple[
        dict[str, Field[Any]],
        set[str],
        set[str],
        set[str],
    ]:
        fields_map: dict[str, Field[Any]] = {}
        foreign_key_fields: set[str] = set()
        many_to_many_fields: set[str] = set()
        one_to_one_fields: set[str] = set()
        for key, value in attributes.items():
            if isinstance(value, Field):
                if is_abstract:
                    value = deepcopy(value)

                fields_map[key] = value
                value.model_field_name = key

                if isinstance(value, OneToOneFieldInstance):
                    one_to_one_fields.add(key)
                elif isinstance(value, ForeignKeyFieldInstance):
                    foreign_key_fields.add(key)
                elif isinstance(value, ManyToManyFieldInstance):
                    many_to_many_fields.add(key)
                else:
                    fields_db_projection[key] = value.source_field or key
        return (fields_map, foreign_key_fields, many_to_many_fields, one_to_one_fields)

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
        # hare.models.model imports ModelMeta from this module at module level, so a top-level
        # import here would be circular - deferred to first use. Safe because this only ever
        # runs well after both hare.models.model and hare.models.model_meta have fully loaded.
        from hare.models.model import Model

        # The whole MRO: Model's methods live on the classes it is composed from, not in
        # Model.__dict__.
        reserved_names = frozenset(
            {key for base in Model.__mro__ for key in base.__dict__ if not key.startswith("__")}
            | {key for key in ModelMeta.__dict__ if not key.startswith("__")}
        )
        ModelFieldCollection.reserved_field_names[()] = reserved_names
        return reserved_names
