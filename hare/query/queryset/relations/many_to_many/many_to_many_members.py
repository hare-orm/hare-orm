from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import IntegrityError, QueryError, ValidationError
from hare.query.key_columns import KeyColumns

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.field import Field
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class ManyToManyMembers:
    """The members given to add(), remove() and set(): instances or primary keys, checked to be of the
    related model and saved, fetched as instances through the related model's default scope, and
    their keys ready for the database."""

    @staticmethod
    def is_model_instance(member: Any) -> bool:
        """Whether a member is given as a model instance, not as a primary key value."""
        from hare.models import Model

        return isinstance(member, Model)

    @staticmethod
    def get_member_pk_db_values(
        relation: ManyToManyRelation[Any], member: Any, types: TypeRegistry
    ) -> tuple[Any, ...]:
        """The DB-ready primary key of a member - a related instance, or its primary key value (a
        tuple for a composite key).

        Args:
            relation: The many-to-many relation of an instance.
            member: A related instance or its primary key.
            types: The type registry of the through table's connection.

        Raises:
            ValidationError: ``member`` is None or an instance of another model, or a composite key value
                that isn't a tuple of one value per key field.
            QueryError: ``member`` is an unsaved instance.
        """
        related_meta = relation.model._meta
        if member is None or ManyToManyMembers.is_model_instance(member):
            ManyToManyMembers.validate_related_instances(relation, (member,))
            return KeyColumns.get_db_values(related_meta, member, types)
        if not related_meta.has_composite_primary_key:
            return (types.get_db_value(related_meta.pk, member, relation.model),)
        if not isinstance(member, tuple) or len(member) != len(related_meta.pk_fields):
            raise ValidationError(
                f"Invalid value for relationship field '{relation.field.model_field_name}': {member!r} - "
                f"{relation.model.__name__} has a composite primary key, pass an instance or a tuple of "
                f"{len(related_meta.pk_fields)} values"
            )
        return tuple(
            types.get_db_value(field, value, relation.model)
            for field, value in zip(related_meta.pk_fields, member, strict=True)
        )

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
        requested_values = {
            ManyToManyMembers.get_member_pk_db_values(relation, member, types): member
            for member in members
            if not ManyToManyMembers.is_model_instance(member)
        }
        related_meta = relation.model._meta
        fetched_instances = await (
            relation.model._meta.manager.get_queryset()
            .filter(pk__in=list(requested_values.values()))
            .using(connection)
        )
        fetched_by_pk_values = {
            KeyColumns.get_db_values(related_meta, instance, types): instance for instance in fetched_instances
        }
        if missing_values := [
            value for pk_values, value in requested_values.items() if pk_values not in fetched_by_pk_values
        ]:
            raise IntegrityError(
                f"Can't add {', '.join(repr(value) for value in missing_values)} to "
                f"'{relation.field.model_field_name}' - no {relation.model.__name__} with that primary key"
            )
        return tuple(
            member
            if ManyToManyMembers.is_model_instance(member)
            else fetched_by_pk_values[ManyToManyMembers.get_member_pk_db_values(relation, member, types)]
            for member in members
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
                    f"Invalid instance for relationship field '{relation.field.model_field_name}'. "
                    f"Expected model type '{relation.model.__name__}', but got '{type(instance).__name__}'."
                )
            if not instance._saved_in_db:
                raise QueryError(f"You should first call .save() on {instance!r}")

    @staticmethod
    def validate_owner_saved(relation: ManyToManyRelation[Any]) -> None:
        """Checks the instance owning this relation is saved.

        Args:
            relation: The many-to-many relation of an instance.

        Raises:
            QueryError: ``self.instance`` is not saved yet.
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
        return list(related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,))
