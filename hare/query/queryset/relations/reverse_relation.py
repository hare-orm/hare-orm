from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.core.connections.connections import Connections
from hare.exceptions import (
    DoesNotExist,
    IntegrityError,
    QueryError,
    ValidationError,
)
from hare.fields.relations.relation_values import RelationValues
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.write.write_steps import WriteSteps
from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.raw_sql_term import RawSQLTerm
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.expressions import Q
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery

TModel = TypeVar("TModel", bound="Model")


class ReverseRelation(RelatedQuerySet[TModel]):
    """
    The relation of a backward ``ForeignKeyField``.
    """

    __slots__ = ()

    def _get_write_connection(self, using: str | DatabaseClient | None = None) -> DatabaseClient:
        """The connection a write to the related rows goes to: the one given, else the router's
        choice, else the connection the parent instance was loaded from or saved to, else the
        related model's default connection - the connection its reads already use.

        Args:
            using: The connection given to the write.

        Returns:
            The connection.
        """
        if (connection := Connections.get_client(using)) is not None:
            return connection
        return InstanceConnections.get_connection_for_instance(self.instance, True, model=self.model)

    async def create(self, *, using: str | DatabaseClient | None = None, **kwargs: Any) -> TModel:
        """
        Creates a row of the related model pointing at the parent instance, and returns its object.

        .. code-block:: python3

            tournament = await Tournament.objects.create(name="...")
            event = await tournament.events.create(...)

        Args:
            using: Specific DB connection to use instead of default bound.
            kwargs: Model parameters for the new object.

        Raises:
            QueryError: The parent instance is not saved, or its referenced ``to_field`` value is
                NULL; or ``kwargs`` points the new object's foreign key at a different row than
                the parent instance.
        """
        created_instance = await self._get_model_queryset(self._get_write_connection(using)).create(
            **self._get_kwargs_with_parent_key(kwargs, "create")
        )
        self._invalidate_local_cache()
        return created_instance

    def bulk_create(
        self,
        objects: Iterable[TModel],
        batch_size: int | None = None,
        *,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        on_conflict_constraint: str | None = None,
        conflict_where: Q | RawSQLTerm | None = None,
        returning: bool | None = None,
        use_copy: bool = False,
    ) -> BulkCreateQuery[TModel]:
        """
        Inserts ``objects``, the foreign key of each pointed at the parent instance - see
        ``QuerySet.bulk_create()``.

        .. code-block:: python3

            await tournament.events.bulk_create([Event(name="a"), Event(name="b")])

        Raises:
            QueryError: The parent instance is not saved, or its referenced value is NULL; or an
                object's foreign key points at a different row than the parent.
            ValidationError: One of ``objects`` is not an instance of the related model.
        """
        objects = list(objects)
        parent_values = self._get_parent_key_values("bulk_create")
        self._validate_instances(tuple(objects), saved=False)
        for obj in objects:
            object_values = [getattr(obj, field_name) for field_name in self.relation_fields]
            if object_values != [None] * len(object_values) and object_values != parent_values:
                raise QueryError(
                    f"{obj!r} points its foreign key at {', '.join(repr(value) for value in object_values)}, not "
                    f"at the parent instance of this relation ({', '.join(repr(value) for value in parent_values)})"
                    f" - drop it, or call {self.model.__name__}.objects.bulk_create() instead"
                )
            for field_name, parent_value in zip(self.relation_fields, parent_values, strict=True):
                setattr(obj, field_name, parent_value)
        self._invalidate_local_cache()
        return self._get_model_queryset(self._get_write_connection()).bulk_create(
            objects,
            batch_size,
            ignore_conflicts=ignore_conflicts,
            update_fields=update_fields,
            on_conflict=on_conflict,
            on_conflict_constraint=on_conflict_constraint,
            conflict_where=conflict_where,
            returning=returning,
            use_copy=use_copy,
        )

    async def get_or_create(
        self, defaults: dict[str, Any] | None = None, *, using: str | DatabaseClient | None = None, **kwargs: Any
    ) -> tuple[TModel, bool]:
        """
        Fetches the related element matching ``kwargs``, else creates one pointing at the parent
        instance - see ``QuerySet.get_or_create()``.

        Raises:
            QueryError: The parent instance is not saved, or its referenced value is NULL; or
                ``kwargs`` points the foreign key at a different row than the parent.
        """
        result = await self._get_model_queryset(self._get_write_connection(using)).get_or_create(
            defaults, **self._get_kwargs_with_parent_key(kwargs, "get_or_create")
        )
        if result[1]:
            self._invalidate_local_cache()
        return result

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        *,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]:
        """
        Updates the related element matching ``kwargs`` with ``defaults``, else creates one
        pointing at the parent instance - see ``QuerySet.update_or_create()``.

        Raises:
            QueryError: The parent instance is not saved, or its referenced value is NULL; or
                ``kwargs`` points the foreign key at a different row than the parent.
        """
        result = await self._get_model_queryset(self._get_write_connection(using)).update_or_create(
            defaults, create_defaults, **self._get_kwargs_with_parent_key(kwargs, "update_or_create")
        )
        self._invalidate_local_cache()
        return result

    async def add(self, *objs: TModel, bulk: bool = True, using: str | DatabaseClient | None = None) -> None:
        """
        Points the foreign key of each of ``objs`` at the parent instance.

        Args:
            objs: The related objs.
            bulk: Set it with one ``UPDATE`` - every one of ``objs`` must be saved. Without it
                each instance is saved on its own (``save()`` hooks run, an unsaved one is created).
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: The parent instance is not saved, or its referenced value is NULL;
                or ``bulk`` is set and one of ``objs`` is not saved.
            ValidationError: One of ``objs`` is not an instance of the related model.
            IntegrityError: ``bulk`` is set and one of ``objs`` isn't shown by the related
                model's default scope (soft-deleted, another tenant's, or filtered out by its
                ``Meta.manager``).
        """
        if not objs:
            return
        parent_values = self._get_parent_key_values("add")
        self._validate_instances(objs, saved=bulk)
        await self._set_relation_key_values(objs, parent_values, bulk, using)

    async def remove(self, *objs: TModel, bulk: bool = True, using: str | DatabaseClient | None = None) -> None:
        """
        Sets the foreign key of each of ``objs`` to NULL - only for a nullable foreign key.

        Args:
            objs: The related objs.
            bulk: Clear it with one ``UPDATE``; without it each instance is saved on its own.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: The foreign key isn't nullable - delete the rows instead.
            DoesNotExist: One of ``objs`` isn't related to the parent instance.
            QueryError: The parent instance or one of ``objs`` is not saved.
            ValidationError: One of ``objs`` is not an instance of the related model.
        """
        if not objs:
            return
        self._validate_nullable("remove")
        parent_values = self._get_parent_key_values("remove")
        self._validate_instances(objs, saved=True)
        for instance in objs:
            if [getattr(instance, field_name) for field_name in self.relation_fields] != parent_values:
                raise DoesNotExist(f"{instance!r} is not related to {self.instance!r}")
        await self._set_relation_key_values(
            objs, [None] * len(self.relation_fields), bulk, using, current_values=parent_values
        )

    async def clear(self, bulk: bool = True, using: str | DatabaseClient | None = None) -> None:
        """
        Sets the foreign key of every related element to NULL - only for a nullable foreign key.

        Args:
            bulk: Clear it with one ``UPDATE``; without it each element is fetched and saved on
                its own.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: The foreign key isn't nullable - delete the rows instead.
            QueryError: The parent instance is not saved.
        """
        self._validate_nullable("clear")
        empty_values = dict.fromkeys(self.relation_fields)
        if bulk:
            await self._clone().using(using).update(**empty_values)
        else:
            connection = self._get_write_connection(using)
            async with connection._in_transaction() as transaction_connection:
                for instance in await self._clone().using(transaction_connection):
                    for field_name in self.relation_fields:
                        setattr(instance, field_name, None)
                    await instance.save(using=transaction_connection, update_fields=list(self.relation_fields))
        self._invalidate_local_cache()

    async def set(
        self, *objs: Any, bulk: bool = True, clear: bool = False, using: str | DatabaseClient | None = None
    ) -> None:
        """
        Makes ``objs`` the related elements, in one transaction - given as arguments, or as one
        iterable or queryset. A nullable foreign key of an element missing from ``objs`` is
        set to NULL; a non-nullable one is left as is, like Django.

        Args:
            objs: The related instances.
            bulk: Passed on to ``add()``/``remove()``.
            clear: Clear the whole relation first and add every one of ``objs`` afresh.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: The parent instance is not saved, or ``bulk`` is set and one of
                ``objs`` is not saved.
            ValidationError: One of ``objs`` is not an instance of the related model.
        """
        members = cast("tuple[TModel, ...]", await self._get_set_members(objs))
        self._validate_instances(members, saved=bulk)
        connection = self._get_write_connection(using)
        async with connection._in_transaction() as transaction_connection:
            if not self._is_nullable():
                await self.add(*members, bulk=bulk, using=transaction_connection)
                return
            if clear:
                await self.clear(bulk=bulk, using=transaction_connection)
                await self.add(*members, bulk=bulk, using=transaction_connection)
                return
            new_pk_values = {instance.pk for instance in members if instance._saved_in_db}
            stale_instances = [
                instance
                for instance in await self._clone().using(transaction_connection)
                if instance.pk not in new_pk_values
            ]
            await self.remove(*stale_instances, bulk=bulk, using=transaction_connection)
            await self.add(*members, bulk=bulk, using=transaction_connection)

    def get_not_related_message(self) -> str:
        """The error of a row ``remove()`` was given that the database holds under another parent."""
        return (
            f"A {self.model.__name__} row given to remove() isn't related to {self.instance!r} in the database - "
            "moved to another one meanwhile, or not shown by its default scope; nothing was removed"
        )

    def _invalidate_local_cache(self) -> None:
        """Drops the fetched rows after a write through the relation - and again if the
        transaction it ran in rolls back: rows read meanwhile may hold the write."""
        super()._invalidate_local_cache()
        self._reset_cache_on_rollback(self._get_write_connection(None))

    def _get_parent_key_values(self, method_name: str) -> list[Any]:
        """The parent instance's value(s) the related elements' foreign key references.

        Args:
            method_name: The calling method, for the error message.

        Raises:
            QueryError: The parent instance is not saved, or its referenced value is NULL.
        """
        if not self.instance._saved_in_db:
            raise QueryError("This objects hasn't been instanced, call .save() before calling related queries")
        parent_values = RelationValues.get_relation_key_values(
            self.instance, self.from_fields, f"The relation to {self.model.__name__}"
        )
        if None in parent_values:
            # A NULL target value is referenced by no row - the object would be detached from the
            # parent it was written through.
            raise QueryError(
                f"Can't {method_name}() a related {self.model.__name__} through this relation - the "
                f"parent {type(self.instance).__name__}'s {', '.join(self.from_fields)} is NULL, so no row "
                "can reference it"
            )
        return list(parent_values)

    def _get_kwargs_with_parent_key(self, kwargs: dict[str, Any], method_name: str) -> dict[str, Any]:
        """``kwargs`` with the foreign key pointed at the parent instance.

        Args:
            kwargs: The model parameters (or query parameters) given.
            method_name: The calling method, for the error messages.

        Raises:
            QueryError: The parent instance is not saved, or its referenced value is NULL.
            QueryError: ``kwargs`` points the foreign key at a different row than the parent.
        """
        parent_values = self._get_parent_key_values(method_name)
        # A caller can conflict with the parent instance either through the shadow column (e.g.
        # "tournament_id") or through the relation's own field name (e.g.
        # "tournament=some_other_tournament").
        for relation_field, parent_value in zip(self.relation_fields, parent_values, strict=True):
            if relation_field in kwargs and kwargs[relation_field] != parent_value:
                raise QueryError(
                    f"'{relation_field}'={kwargs[relation_field]!r} conflicts with the parent instance "
                    f"of this relation ({parent_value!r}) - drop it, or call "
                    f"{self.model.__name__}.objects.{method_name}() instead"
                )
        relation_name = self._get_relation_name()
        if relation_name and relation_name in kwargs:
            given_relation_value = kwargs[relation_name]
            # Compared through the same to_field= attribute(s) the parent is referenced by (not
            # .pk), component by component for a composite target.
            given_values = (
                [None] * len(self.from_fields)
                if given_relation_value is None
                else [getattr(given_relation_value, field_name, None) for field_name in self.from_fields]
            )
            if given_values != parent_values:
                raise QueryError(
                    f"'{relation_name}'={given_relation_value!r} conflicts with the parent instance "
                    f"of this relation ({', '.join(repr(value) for value in parent_values)}) - drop it, "
                    f"or call {self.model.__name__}.{method_name}() instead"
                )
        return {**kwargs, **dict(zip(self.relation_fields, parent_values, strict=True))}

    def _get_relation_name(self) -> str | None:
        """The name of the related model's foreign key field, or None when it has no field of its own."""
        relation_key = self.model._meta.foreign_key_shadow_columns.get(self.relation_fields[0])
        return relation_key[1:] if relation_key else None

    def _is_nullable(self) -> bool:
        """Whether the related model's foreign key accepts NULL."""
        fields_map = self.model._meta.fields_map
        relation_name = self._get_relation_name()
        if relation_name and relation_name in fields_map:
            return fields_map[relation_name].null
        return all(fields_map[field_name].null for field_name in self.relation_fields)

    def _validate_nullable(self, method_name: str) -> None:
        """
        Raises:
            QueryError: The related model's foreign key isn't nullable.
        """
        if not self._is_nullable():
            raise QueryError(
                f"Can't {method_name}() through this relation - {self.model.__name__}'s foreign key to "
                f"{type(self.instance).__name__} isn't nullable, delete the rows instead"
            )

    def _validate_instances(self, instances: tuple[Any, ...], *, saved: bool) -> None:
        """Checks every one of ``instances`` is an instance of the related model.

        Args:
            instances: The instances to check.
            saved: Also check every one of ``instances`` is saved.

        Raises:
            ValidationError: An instance is not of the related model's type.
            QueryError: ``saved`` is set and an instance is not saved yet.
        """
        for instance in instances:
            if type(instance) is not self.model:
                raise ValidationError(
                    f"Invalid instance for relationship to '{type(self.instance).__name__}'. "
                    f"Expected model type '{self.model.__name__}', but got '{type(instance).__name__}'."
                )
            if saved and not instance._saved_in_db:
                raise QueryError(f"You should first call .save() on {instance!r}, or pass bulk=False to save it here")

    async def _set_relation_key_values(
        self,
        instances: tuple[TModel, ...],
        values: list[Any],
        bulk: bool,
        using: str | DatabaseClient | None,
        *,
        current_values: list[Any] | None = None,
    ) -> None:
        """Writes ``values`` into the foreign key of each of ``instances``, in the database and in
        memory.

        Args:
            instances: The related instances.
            values: The foreign key value(s), in ``relation_fields`` order.
            bulk: One ``UPDATE`` for every one of ``instances``, else a ``save()`` of each.
            using: Specific DB connection to use instead of default bound.
            current_values: The foreign key value(s) each row must hold in the database to be
                written - ``remove()`` unlinks a row from this parent only, not from another one
                it was moved to meanwhile.

        Raises:
            IntegrityError: ``bulk`` is set and one of ``instances`` isn't shown by the related
                model's default scope.
            DoesNotExist: One of ``instances`` doesn't hold ``current_values`` in the database.
        """
        relation_values = dict(zip(self.relation_fields, values, strict=True))
        current_filter = {} if current_values is None else dict(zip(self.relation_fields, current_values, strict=True))
        connection = self._get_write_connection(using)
        async with connection._in_transaction() as transaction_connection:
            pk_values = list(dict.fromkeys(instance.pk for instance in instances))
            if current_filter and not bulk:
                related_count = (
                    await self._get_model_queryset(transaction_connection)
                    .filter(pk__in=pk_values, **current_filter)
                    .count()
                )
                if related_count != len(pk_values):
                    raise DoesNotExist(self.get_not_related_message())
            if bulk:
                # Reported below with the rows it names - the UPDATE itself names none.
                with ChangeEvents.reporting_as_a_whole():
                    updated_count = await (
                        self._get_model_queryset(transaction_connection)
                        .filter(pk__in=pk_values, **current_filter)
                        .update(**relation_values)
                    )
                if updated_count != len(pk_values) and current_filter:
                    raise DoesNotExist(self.get_not_related_message())
                if updated_count != len(pk_values):
                    raise IntegrityError(
                        f"Can't write the relation to {type(self.instance).__name__} - one of the "
                        f"{self.model.__name__} rows isn't shown by its default scope"
                    )
                await WriteSteps.report(
                    transaction_connection, self.model, RowOperation.UPDATE, pks=pk_values, fields=self.relation_fields
                )
            for instance in instances:
                for field_name, value in relation_values.items():
                    setattr(instance, field_name, value)
                if not bulk:
                    update_fields = list(self.relation_fields) if instance._saved_in_db else None
                    await instance.save(using=transaction_connection, update_fields=update_fields)
        self._invalidate_local_cache()
