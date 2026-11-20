from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import chain
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.classes.deferred_import import DeferredImport
from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.exceptions import IntegrityError, QueryError, ValidationError
from hare.models.instances.instance_origins import InstanceOrigins
from hare.query.key_columns import KeyColumns
from hare.query.key_comparison import KeyComparison

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class ManyToManyMembers:
    """The members given to add(), remove() and set(): instances or primary keys, checked to be of the
    related model and saved, fetched as instances through the related model's default scope, and
    their keys ready for the database."""

    #: ``Model`` - imported at its first read, as its module imports this one.
    model_class: ClassVar[DeferredImport[type[Model]]] = DeferredImport("hare.models", "Model")

    @staticmethod
    async def get_set_members(relation: ManyToManyRelation[Any], members: tuple[Any, ...]) -> tuple[Any, ...]:
        """Separate one composite key from a collection of related members for ``set()``.

        A flat tuple with one value per primary-key column names one member. Tuples of
        instances or key tuples remain collections, as do lists and other iterable inputs.
        The ordinary member conversion still validates and writes every resulting key.

        Args:
            relation: The relation receiving the members.
            members: The positional arguments of ``set()``.

        Returns:
            The members, with a single composite key kept intact.
        """
        meta: ModelBinding = relation._binding
        if (
            meta.has_composite_primary_key
            and len(members) == 1
            and isinstance(key := members[0], tuple)
            and len(key) == len(meta.pk_fields)
            and not any(isinstance(value, tuple) or ManyToManyMembers.is_model_instance(value) for value in key)
        ):
            return members
        return await relation._get_set_members(members)

    @staticmethod
    def get_connection_of(
        relation: ManyToManyRelation[Any], model: type[Model], connection: DatabaseClient
    ) -> DatabaseClient:
        """The connection the rows of ``model`` are read and written on in a write of the relation -
        ``connection``, the through table's, when the model is on it; the model's own otherwise, for a
        relation to a model on another connection.

        Args:
            relation: The relation whose forward declaration selects the source route.
            model: A model of the relation.
            connection: The connection the through table is written on.

        Returns:
            The connection.
        """
        from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows

        target_binding = relation._binding.graph.get_binding(model)
        selected = ModelConnectionChoice.get_related_write_client(
            relation._context,  # type: ignore[arg-type]
            target_binding,
            relation._binding.graph.get_binding(ThroughRows.get_declaring_model(relation)),
            connection,
        )
        return selected or ModelConnectionChoice.get(
            relation._context,  # type: ignore[arg-type]
            target_binding,
            True,
        )

    @staticmethod
    def is_model_instance(member: Any) -> bool:
        """Whether a member is given as a model instance, not as a primary key value."""

        return isinstance(member, ManyToManyMembers.model_class)

    @staticmethod
    def get_member_pk_db_values(
        relation: ManyToManyRelation[Any],
        member: Any,
        *,
        writers: Sequence[tuple[str, Callable[[Any, Any], Any]]],
    ) -> tuple[Any, ...]:
        """The DB-ready primary key of a member - a related instance, or its primary key value (a
        tuple for a composite key).

        Args:
            relation: The many-to-many relation of an instance.
            member: A related instance or its primary key.
            writers: Prepared attribute names and writers for the related primary key.

        Raises:
            ValidationError: ``member`` is None or an instance of another model, or a composite key value
                that isn't a tuple of one value per key field.
            QueryError: ``member`` is an unsaved instance.
        """
        related_meta: ModelBinding = relation._binding
        if member is None or ManyToManyMembers.is_model_instance(member):
            ManyToManyMembers.validate_related_instances(relation, (member,))
            return tuple(write(getattr(member, name), member) for name, write in writers)
        if not related_meta.has_composite_primary_key:
            return (writers[0][1](member, relation.model),)
        if not isinstance(member, tuple) or len(member) != len(related_meta.pk_fields):
            raise ValidationError(
                f"Invalid value for relationship field {relation.field.model_field_name!r}: {member!r} - "
                f"{relation.model.__name__} has a composite primary key, pass an instance or a tuple of "
                f"{len(related_meta.pk_fields)} values"
            )
        return tuple(write(value, relation.model) for (_name, write), value in zip(writers, member, strict=True))

    @staticmethod
    async def get_related_instances(
        relation: ManyToManyRelation[TModel], members: tuple[Any, ...], connection: DatabaseClient
    ) -> tuple[TModel, ...]:
        """``members`` as related instances - a primary key value is fetched through the related
        model's default scope.

        Args:
            relation: The many-to-many relation of an instance.
            members: Related instances or their primary key values.
            connection: Connection to fetch on, None for the related model's own.

        Raises:
            IntegrityError: No row the default scope shows has one of the primary key values.
        """
        if all(ManyToManyMembers.is_model_instance(member) for member in members):
            return members
        types = connection.dialect.types
        writers = KeyColumns.get_instance_writers(relation._binding, types)
        member_keys = [
            None
            if ManyToManyMembers.is_model_instance(member)
            else ManyToManyMembers.get_member_pk_db_values(relation, member, writers=writers)
            for member in members
        ]
        requested_values = {key: member for member, key in zip(members, member_keys, strict=True) if key is not None}
        related_meta: ModelBinding = relation._binding
        fetched_instances = await (
            relation._binding.manager.create_queryset(binding=relation._binding, context=relation._context)
            .filter(pk__in=list(requested_values.values()))
            .using(connection)
        )
        fetched_keys = KeyColumns.get_db_rows(related_meta, fetched_instances, types)
        read_key = await KeyComparison.get_key_reader(
            connection, related_meta.pk_fields, chain(requested_values, fetched_keys)
        )
        fetched_by_key = dict(zip(map(read_key, fetched_keys), fetched_instances, strict=True))
        if missing_values := [
            value for pk_values, value in requested_values.items() if read_key(pk_values) not in fetched_by_key
        ]:
            raise IntegrityError(
                f"Can't add {', '.join(repr(value) for value in missing_values)} to "
                f"{relation.field.model_field_name!r} - no {relation.model.__name__} with that primary key"
            )
        return tuple(
            member if key is None else fetched_by_key[read_key(key)]
            for member, key in zip(members, member_keys, strict=True)
        )

    @staticmethod
    def validate_related_instances(relation: ManyToManyRelation[Any], instances: tuple[Any, ...]) -> None:
        """Checks every one of ``instances`` is a saved instance of this relation's related model.

        Args:
            relation: The many-to-many relation of an instance.
            instances: The instances given to the call.

        Raises:
            ValidationError: An instance is not of the related model's type.
            QueryError: An instance is not saved yet.
        """
        for instance in instances:
            if type(instance) is not relation.model:
                raise ValidationError(
                    f"Invalid instance for relationship field {relation.field.model_field_name!r}. "
                    f"Expected model type {relation.model.__name__!r}, but got {type(instance).__name__!r}."
                )
            InstanceOrigins.check(relation._binding, relation._context, instance, "Many-to-many relation")
            if not instance._saved_in_db:
                raise QueryError(f"You should first call .save() on {instance!r}")

    @staticmethod
    def validate_owner_saved(relation: ManyToManyRelation[Any]) -> None:
        """Checks the instance owning this relation is saved.

        Args:
            relation: The many-to-many relation of an instance.

        Raises:
            QueryError: ``relation.instance`` is not saved yet.
        """
        if not relation.instance._saved_in_db:
            raise QueryError(f"You should first call .save() on {relation.instance!r}")
