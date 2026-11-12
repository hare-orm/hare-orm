from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.classes.deferred_import import DeferredImport
from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.exceptions import IntegrityError, QueryError, ValidationError
from hare.query.key_columns import KeyColumns

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class ManyToManyMembers:
    """The members given to add(), remove() and set(): instances or primary keys, checked to be of the
    related model and saved, fetched as instances through the related model's default scope, and
    their keys ready for the database."""

    #: ``Model`` - imported at its first read, as its module imports this one.
    model_class: ClassVar[DeferredImport[type[Model]]] = DeferredImport("hare.models", "Model")

    @staticmethod
    def get_connection_of(model: type[Model], connection: DatabaseClient) -> DatabaseClient:
        """The connection the rows of ``model`` are read and written on in a write of the relation -
        ``connection``, the through table's, when the model is on it; the model's own otherwise, for a
        relation to a model on another connection.

        Args:
            model: A model of the relation.
            connection: The connection the through table is written on.

        Returns:
            The connection.
        """
        model_connection = ModelConnectionChoice.get_metadata_connection(model, for_write=True)
        if connection.connection_alias == model_connection.connection_alias:
            return connection
        return model.get_connection(for_write=True)

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
        related_meta = relation.model._meta
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
        relation: ManyToManyRelation[TModel], members: tuple[Any, ...], connection: DatabaseClient | None
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
        types = (connection or relation.model.get_connection()).dialect.types
        meta = relation.model._meta
        writers = KeyColumns.get_instance_writers(meta, types)
        member_keys = [
            None
            if ManyToManyMembers.is_model_instance(member)
            else ManyToManyMembers.get_member_pk_db_values(relation, member, writers=writers)
            for member in members
        ]
        requested_values = {key: member for member, key in zip(members, member_keys, strict=True) if key is not None}
        related_meta = relation.model._meta
        fetched_instances = await (
            relation.model._meta.manager.create_queryset()
            .filter(pk__in=list(requested_values.values()))
            .using(connection)
        )
        fetched_by_pk_values = dict(
            zip(KeyColumns.get_db_rows(related_meta, fetched_instances, types), fetched_instances, strict=True)
        )
        if missing_values := [
            value for pk_values, value in requested_values.items() if pk_values not in fetched_by_pk_values
        ]:
            raise IntegrityError(
                f"Can't add {', '.join(repr(value) for value in missing_values)} to "
                f"{relation.field.model_field_name!r} - no {relation.model.__name__} with that primary key"
            )
        return tuple(
            member if key is None else fetched_by_pk_values[key]
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

    @staticmethod
    def get_forward_key_fields(relation: ManyToManyRelation[Any]) -> list[Field[Any] | None]:
        """The field of each forward key column - the related model's primary key field(s) - for
        binding a long list of their values.

        Args:
            relation: The many-to-many relation of an instance.

        Returns:
            One field per forward key column.
        """
        related_meta = relation.model._meta
        return list(related_meta.pk_fields)
