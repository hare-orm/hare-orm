from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from hare.core.hare_context import HareContext
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import ConfigurationError, IntegrityError
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.models.tenancy.tenancy import Tenancy
from hare.models.uniqueness.declared_uniqueness import DeclaredUniqueness
from hare.models.write.rules.tenant_write_rules import TenantWriteRules

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.select.model_rows_query import ModelRowsQuery


EventType = TypeVar("EventType", bound="OutboxEvent")


class OutboxIdempotency:
    """Inserts keyed events atomically and reads the matching event after a key conflict."""

    @staticmethod
    async def insert_or_get_by_idempotency_key(
        binding: ModelBinding,
        topic: str,
        payload: Any,
        idempotency_key: str,
        ordering_key: str | None,
        headers: dict[str, Any],
        write_connection: DatabaseClient,
        field_values: Mapping[str, Any],
    ) -> tuple[EventType, bool]:
        """Inserts an event with ``idempotency_key`` unless one already exists, then reads it back.

        Args:
            binding: The binding of the outbox event model.
            topic: The topic of a new event.
            payload: The payload of a new event.
            idempotency_key: The unique key.
            ordering_key: The ordering key of a new event.
            headers: The headers of a new event.
            write_connection: The connection - possibly a transaction - both statements run on.
            field_values: Subclass column values of a new event.

        Returns:
            The event stored under the key, and whether this call inserted it.

        Raises:
            IntegrityError: The key is taken by an event not visible to this call.
            ConfigurationError: ``Meta.tenant_field`` is set, ``field_values`` names no tenant and
                no tenant scope is active; or the named tenant conflicts with the active scope.
        """
        model = binding._model
        if binding.tenant_field and Tenancy.get_scope(binding) is None:
            tenant_given, given_tenant, _ = Tenancy.get_given_tenant(binding, field_values)
            if tenant_given and given_tenant is not None:
                # An explicitly named tenant is trusted with no active scope, as create() does.
                with Tenancy.scope_if_unset(binding, given_tenant):
                    return await OutboxIdempotency.insert_or_get_by_idempotency_key(
                        binding, topic, payload, idempotency_key, ordering_key, headers, write_connection, field_values
                    )
        candidate: EventType = InstanceInitialization.new(
            binding,
            HareContext.require_current(),
            topic=topic,
            payload=payload,
            idempotency_key=idempotency_key,
            ordering_key=ordering_key,
            headers=headers,
            **field_values,
        )  # type: ignore[assignment]
        OutboxIdempotency.prepare_candidates(binding, [candidate], "enqueue")
        # RETURNING tells exactly whether the row was written - where bulk_create() can return rows
        # at all, which needs the database to return them in the order they were written.
        read_back_inserted_row = write_connection.features.guarantees_returning_order
        conflict_field_names = OutboxIdempotency.get_idempotency_conflict_field_names(
            binding,
        )
        await (
            binding.manager.get_bound_queryset(binding, HareContext.require_current())
            .using(write_connection)
            .bulk_create(
                [candidate],
                ignore_conflicts=True,
                on_conflict=conflict_field_names,
                returning=True if read_back_inserted_row else None,
            )
        )
        # The row stored under the key is the one the insert conflicted with - the same values of
        # every field the key is unique with (a tenant, under a scope showing several).
        stored_row_filter = {field_name: getattr(candidate, field_name) for field_name in conflict_field_names}
        queryset = (
            binding.manager.get_bound_queryset(binding, HareContext.require_current())
            .filter(**stored_row_filter)
            .using(write_connection)
        )
        if binding.soft_delete_field:
            queryset = queryset.include_deleted()
        stored = await queryset.first()
        if stored is None:
            raise IntegrityError(
                f"{model.__name__}.enqueue(): idempotency_key {idempotency_key!r} is already used by an event "
                "not visible here (another tenant's, or committed after this transaction's snapshot)"
            )
        if read_back_inserted_row:
            is_new = candidate._saved_in_db
        else:
            # An explicit id alone can't tell this insert apart from an earlier event stored under the
            # same key and id - the created_at this insert wrote can.
            is_new = stored.id == candidate.id and stored.created_at == candidate.created_at
        return stored, is_new

    @staticmethod
    def prepare_candidates(binding: ModelBinding, candidates: Sequence[EventType], operation: str) -> None:
        """Retain each private candidate's effective tenant through skipped-insert restoration.

        The ordinary bulk preparation snapshot must already contain the tenant used
        for idempotency lookup. Caller-owned bulk objects keep their usual restoration
        contract. Tenant assignment and checks use the normal write rules unchanged.
        """
        if binding.tenant_field:
            TenantWriteRules.scope_objects(binding, candidates, operation, fills_missing=True, requires_active=False)

    @staticmethod
    async def read_events_for_inputs(
        binding: ModelBinding,
        candidates: Sequence[EventType],
        inserted: Sequence[EventType],
        field_names: Sequence[str],
        connection: DatabaseClient,
    ) -> list[EventType]:
        """Associate skipped inputs with stored events using database equality, in input order.

        Values are converted by normal lookup writers and joined as a typed VALUES
        source. Input ordinals preserve duplicates without assuming Python equality
        matches the column's equality. Manager scopes, execution dependencies, decoding
        and prefetch remain under the existing query execution and hydration contracts.
        """
        inserted_objects = {id(event) for event in inserted}
        events = [event if id(event) in inserted_objects else None for event in candidates]
        missing = [(index, event) for index, event in enumerate(candidates) if events[index] is None]
        if not missing:
            return events  # type: ignore[return-value]
        queryset = binding.manager.get_bound_queryset(binding, HareContext.require_current()).using(connection)
        if binding.soft_delete_field:
            queryset = queryset.include_deleted()
        from hare.query.execution.query_execution import QueryExecution

        return await QueryExecution.run_operation(
            (queryset,),
            partial(
                OutboxIdempotency._read_events_for_inputs_bound,
                binding,
                candidates,
                inserted,
                field_names,
                connection,
                queryset,
                events,
                missing,
            ),
        )

    @staticmethod
    async def _read_events_for_inputs_bound(
        binding: ModelBinding,
        candidates: Sequence[EventType],
        inserted: Sequence[EventType],
        field_names: Sequence[str],
        connection: DatabaseClient,
        queryset: QuerySet[EventType, EventType],
        events: list[EventType | None],
        missing: Sequence[tuple[int, EventType]],
    ) -> list[EventType]:
        """Read ordinal-tagged events inside the prepared query execution context.

        Each batch reserves the base scope's bind parameters, including sensitivity
        metadata. The temporary ordinal is an allocated annotation of a fresh reader;
        it is removed before any event leaves this method. Repeated inputs share the
        same model object, keyed by the outbox event's unique UUID.
        """
        model = binding._model
        from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
        from hare.query.statements.select.model_rows.instance_hydration import InstanceHydration
        from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
        from hare.sql.identifiers import Identifiers
        from hare.sql.terms.parameters.parameterizer import Parameterizer
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        source: ModelRowsQuery[EventType] = queryset._get_compiler()._get_execution_query(False)  # type: ignore[assignment]
        source._make_query(value_wrapper_references=[])
        columns = source._get_output_columns()
        aliases = {path: alias for _name, path, alias, field in columns if path in field_names and field is not None}
        model_binding: ModelBinding = queryset._binding  # type: ignore[assignment]
        event_binding = model_binding.graph.get_binding(model)
        names = Identifiers(
            [
                *event_binding.fields_map,
                *event_binding.fields_db_projection.values(),
                *(alias for _name, _path, alias, _field in columns if alias),
            ]
        )
        allocated = []
        for suggestion in (
            "hare outbox stored",
            "hare outbox inputs",
            "hare outbox ordinal",
            *(f"hare outbox key {index}" for index in range(len(field_names))),
        ):
            name = names.get_free_name(suggestion)
            names.add(name)
            allocated.append(name)
        stored_alias, input_alias, ordinal, *key_columns = allocated
        fields = [event_binding.fields_map[name] for name in field_names]
        dialect = connection.dialect
        writers = [dialect.types.get_lookup_writer(field) for field in fields]
        stored_by_id = {event.id: event for event in inserted}
        reader = InstanceHydration.get_model_rows_reader(
            source, connection=connection, reader_class=source.rows_reader_class
        )
        reader = reader.with_extra_annotations(ordinal)
        prefetch = PrefetchRequest.from_query(source, source._prefetch_map, source._prefetch_queries)
        # A base scope is rendered before input rows: its parameters are counted and bound first.
        base_parameters = Parameterizer()
        base_sql = source.query.get_sql(connection.sql_context.copy(parameterizer=base_parameters))
        size = BulkWriteBatches.get_bind_parameter_safe_batch_size(
            None, len(fields), connection.features.max_bind_parameters - len(base_parameters.values)
        )
        for batch in BulkWriteBatches.get_batches(missing, size):
            parameters = Parameterizer()
            parameters.values = base_parameters.values.copy()
            context = connection.sql_context.copy(parameterizer=parameters)
            quote = context.quote
            row_sqls = []
            for index, candidate in batch:
                cells = [str(index)]
                for name, field, write in zip(field_names, fields, writers, strict=True):
                    value = getattr(candidate, name)
                    wire = None if value is None else write(value, model)
                    wrapper = ValueWrapper(wire)
                    wrapper.sensitive = field.sensitive
                    cells.append(
                        dialect.clauses.get_typed_placeholder_template(
                            wrapper.get_sql(context), dialect.parameters.get_field_parameter_cast_type(field)
                        )
                    )
                row_sqls.append("(" + ", ".join(cells) + ")")
            inputs_sql = dialect.clauses.get_values_source_sql(
                row_sqls,
                [quote(ordinal), *(quote(name) for name in key_columns)],
                quote(input_alias),
                matches_no_row=False,
            )
            sql = dialect.clauses.get_values_join_sql(
                base_sql,
                inputs_sql,
                stored_alias,
                input_alias,
                ordinal,
                [(aliases[name], column) for name, column in zip(field_names, key_columns, strict=True)],  # type: ignore[misc]
            )
            rows = await reader.fetch(connection, sql, parameters.values, prefetch, specification=source)
            for stored in rows:
                index = getattr(stored, ordinal)
                delattr(stored, ordinal)
                events[index] = stored_by_id.setdefault(stored.id, stored)
        for index, event in enumerate(events):
            if event is None:
                raise IntegrityError(
                    f"{model.__name__}.enqueue_many(): idempotency_key {candidates[index].idempotency_key!r} "
                    "is already used by an event not visible here (another tenant's, or committed after "
                    "this transaction's snapshot)"
                )
        return events  # type: ignore[return-value]

    @staticmethod
    def get_idempotency_conflict_field_names(binding: ModelBinding) -> tuple[str, ...]:
        """The unique field set ``idempotency_key`` conflicts on.

        Returns:
            ``("idempotency_key",)`` for a unique column, otherwise the fields of the first
            unconditional ``UniqueConstraint`` that includes it.

        Raises:
            ConfigurationError: No unique constraint covers ``idempotency_key``.
        """
        model = binding._model
        if binding.fields_map["idempotency_key"].unique:
            return ("idempotency_key",)
        for constraint in binding.constraints:
            if (
                isinstance(constraint, UniqueConstraint)
                and constraint.condition is None
                and "idempotency_key" in constraint.fields
            ):
                return DeclaredUniqueness.get_attribute_names(binding, constraint.fields)
        raise ConfigurationError(
            f"{model.__name__}.idempotency_key isn't unique - enqueue(idempotency_key=...) needs a unique "
            "constraint covering it to deduplicate on"
        )
