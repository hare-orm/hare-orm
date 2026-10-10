from __future__ import annotations

import weakref
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.exceptions import IncompleteInstanceError
from hare.fields.enums import RelationLoadStrategy
from hare.fields.relations.declarations import RelationProperty
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.query.queryset import QuerySet, QuerySetSingle
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.queryset.relations.reverse_relation import ReverseRelation
from hare.query.queryset.single_rows.none_awaitable_type import NoneAwaitable

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class RelationAccessors:
    """The attributes of an instance's relations: the properties MetaInfo installs for each relation,
    and the related queryset they read. A property's functions are closures over the relation's
    names - read on every attribute access, they take no keyword arguments to merge."""

    @staticmethod
    def make_foreign_key_property(
        _key: str, related_model: type[Model], relation_fields: tuple[str, ...], to_fields: tuple[str, ...]
    ) -> RelationProperty:
        """The property of a forward foreign key or one-to-one relation.

        Args:
            _key: The instance attribute holding the related instance.
            related_model: The related model.
            relation_fields: The instance's fields holding the related row's key.
            to_fields: The related model's fields the key references.

        Returns:
            The property.
        """
        usage = f"Assigning '{_key.removeprefix('_')}'"
        empty_values = (None,) * len(to_fields)

        def get_related_obj(obj: Model) -> Awaitable[Model | None]:
            try:
                return getattr(obj, _key)
            except AttributeError:
                values = [getattr(obj, relation_field) for relation_field in relation_fields]
                set_values = [value for value in values if value is not None]
                if not set_values:
                    return NoneAwaitable
                if len(set_values) != len(values):
                    # Some key columns of a composite relation set and some not: neither a reference
                    # nor "no relation".
                    raise IncompleteInstanceError(
                        f"{_key!r} on {type(obj).__name__} has only some of its composite key "
                        f"columns set ({dict(zip(relation_fields, values, strict=True))!r}) - set all "
                        "of them, or none."
                    )
                return RelationAccessors.get_related_queryset(
                    related_model, dict(zip(to_fields, values, strict=True)), obj
                ).first()

        def set_related_obj(obj: Model, value: Model | None) -> None:
            to_field_values = (
                RelationValues.get_relation_key_values(value, to_fields, usage) if value else empty_values
            )
            for relation_field, to_field_value in zip(relation_fields, to_field_values, strict=True):
                setattr(obj, relation_field, to_field_value)
            setattr(obj, _key, value)

        def clear_related_obj(obj: Model) -> None:
            set_related_obj(obj, None)

        return RelationProperty(get_related_obj, set_related_obj, clear_related_obj)

    @staticmethod
    def make_reverse_relation_property(
        _key: str, related_model: type[Model], relation_fields: tuple[str, ...], from_fields: tuple[str, ...]
    ) -> RelationProperty:
        """The property of a backward foreign key - the instance's ReverseRelation, made again only
        once nothing holds the one made before (``RelationRows``).

        Args:
            _key: The instance attribute holding the relation.
            related_model: The related model.
            relation_fields: The related model's fields referencing the instance.
            from_fields: The instance's fields they reference.

        Returns:
            The property.
        """

        def get_relation(obj: Model, bind: bool = True) -> ReverseRelation[Model]:
            relation_rows: RelationRows | None = getattr(obj, _key, None)
            relation: ReverseRelation[Model] | None
            if relation_rows is None:
                relation_rows = RelationRows()
                setattr(obj, _key, relation_rows)
                relation = None
            else:
                relation = relation_rows.held_relation  # type: ignore[assignment]
                if relation is None and (relation_reference := relation_rows.relation_reference) is not None:
                    relation = relation_reference()  # type: ignore[assignment]
            if relation is None:
                # The relation class itself for a manager of plain querysets - get_class_for() answers
                # the same, one call later.
                relation_class: type[ReverseRelation[Model]] = (
                    ReverseRelation
                    if related_model._meta.manager.queryset_class is QuerySet
                    else ReverseRelation.get_class_for(related_model)
                )
                relation = relation_class(related_model, relation_fields, obj, from_fields, relation_rows)
                if relation_rows._fetched:
                    relation_rows.relation_reference = weakref.ref(relation)
                else:
                    relation_rows.held_relation = relation
            # Fetched rows not bound yet are bound on the first use of a query setting - often never.
            if bind and (relation._is_bound or not relation_rows._fetched):
                relation._bind()
            return relation

        return RelationProperty(get_relation)

    @staticmethod
    def make_reverse_one_to_one_property(
        _key: str, related_model: type[Model], relation_fields: tuple[str, ...], from_fields: tuple[str, ...]
    ) -> RelationProperty:
        """The property of a backward one-to-one relation.

        Args:
            _key: The instance attribute holding the related instance select_related() or
                prefetch_related() set.
            related_model: The related model.
            relation_fields: The related model's fields referencing the instance.
            from_fields: The instance's fields they reference.

        Returns:
            The property.
        """
        usage = f"Reading '{_key.removeprefix('_')}'"

        def get_related_obj(obj: Model) -> QuerySetSingle[Model | None]:
            # Not cached: the fields the relation is read by can still change (an unsaved obj, a
            # clone given a new pk). A value select_related()/prefetch_related() set is returned as is.
            if hasattr(obj, _key):
                return getattr(obj, _key)
            values = RelationValues.get_relation_key_values(obj, from_fields, usage)
            if None in values:
                # A NULL target value (a nullable to_field=, or an unsaved pk) is referenced by no row.
                return cast("QuerySetSingle[Model | None]", NoneAwaitable)
            return RelationAccessors.get_related_queryset(
                related_model, dict(zip(relation_fields, values, strict=True)), obj
            ).first()

        return RelationProperty(get_related_obj)

    @staticmethod
    def make_many_to_many_property(_key: str, field_object: ManyToManyFieldInstance[Model]) -> RelationProperty:
        """The property of a many-to-many relation - the instance's ManyToManyRelation, made again only
        once nothing holds the one made before (``RelationRows``).

        Args:
            _key: The instance attribute holding the relation.
            field_object: The many-to-many field.

        Returns:
            The property.
        """

        def get_relation(obj: Model, bind: bool = True) -> ManyToManyRelation[Model]:
            relation_rows: RelationRows | None = getattr(obj, _key, None)
            relation: ManyToManyRelation[Model] | None
            if relation_rows is None:
                relation_rows = RelationRows()
                setattr(obj, _key, relation_rows)
                relation = None
            else:
                relation = relation_rows.held_relation  # type: ignore[assignment]
                if relation is None and (relation_reference := relation_rows.relation_reference) is not None:
                    relation = relation_reference()  # type: ignore[assignment]
            if relation is None:
                # The relation class itself for a manager of plain querysets - get_class_for() answers
                # the same, one call later.
                many_to_many_class: type[ManyToManyRelation[Model]] = (
                    ManyToManyRelation
                    if field_object.related_model._meta.manager.queryset_class is QuerySet
                    else ManyToManyRelation.get_class_for(field_object.related_model)
                )
                relation = many_to_many_class(obj, field_object, relation_rows)
                if relation_rows._fetched:
                    relation_rows.relation_reference = weakref.ref(relation)
                else:
                    relation_rows.held_relation = relation
            # Fetched rows not bound yet are bound on the first use of a query setting - often never.
            if bind and (relation._is_bound or not relation_rows._fetched):
                relation._bind()
            return relation

        return RelationProperty(get_relation)

    @staticmethod
    def get_relation(obj: Model, field_name: str) -> RelatedQuerySet[Any]:
        """The to-many relation ``field_name`` of the obj as the holder of its fetched rows -
        not made ready to query, which an unsaved obj can't be.

        Args:
            obj: The model obj.
            field_name: A backward foreign key or many-to-many field name.

        Returns:
            The relation.
        """
        return cast("RelatedQuerySet[Any]", getattr(type(obj), field_name).fget(obj, bind=False))

    @staticmethod
    def get_related_queryset(model: type[TModel], filters: dict[str, Any], obj: Model) -> QuerySet[TModel]:
        """A new queryset of this model's rows a relation of ``obj`` points at, read on the
        connection ``obj`` came from.

        Args:
            model: The model.
            filters: The relation field names of this model to the values they must equal.
            obj: The obj the relation is read from.

        Returns:
            The queryset.
        """
        queryset = cast("QuerySet[TModel]", model._meta.manager.get_queryset())
        queryset._append_filters(False, (), filters)
        queryset._set_instance_connection(obj)
        return queryset

    @staticmethod
    def get_lazy_joined_relation_names_needing_refresh(obj: Model, refresh_fields: set[str]) -> frozenset[str]:
        """Forward FK/O2O relation names declared ``lazy="joined"`` whose own shadow column(s)
        are among ``refresh_fields``.

        Args:
            obj: The model obj.
            refresh_fields: The field names a partial ``refresh_from_db(fields=...)`` is about
                to refresh.
        Returns:
            The matching relation names.
        """
        relation_names_needing_refresh = set()
        for relation_name in obj._meta.foreign_key_fields | obj._meta.one_to_one_fields:
            relation_field = cast("RelationalField[Any]", obj._meta.fields_map[relation_name])
            if getattr(relation_field, "lazy", None) != RelationLoadStrategy.JOINED:
                continue
            if set(relation_field.source_fields) & refresh_fields:
                relation_names_needing_refresh.add(relation_name)
        return frozenset(relation_names_needing_refresh)
