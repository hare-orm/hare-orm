from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import DoesNotExist, FieldError, IntegrityError, QueryError
from hare.query.enums import GetException
from hare.query.queryset.single_rows.query_set_single import QuerySetSingle

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


SameQuerySet = TypeVar("SameQuerySet", bound="QuerySet[Any, Any]")


class CreateOrUpdate:
    """get_or_create() and update_or_create(): the row matching the lookups read inside a transaction
    on the connection writes go to, created from the lookups and the defaults when none matches - a
    concurrent create of the same row read back instead of failing."""

    @staticmethod
    async def create_or_get(
        queryset: QuerySet[Any, Any], defaults: dict[str, Any], kwargs: dict[str, Any]
    ) -> tuple[TModel, bool]:
        """Creates the row ``kwargs`` describes on the pinned connection, or - when a concurrent writer
        created it first - reads that row. Inside a transaction, or for a model overriding
        ``save()``, the create runs in a savepoint or transaction, so a failed INSERT leaves the
        connection usable.

        Args:
            queryset: The queryset.
            defaults: The values for a created object.
            kwargs: The conditions.

        Returns:
            The object and whether it was created.

        Raises:
            QueryError: ``defaults`` conflicts with an exact ``kwargs`` value.
            IntegrityError: The create failed for a reason other than the row already existing.
        """
        from hare.models import Model

        create_values = CreateOrUpdate.get_create_values(defaults, kwargs)
        pinned_connection = queryset._connection
        try:
            if isinstance(pinned_connection, TransactionClient) or queryset.model.save is not Model.save:
                async with pinned_connection._in_transaction() as connection:
                    return await queryset.using(connection).create(**create_values), True
            return await queryset.create(**create_values), True
        except IntegrityError as error:
            try:
                return await CreateOrUpdate.get_matching(queryset, kwargs), False
            except DoesNotExist:
                raise error from None

    @staticmethod
    async def update_with_defaults(
        queryset: QuerySet[Any, Any], obj: TModel, defaults: dict[str, Any], connection: DatabaseClient
    ) -> None:
        """Writes ``update_or_create()``'s ``defaults`` onto the existing row.

        Args:
            queryset: The queryset.
            obj: The existing row.
            defaults: The values to write.
            connection: The connection to save on.

        Raises:
            QueryError: ``defaults`` sets ``Meta.optimistic_lock_field``.
            FieldError: ``defaults`` names a field the model doesn't have.
        """
        meta = queryset.model._meta
        if (optimistic_lock_field := meta.optimistic_lock_field) and optimistic_lock_field in defaults:
            raise QueryError(
                f"Cannot set '{optimistic_lock_field}' via update_or_create() - it's bumped automatically"
            )
        known_field_names = (
            {"pk"}
            | meta.foreign_key_fields
            | meta.one_to_one_fields
            | set(meta.fields_db_projection)
            | meta.backward_foreign_key_fields
            | meta.backward_one_to_one_fields
            | meta.many_to_many_fields
        )
        for field_name in defaults:
            if field_name not in known_field_names:
                raise FieldError(f"Unknown field '{field_name}' for model {meta.full_name}")
        await obj.update_from_dict(defaults).save(using=connection)

    @staticmethod
    def get_matching(queryset: QuerySet[Any, Any], kwargs: dict[str, Any]) -> QuerySetSingle[TModel]:
        """The one row of the queryset matching ``kwargs`` - not ``get(**kwargs)``, whose own
        exception parameters would take a field of that name.

        Args:
            queryset: The queryset.
            kwargs: The conditions.

        Returns:
            The awaitable single-row query.
        """
        return cast(
            "QuerySetSingle[TModel]",
            queryset._get_single_queryset(
                (),
                kwargs,
                does_not_exist_exception=GetException.STANDARD,
                multiple_objects_returned_exception=GetException.STANDARD,
            ),
        )

    @staticmethod
    def get_create_values(defaults: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        """The field values of the object ``get_or_create()``/``update_or_create()`` creates:
        ``kwargs`` without its lookups (a lookup only filters, like Django), then ``defaults``.

        Args:
            defaults: The values for a created object.
            kwargs: The conditions.

        Returns:
            The field values.

        Raises:
            QueryError: ``defaults`` conflicts with an exact ``kwargs`` value.
        """
        exact_match_kwargs = {key: value for key, value in kwargs.items() if "__" not in key}
        for key in defaults.keys() & exact_match_kwargs.keys():
            if (default_value := defaults[key]) != (query_value := exact_match_kwargs[key]):
                raise QueryError(f"Conflict value with {key=}: {default_value=} vs {query_value=}")
        return {**exact_match_kwargs, **defaults}

    @staticmethod
    def pinned_for_write(queryset: SameQuerySet) -> SameQuerySet:
        """The queryset pinned to the connection its writes go to.

        Args:
            queryset: The queryset.

        Returns:
            The queryset itself when a connection is pinned already.
        """
        if cast("DatabaseClient | None", queryset._connection) is not None:
            return queryset
        clone = queryset._clone()
        clone._apply_connection(queryset.get_connection(for_write=True))
        return clone
