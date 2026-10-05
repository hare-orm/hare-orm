from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import UnSupportedError
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.write.write_steps import WriteSteps
from hare.query.statements.constants import UPSERT_INSERTED_FLAG_ALIAS
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.sql.terms.values.literal_value import LiteralValue
from hare.time import Timezone

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class BulkCreateReports:
    """The report of the rows a bulk insert changed (ChangeEvents): the inserted objects, or the rows
    the statements return when conflicting rows are skipped or updated, and of a failed insert the
    batches already committed."""

    @staticmethod
    def reports_returned_rows(bulk_create: BulkCreateQuery[Any]) -> bool:
        """Whether the statements return the rows they wrote for the report of the changes -
        when a listener hears the model and conflicting rows are skipped or updated, so not every
        object is a row inserted.

        Args:
            bulk_create: The bulk insert.
        """
        return bulk_create._reported_rows is not None

    @staticmethod
    def get_reported_returning_terms(bulk_create: BulkCreateQuery[Any]) -> list[Any]:
        """The ``RETURNING`` terms the report of the changes reads: the primary key, and for an
        upsert the dialect's inserted-row flag - or the conflict key, matched against the keys
        read before the write.

        Args:
            bulk_create: The bulk insert.

        Returns:
            The terms.
        """
        if not BulkCreateReports.reports_returned_rows(bulk_create):
            return []
        meta = bulk_create.model._meta
        terms: list[Any] = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
        if bulk_create._update_fields:
            flag_sql = bulk_create._connection.dialect.clauses.get_upsert_inserted_flag_sql()
            if flag_sql is not None:
                terms.append(LiteralValue(flag_sql, alias=UPSERT_INSERTED_FLAG_ALIAS))
        return terms

    @staticmethod
    async def read_existing_primary_keys(bulk_create: BulkCreateQuery[Any]) -> None:
        """Reads which objects' rows exist before an upsert writes - on a database without an
        inserted-row flag, so the report can tell an updated row from an inserted one.

        Args:
            bulk_create: The bulk insert.
        """
        # Deferred import: the queryset package builds bulk_create() queries.
        from hare.query.queryset.queryset import QuerySet

        meta = bulk_create.model._meta
        conflict_names = ConflictClause.get_conflict_update_key_field_names(bulk_create)
        keys = {tuple(getattr(obj, name) for name in conflict_names) for obj in bulk_create._objects}
        existing = await (
            QuerySet(bulk_create.model)
            .using(bulk_create._connection)
            .filter(**{f"{conflict_names[0]}__in": [key[0] for key in keys]})
            .values_list(*meta.primary_key_attribute_names, *conflict_names)
        )
        pk_count = len(meta.primary_key_attribute_names)
        bulk_create._existing_primary_keys = {
            tuple(row[:pk_count]) for row in existing if tuple(row[pk_count:]) in keys
        }

    @staticmethod
    async def report_bulk_create(bulk_create: BulkCreateQuery[Any], create: Awaitable[None]) -> None:
        """Runs the insert and reports its rows (``ChangeEvents``) - of a failed insert, only the
        batches a ``COPY`` outside the transaction committed. Where conflicting rows are skipped or
        updated, the returned rows tell inserted from updated.

        Args:
            bulk_create: The bulk insert.
            create: The insert.
        """
        listened = ChangeEvents.is_observed(bulk_create.model)
        capture_needs = bulk_create.model._meta.change_capture_needs
        if (
            (listened and (bulk_create._ignore_conflicts or bulk_create._update_fields) or capture_needs is not None)
            and bulk_create._connection.features.supports_returning
            and not bulk_create._use_copy
            and bulk_create.model._meta.has_primary_key
        ):
            bulk_create._reported_rows = []
            if bulk_create._update_fields and (
                bulk_create._connection.dialect.clauses.get_upsert_inserted_flag_sql() is None
                or (capture_needs is not None and capture_needs.reads_before)
            ):
                await BulkCreateReports.read_existing_primary_keys(bulk_create)
        before_by_pk = await BulkCreateReports.read_values_before(bulk_create) if capture_needs is not None else None
        await create
        if capture_needs is not None:
            await BulkCreateReports.capture(bulk_create, capture_needs, before_by_pk)
        if bulk_create._reported_rows is None:
            await BulkCreateReports.report_inserted_objects(bulk_create, list(bulk_create._objects))
            if bulk_create._update_fields and listened and bulk_create._objects:
                await WriteSteps.report(
                    bulk_create._connection, bulk_create.model, RowOperation.UPDATE, fields=bulk_create._update_fields
                )
            return
        await BulkCreateReports.report_returned_rows(bulk_create)

    @staticmethod
    async def read_values_before(bulk_create: BulkCreateQuery[Any]) -> dict[Any, Any] | None:
        """The captured fields of the rows an upsert of a model with ``Meta.change_capture`` may
        update, as they are before it - locked.

        Args:
            bulk_create: The bulk insert.

        Returns:
            The values by primary key, None when the payload holds no rows before a write.
        """
        capture_needs = bulk_create.model._meta.change_capture_needs
        existing_primary_keys = bulk_create._existing_primary_keys
        if capture_needs is None or not capture_needs.reads_before or not existing_primary_keys:
            return None
        pks = [key[0] if len(key) == 1 else key for key in existing_primary_keys]
        values_by_pk = await ChangeCapturing.read_values(
            bulk_create.model, bulk_create._connection, pks, capture_needs, lock=True
        )
        return {pk: values for pk, (values, _tenant) in values_by_pk.items()}

    @staticmethod
    def get_written_keys(bulk_create: BulkCreateQuery[Any]) -> tuple[list[Any], list[Any]]:
        """The primary keys of the rows the insert wrote.

        Args:
            bulk_create: The bulk insert, run.

        Returns:
            The keys of the rows inserted, and of those an upsert updated.

        Raises:
            UnSupportedError: The rows can't be named - a ``COPY`` of objects whose keys the database
                generates.
        """
        if bulk_create._reported_rows is None:
            objects = list(bulk_create._objects)
            if any(obj.pk is None for obj in objects):
                raise UnSupportedError(
                    f"bulk_create() of {bulk_create.model.__name__} can't capture its rows (Meta.change_capture) - "
                    "a bulk load returns no generated keys; give the objects their keys"
                )
            return [obj.pk for obj in objects], []
        meta = bulk_create.model._meta
        pk_columns = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
        pk_fields = [meta.fields_map[name] for name in meta.primary_key_attribute_names]
        types = bulk_create._connection.dialect.types
        inserted_pks: list[Any] = []
        updated_pks: list[Any] = []
        existing_primary_keys = bulk_create._existing_primary_keys
        for row in bulk_create._reported_rows or ():
            key = tuple(
                types.get_python_value(field, row[column]) for field, column in zip(pk_fields, pk_columns, strict=True)
            )
            pk = key if len(key) > 1 else key[0]
            if UPSERT_INSERTED_FLAG_ALIAS in row:
                inserted = bool(row[UPSERT_INSERTED_FLAG_ALIAS])
            else:
                inserted = existing_primary_keys is None or key not in existing_primary_keys
            (inserted_pks if inserted else updated_pks).append(pk)
        return inserted_pks, updated_pks

    @staticmethod
    async def capture(
        bulk_create: BulkCreateQuery[Any], capture_needs: CaptureNeeds, before_by_pk: dict[Any, Any] | None
    ) -> None:
        """Captures the rows the insert wrote - their values read after it, in its transaction.

        Args:
            bulk_create: The bulk insert, run.
            capture_needs: The model's needs.
            before_by_pk: The upserted rows' values before the write, by primary key.
        """
        model = bulk_create.model
        connection = bulk_create._connection
        inserted_pks, updated_pks = BulkCreateReports.get_written_keys(bulk_create)
        pks = [*inserted_pks, *updated_pks]
        if not pks:
            return
        values_by_pk: dict[Any, Any] = {}
        if capture_needs.reads_values or capture_needs.tenant_field_name:
            values_by_pk = await ChangeCapturing.read_values(model, connection, pks, capture_needs)
        occurred_at = Timezone.now()
        changes = [
            ChangeCapturing.build_change(
                model,
                capture_needs,
                operation,
                pk,
                changed=bulk_create._update_fields if operation is RowOperation.UPDATE else None,
                before=(before_by_pk or {}).get(pk),
                after=values_by_pk.get(pk, (None, None))[0],
                tenant=values_by_pk.get(pk, (None, None))[1],
                occurred_at=occurred_at,
            )
            for operation, operation_pks in ((RowOperation.INSERT, inserted_pks), (RowOperation.UPDATE, updated_pks))
            for pk in operation_pks
        ]
        await ChangeCapturing.capture(connection, model, changes)

    @staticmethod
    async def report_returned_rows(bulk_create: BulkCreateQuery[Any]) -> None:
        """Reports the rows the statements returned - inserted, or updated by an upsert.

        Args:
            bulk_create: The bulk insert.
        """
        inserted_pks, updated_pks = BulkCreateReports.get_written_keys(bulk_create)
        if inserted_pks:
            await WriteSteps.report(bulk_create._connection, bulk_create.model, RowOperation.INSERT, pks=inserted_pks)
        if updated_pks:
            await WriteSteps.report(
                bulk_create._connection,
                bulk_create.model,
                RowOperation.UPDATE,
                pks=updated_pks,
                fields=bulk_create._update_fields,
            )

    @staticmethod
    async def report_inserted_objects(bulk_create: BulkCreateQuery[Any], objects: list[TModel]) -> None:
        """Reports the rows of inserted objects (``ChangeEvents``).

        Args:
            bulk_create: The bulk insert.
            objects: The objects.
        """
        await WriteSteps.report(bulk_create._connection, bulk_create.model, RowOperation.INSERT, instances=objects)
