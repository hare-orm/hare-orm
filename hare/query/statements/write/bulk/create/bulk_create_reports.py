from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import HareError, UnSupportedError
from hare.fields.database_default import DatabaseDefault
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions import Q
from hare.query.expressions.term_expression import TermExpression
from hare.query.hare_column_names import HareColumnNames
from hare.query.statements.constants import UPSERT_INSERTED_FLAG_ALIAS
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.sql.terms.values.literal_value import LiteralValue

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery
    from hare.query.statements.write.bulk.create.prepared_bulk_insert import PreparedBulkInsert


class BulkCreateReports:
    """The report of the rows a bulk insert changed (ChangeEvents): the inserted objects, or the rows
    the statements return when conflicting rows are skipped or updated, and of a failed insert on a
    database without transactions the rows its earlier statements left."""

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
    def prepare_returned_rows(bulk_create: BulkCreateQuery[Any], *, listened: bool) -> None:
        """Select the same returned-row reporting for a preview and a write."""
        binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        if (
            (
                listened
                and (bulk_create._ignore_conflicts or bulk_create._update_fields)
                or binding.change_capture_needs is not None
            )
            and bulk_create._connection.features.supports_returning
            and not bulk_create._use_copy
            and binding.has_primary_key
        ):
            bulk_create._reported_rows = []

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
        meta: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        terms: list[Any] = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
        if bulk_create._update_fields:
            flag_sql = bulk_create._connection.dialect.clauses.get_upsert_inserted_flag_sql()
            if flag_sql is not None:
                terms.append(
                    LiteralValue(
                        flag_sql,
                        alias=HareColumnNames.get_free_name(UPSERT_INSERTED_FLAG_ALIAS, (meta,)),
                    )
                )
        return terms

    @staticmethod
    async def read_existing_primary_keys(bulk_create: BulkCreateQuery[Any], prepared: PreparedBulkInsert) -> None:
        """Reads which objects' rows exist before an upsert writes - on a database without an
        inserted-row flag, so the report can tell an updated row from an inserted one.

        Args:
            bulk_create: The bulk insert.
            prepared: The prepared write for this execution, including its converted values.
        """
        # Deferred import: the queryset package builds bulk_create() queries.
        from hare.query.queryset.queryset import QuerySet

        meta: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        conflict_names = ConflictClause.get_conflict_key_field_names(bulk_create)
        objects = bulk_create._objects
        queryset: QuerySet[Any] = QuerySet.from_binding(meta, context=bulk_create._context).using(
            bulk_create._connection
        )
        if any(isinstance(getattr(obj, name), DatabaseDefault) for obj in objects for name in conflict_names):
            # The conflict key isn't known until the database writes it. Its returned PK is compared
            # against the existing rows instead, including when a default is an SQL expression.
            existing_keys = await queryset.values_list(*meta.primary_key_attribute_names)
            bulk_create._existing_primary_keys = {tuple(row) for row in existing_keys}
            return
        types = bulk_create._connection.dialect.types
        keys = set()
        values: list[Any] = []
        for batch in prepared.batches:
            bound_rows = batch.bound_rows
            if bound_rows is None or any(name not in bound_rows.field_names for name in conflict_names):
                existing_keys = await queryset.values_list(*meta.primary_key_attribute_names)
                bulk_create._existing_primary_keys = {tuple(row) for row in existing_keys}
                return
            keys.update(bound_rows.get_keys(meta, types, conflict_names))
            position = bound_rows.field_names.index(conflict_names[0])
            values.extend(row[position] for row in bound_rows.values)
        # These are the actual INSERT values, including blind indexes. An ordinary
        # field lookup would encode the original source a second time.
        criterion = meta.basetable[meta.fields_db_projection[conflict_names[0]]].isin(values)
        existing = await queryset.filter(Q.from_expression(TermExpression(criterion))).values_list(
            *meta.primary_key_attribute_names, *conflict_names
        )
        pk_count = len(meta.primary_key_attribute_names)
        bulk_create._existing_primary_keys = {
            tuple(row[:pk_count]) for row in existing if ReturnedValues.get_hashable(row[pk_count:]) in keys
        }

    @staticmethod
    async def report_bulk_create(
        bulk_create: BulkCreateQuery[TModel], create: Awaitable[list[TModel]]
    ) -> list[TModel]:
        """Runs the insert and reports its rows (``ChangeEvents``) - of a failed insert, only the rows
        its earlier statements left on a database without transactions. Where conflicting rows are
        skipped or updated, the returned rows tell inserted from updated.

        Args:
            bulk_create: The bulk insert.
            create: The insert.

        Returns:
            The objects the insert gives.
        """
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        listened = ChangeEvents.is_observed(model_binding)
        capture_needs = model_binding.change_capture_needs
        BulkCreateReports.prepare_returned_rows(bulk_create, listened=listened)
        reads_existing_keys = (
            BulkCreateReports.reports_returned_rows(bulk_create)
            and bool(bulk_create._update_fields)
            and (
                bulk_create._connection.dialect.clauses.get_upsert_inserted_flag_sql() is None
                or (capture_needs is not None and capture_needs.reads_before)
            )
        )
        captured = (
            None
            if capture_needs is None
            else CapturedWrite(
                model_binding,
                capture_needs,
                bulk_create._connection,
                RowOperation.INSERT,
                changed=bulk_create._update_fields,
            )
        )
        before_by_pk: dict[Any, dict[str, Any]] | None = None

        async def read_before_write(prepared: PreparedBulkInsert) -> None:
            nonlocal before_by_pk
            # Read once the insert has given the objects the values it writes - a tenant, an async default,
            # a blind index of a conflict key.
            if reads_existing_keys:
                await BulkCreateReports.read_existing_primary_keys(bulk_create, prepared)
            if captured is not None:
                before_by_pk = await BulkCreateReports.read_values_before(bulk_create, captured)

        bulk_create._read_before_write = read_before_write
        try:
            objects = await create
        except BaseException:
            stored_pks = bulk_create._stored_primary_keys
            if stored_pks:
                if captured is not None:
                    await BulkCreateReports.capture_keys(captured, None, stored_pks, [])
                await WriteSteps.report(bulk_create._connection, model_binding, RowOperation.INSERT, pks=stored_pks)
            raise
        if captured is not None:
            inserted_pks, updated_pks = BulkCreateReports.get_written_keys(bulk_create)
            await BulkCreateReports.capture_keys(captured, before_by_pk, inserted_pks, updated_pks)
        if bulk_create._reported_rows is None:
            await WriteSteps.report(
                bulk_create._connection, model_binding, RowOperation.INSERT, instances=bulk_create._objects
            )
            if bulk_create._update_fields and listened and bulk_create._objects:
                await WriteSteps.report(
                    bulk_create._connection,
                    model_binding,
                    RowOperation.UPDATE,
                    fields=bulk_create._update_fields,
                )
            return objects
        await BulkCreateReports.report_returned_rows(bulk_create)
        return objects

    @staticmethod
    async def read_values_before(
        bulk_create: BulkCreateQuery[Any], captured: CapturedWrite
    ) -> dict[Any, dict[str, Any]] | None:
        """The captured fields of the rows an upsert of a model with ``Meta.change_capture`` may
        update, as they are before it - locked.

        Args:
            bulk_create: The bulk insert.
            captured: The capture of the insert.

        Returns:
            The values by primary key, None when the payload holds no rows before a write.
        """
        existing_primary_keys = bulk_create._existing_primary_keys
        if not captured.needs.reads_before or not existing_primary_keys:
            return None
        return await captured.read_before([key[0] if len(key) == 1 else key for key in existing_primary_keys])

    @staticmethod
    async def read_stored_primary_keys(bulk_create: BulkCreateQuery[Any]) -> list[Any]:
        """The keys of the objects' rows a failed insert left - on a database without transactions,
        when the rows are reported or captured.

        Args:
            bulk_create: The bulk insert, failed.

        Returns:
            The keys of the rows found.
        """
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        if not ChangeEvents.is_observed(model_binding) and model_binding.change_capture_needs is None:
            return []
        keys = [obj.pk for obj in bulk_create._objects if obj.pk is not None]
        if not keys:
            return []
        # Deferred import: the queryset package builds bulk_create() queries.
        from hare.query.queryset.queryset import QuerySet

        primary_key_attribute_names = model_binding.primary_key_attribute_names
        try:
            rows = await (
                QuerySet.from_binding(model_binding, context=bulk_create._context)
                .using(bulk_create._connection)
                .filter(pk__in=keys)
                .values_list(*primary_key_attribute_names)
            )
        except HareError:
            # The connection the insert failed on can't be read either - the insert's own error is raised.
            return []
        return [row[0] if len(row) == 1 else tuple(row) for row in rows]

    @staticmethod
    def get_written_keys(bulk_create: BulkCreateQuery[Any]) -> tuple[list[Any], list[Any]]:
        """The primary keys of the rows the insert wrote.

        Args:
            bulk_create: The bulk insert, run.

        Returns:
            The keys of the rows inserted, and of those an upsert updated.

        Raises:
            UnSupportedError: The rows can't be named - a bulk load of objects whose keys the database
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
        meta: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        primary_key_names = meta.primary_key_attribute_names
        pk_columns = [meta.fields_db_projection[name] for name in primary_key_names]
        pk_fields = [meta.fields_map[name] for name in primary_key_names]
        types = bulk_create._connection.dialect.types
        inserted_pks: list[Any] = []
        updated_pks: list[Any] = []
        existing_primary_keys = bulk_create._existing_primary_keys
        # A key an earlier row of the call inserted - a later object of that key updates the row.
        inserted_keys: set[tuple[Any, ...]] = set()
        inserted_flag_column = HareColumnNames.get_free_name(UPSERT_INSERTED_FLAG_ALIAS, (meta,))
        readers = tuple(types.get_python_reader(field) for field in pk_fields)
        for row in bulk_create._reported_rows or ():
            key = tuple(read(row[column]) for read, column in zip(readers, pk_columns, strict=True))
            pk = key if len(key) > 1 else key[0]
            if inserted_flag_column in row:
                inserted = bool(row[inserted_flag_column])
            else:
                inserted = (existing_primary_keys is None or key not in existing_primary_keys) and (
                    key not in inserted_keys
                )
                if inserted:
                    inserted_keys.add(key)
            (inserted_pks if inserted else updated_pks).append(pk)
        return inserted_pks, updated_pks

    @staticmethod
    async def capture_keys(
        captured: CapturedWrite,
        before_by_pk: dict[Any, dict[str, Any]] | None,
        inserted_pks: list[Any],
        updated_pks: list[Any],
    ) -> None:
        """Captures rows the insert wrote, by their keys - their values read after it, in its transaction.

        Args:
            captured: The capture of the insert.
            before_by_pk: The upserted rows' values before the write, by primary key.
            inserted_pks: The keys of the rows inserted.
            updated_pks: The keys of the rows an upsert updated.
        """
        pks = [*inserted_pks, *updated_pks]
        if not pks:
            return
        capture_needs = captured.needs
        values_by_pk: dict[Any, Any] = {}
        if capture_needs.reads_values or capture_needs.tenant_field_name:
            values_by_pk = await captured.read(pks)
        before_by_pk = before_by_pk or {}
        types = captured.connection.dialect.types
        changes = []
        for operation, operation_pks in (
            (RowOperation.INSERT, inserted_pks),
            (RowOperation.UPDATE, updated_pks),
        ):
            for pk in operation_pks:
                previous = ChangeCapturing.get_read_row(captured._binding, types, pk, before_by_pk)
                current = ChangeCapturing.get_read_row(captured._binding, types, pk, values_by_pk)
                changes.append(
                    captured.build_change(
                        pk,
                        before=previous,
                        after=None if current is None else current[0],
                        tenant=None if current is None else current[1],
                        operation=operation,
                    )
                )
        await captured.capture(changes)

    @staticmethod
    async def report_returned_rows(bulk_create: BulkCreateQuery[Any]) -> None:
        """Reports the rows the statements returned - inserted, or updated by an upsert.

        Args:
            bulk_create: The bulk insert.
        """
        inserted_pks, updated_pks = BulkCreateReports.get_written_keys(bulk_create)
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        if inserted_pks:
            await WriteSteps.report(bulk_create._connection, model_binding, RowOperation.INSERT, pks=inserted_pks)
        if updated_pks:
            await WriteSteps.report(
                bulk_create._connection,
                model_binding,
                RowOperation.UPDATE,
                pks=updated_pks,
                fields=bulk_create._update_fields,
            )
