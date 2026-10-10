from __future__ import annotations

from collections.abc import Generator, Iterable
from itertools import repeat
from operator import attrgetter
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import (
    FieldError,
    QueryError,
    UnSupportedError,
)
from hare.fields.database_default import DatabaseDefault
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.constraints.written_row_checks import WrittenRowChecks
from hare.models.write.generated_keys import GeneratedKeys
from hare.models.write.insert.insert_statement import InsertStatement
from hare.models.write.instance_values import InstanceValues
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.write_fields import WriteFields
from hare.models.write.write_steps import WriteSteps
from hare.native.native_modules import NativeModules
from hare.query.expressions import Q
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.constants import SAVED_OBJECT_ATTRIBUTE_NAMES
from hare.query.statements.write.bulk.bulk_objects import BulkObjects
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.bulk_create_reports import BulkCreateReports
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.conflict_emulation import ConflictEmulation
from hare.query.statements.write.bulk.create.copy_insert import CopyInsert
from hare.query.statements.write.bulk.create.default_rows_insert import DefaultRowsInsert
from hare.query.statements.write.bulk.create.inline_insert import InlineInsert
from hare.query.statements.write.bulk.create.multi_row_insert import MultiRowInsert
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.query.statements.write.bulk.create.template_insert import TemplateInsert
from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.values.literal_value import LiteralValue

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BulkCreateQuery(AwaitableQuery[TModel], Generic[TModel]):
    plannable = False
    #: The compiled ``rust.native.rows`` - marks the inserted objects saved in one call; None where it
    #: isn't built.
    native_rows: ClassVar[Any] = NativeModules.rows

    __slots__ = (
        "_objects",
        "_tenant_scoped",
        "_ignore_conflicts",
        "_batch_size",
        "_writer",
        "_reported_rows",
        "_existing_primary_keys",
        "_update_fields",
        "_on_conflict",
        "_on_conflict_constraint",
        "_conflict_where",
        "_returning",
        "_returning_explicitly_requested",
        "_use_copy",
        "_conflict_update_tenants",
        "_reads_back_rows",
    )

    def __init__(
        self,
        model: type[TModel],
        connection: DatabaseClient,
        objects: Iterable[TModel],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
        on_conflict_constraint: str | None = None,
        conflict_where: Q | RawSQLTerm | None = None,
        returning: bool | None = None,
        use_copy: bool = False,
        all_tenants: bool = False,
    ):
        # Meta.returning is a preference: inherited, it yields to use_copy or a dialect without
        # ordered RETURNING; passed explicitly, a conflict raises.
        returning_explicitly_requested = returning is not None
        returning = model._meta.returning if returning is None else returning
        if ignore_conflicts and update_fields:
            raise QueryError("ignore_conflicts and update_fields are mutually exclusive.")
        if on_conflict and on_conflict_constraint:
            raise QueryError("on_conflict and on_conflict_constraint are mutually exclusive.")
        if update_fields is not None:
            update_fields = model._meta.get_with_blind_indexes(
                BulkCreateQuery.get_db_field_names(model, update_fields, "update_fields")
            )
        if on_conflict is not None:
            # An encrypted field is unique through its blind index - the conflict is on the index.
            on_conflict = [
                model._meta.blind_index_fields.get(field_name, field_name)
                for field_name in BulkCreateQuery.get_db_field_names(model, on_conflict, "on_conflict")
            ]
        BulkCreateQuery.check_conflict_arguments(
            model, ignore_conflicts, update_fields, on_conflict, on_conflict_constraint, conflict_where
        )
        if use_copy:
            returning = BulkCreateQuery.check_copy_arguments(
                returning,
                returning_explicitly_requested,
                has_conflict_handling=bool(
                    ignore_conflicts or update_fields or on_conflict or on_conflict_constraint or conflict_where
                ),
            )
        # The checks needing the dialect are made when the query is awaited and its connection is
        # known.
        if returning and ignore_conflicts and not on_conflict:
            # Skipped rows aren't returned, so the returned ones are matched by the on_conflict
            # columns - required up front, not found out when a conflict happens.
            returning = BulkCreateQuery.get_inherited_returning(
                returning_explicitly_requested,
                "bulk_create(ignore_conflicts=True, returning=True) requires on_conflict=[...] "
                "naming the conflict target columns - a row skipped by ON CONFLICT DO NOTHING "
                "never appears in RETURNING, so the surviving rows can't be matched back to "
                "their objects without them (a named on_conflict_constraint isn't enough).",
            )
        if batch_size is not None and batch_size <= 0:
            raise QueryError(f"bulk_create(): batch_size must be a positive integer, got {batch_size!r}")
        objects = list(objects)
        BulkObjects.validate(model, objects, "bulk_create")
        tenant_scoped = bool(model._meta.tenant_field) and not all_tenants
        # ON CONFLICT resolves against the table's physical unique constraint regardless of tenant -
        # the DO UPDATE is limited to the active tenant's rows, so a conflicting row of another
        # tenant is left untouched (and its object unwritten).
        if tenant_scoped and update_fields and returning and not on_conflict:
            returning = BulkCreateQuery.get_inherited_returning(
                returning_explicitly_requested,
                f"bulk_create(update_fields=..., returning=True) on {model.__name__} "
                "requires on_conflict=[...] naming the conflict target columns - a row of "
                "another tenant is skipped by the tenant-limited ON CONFLICT DO UPDATE and "
                "never appears in RETURNING, so the surviving rows can only be matched back "
                "to their objects through them.",
            )
        super().__init__(model)
        self._objects = objects
        self._ignore_conflicts = ignore_conflicts
        self._batch_size = batch_size
        self._apply_connection(connection)
        self._update_fields = update_fields
        self._on_conflict = on_conflict
        self._on_conflict_constraint = on_conflict_constraint
        self._conflict_where = conflict_where
        self._returning = returning
        # Whether `returning=` was passed, not taken from Meta.returning - an explicit one a dialect
        # can't serve raises, an inherited one falls back to False.
        self._returning_explicitly_requested = returning_explicitly_requested
        self._use_copy = use_copy
        # Whether the rows belong to the tenant active when the query runs (Meta.tenant_field set,
        # no .all_tenants()).
        self._tenant_scoped = tenant_scoped
        # The tenant values an ON CONFLICT DO UPDATE is limited to - a conflicting row of another
        # tenant is left untouched instead of overwritten; None for no limit (every tenant).
        # Set when the query runs.
        self._conflict_update_tenants: tuple[Any, ...] | None = None
        #: Whether the values the database gives the written rows are read by their keys after the
        #: insert - ``returning=True`` on a database returning rows by reading them.
        self._reads_back_rows = False
        # Built per execution, by _run()/sql(), once the connection is chosen.
        self._writer: InstanceWriter = None  # type: ignore[assignment]
        #: The rows the statements returned for the report of the changes - None when the
        #: report doesn't need them.
        self._reported_rows: list[dict[str, Any]] | None = None
        #: The primary keys of the objects' rows that existed before an upsert - read where the
        #: dialect has no inserted-row flag.
        self._existing_primary_keys: set[tuple[Any, ...]] | None = None
        # Objects whose rows a COPY committed on its own connection during the current execution.

    async def _prepare_objects(self) -> bool:
        """Gets the objects ready to be written: scoped to the active tenant, their async defaults
        resolved - as save() does - their relations checked, the conflicting ones handled where the
        database has no ON CONFLICT, their keys handed out and their rows checked.

        Returns:
            False when no object is left to insert.
        """
        if self._tenant_scoped:
            self._scope_to_active_tenant()
        for obj in self._objects:
            if obj._await_when_save:
                await InstanceSaving.set_async_default_field(obj)
        if Tenancy.has_tenant_scoped_relations(self.model):
            await Tenancy.check_objects_relation_targets(self.model, self._objects, self._connection)
        features = self._connection.features
        if (self._ignore_conflicts or self._update_fields) and features.checks_constraints_before_write:
            # No ON CONFLICT on the database - the conflicting objects are skipped or updated first.
            await ConflictEmulation.apply(self)
            if not self._objects:
                return False
        # Nothing to hand out or check where the database generates the keys as it writes the rows
        # and checks the constraints itself.
        if (
            features.takes_keys_before_insert
            or features.checks_constraints_before_write
            or not features.supports_generated_keys
        ):
            # Keys the database hands out before the rows are written stay with the objects
            # whatever follows - a key of the series is never handed out again.
            series_keyed = await GeneratedKeys.assign(self.model, self._connection, self._objects)
            await WrittenRowChecks.check_new_rows(self.model, self._connection, self._objects, series_keyed)
        return True

    def _get_pre_write_states(self) -> list[tuple[bool, Any, dict[str, Any] | None, bool, str | None]]:
        """Each object's state before the write - put back when the call fails or its transaction
        rolls back.

        Returns:
            ``(_saved_in_db, pk, dirty snapshot, _custom_generated_pk, _connection_alias)`` of each.
        """
        track_dirty_fields = self.model._meta.track_dirty_fields
        primary_key_attribute = self.model._meta.primary_key_attribute
        objects = self._objects
        if not track_dirty_fields and type(primary_key_attribute) is str:
            # Read by attrgetter, without a Python call per object.
            return list(
                zip(
                    map(attrgetter("_saved_in_db"), objects),
                    map(attrgetter(primary_key_attribute), objects),
                    repeat(None),
                    map(attrgetter("_custom_generated_pk"), objects),
                    map(attrgetter("_connection_alias"), objects),
                )
            )
        return [
            (
                obj._saved_in_db,
                obj.pk,
                dict(obj._dirty_snapshot) if track_dirty_fields and obj._dirty_snapshot is not None else None,
                obj._custom_generated_pk,
                obj._connection_alias,
            )
            for obj in objects
        ]

    @staticmethod
    def check_conflict_arguments(
        model: type[Model],
        ignore_conflicts: bool,
        update_fields: list[str] | None,
        on_conflict: list[str] | None,
        on_conflict_constraint: str | None,
        conflict_where: Q | RawSQLTerm | None,
    ) -> None:
        """Raises for arguments of the ON CONFLICT clause that don't go together.

        Args:
            model: The model.
            ignore_conflicts: Whether a conflicting row is skipped.
            update_fields: The fields a conflicting row is updated with.
            on_conflict: The conflict target's columns.
            on_conflict_constraint: The conflict target's constraint.
            conflict_where: The conflict target's partial index condition.

        Raises:
            QueryError: The arguments don't go together, or an update field can't be written.
        """
        conflict_target = on_conflict or on_conflict_constraint
        if not ignore_conflicts and bool(update_fields) != bool(conflict_target):
            raise QueryError("update_fields and on_conflict/on_conflict_constraint need set in same time.")
        if update_fields:
            BulkCreateQuery.raise_if_update_fields_unwritable(model, update_fields)
        if conflict_where is not None:
            if not isinstance(conflict_where, (Q, RawSQLTerm)):
                raise QueryError(
                    "bulk_create(conflict_where=...) takes a Q over the model's fields or RawSQLTerm(...) of raw "
                    f"SQL, got {conflict_where!r}"
                )
            if on_conflict_constraint:
                raise QueryError("conflict_where and on_conflict_constraint are mutually exclusive.")
            if not on_conflict:
                raise QueryError("conflict_where requires on_conflict to name the partial index's own columns.")

    @staticmethod
    def check_copy_arguments(
        returning: bool, returning_explicitly_requested: bool, *, has_conflict_handling: bool
    ) -> bool:
        """Raises for arguments a bulk load (``use_copy``) can't serve.

        Args:
            returning: Whether the rows are read back.
            returning_explicitly_requested: Whether ``returning`` was passed rather than inherited.
            has_conflict_handling: Whether an ON CONFLICT argument is given.

        Returns:
            Whether the rows are read back - an inherited ``returning`` yields to the bulk load.

        Raises:
            UnSupportedError: ``returning`` was passed, or an ON CONFLICT argument is given.
        """
        if returning:
            returning = BulkCreateQuery.get_inherited_returning(
                returning_explicitly_requested,
                "use_copy and returning are mutually exclusive - a bulk load returns no rows.",
                UnSupportedError,
            )
        if has_conflict_handling:
            raise UnSupportedError(
                "use_copy does not support ON CONFLICT - a bulk load inserts every row, it handles no conflicts."
            )
        return returning

    @staticmethod
    def get_inherited_returning(
        returning_explicitly_requested: bool, message: str, error_class: type[Exception] = QueryError
    ) -> bool:
        """``returning`` where the rows can't be read back - an inherited ``Meta.returning`` falls back
        to False, a passed one raises.

        Args:
            returning_explicitly_requested: Whether ``returning`` was passed rather than inherited.
            message: Why the rows can't be read back.
            error_class: The error raised.

        Returns:
            False.

        Raises:
            QueryError: ``returning`` was passed - or ``error_class``.
        """
        if returning_explicitly_requested:
            raise error_class(message)
        return False

    @staticmethod
    def raise_if_update_fields_unwritable(model: type[Model], update_fields: Iterable[str]) -> None:
        """Rejects the ``update_fields`` an ``ON CONFLICT DO UPDATE`` mustn't write: a generated field
        is computed by the database, and updating ``Meta.tenant_field``, ``Meta.optimistic_lock_field``
        or ``Meta.soft_delete_field`` would move a row into another tenant, skip the lock's bump or
        bypass the soft-delete cascade.

        Args:
            model: The model created.
            update_fields: The column field names a conflicting row takes from its object.

        Raises:
            QueryError: A field is one of those.
        """
        update_fields_set = set(update_fields)
        meta = model._meta
        generated_fields = [
            field for field in update_fields_set if getattr(meta.fields_map.get(field), "generated", False)
        ]
        if generated_fields:
            raise QueryError(
                f"bulk_create() on {model.__name__} can't target generated field(s) "
                f"{generated_fields} in update_fields - they're computed by the database, not "
                "written to."
            )
        tenant_field = meta.tenant_field
        if tenant_field and update_fields_set.intersection(
            BulkCreateQuery.get_db_field_names(model, [tenant_field], "update_fields")
        ):
            raise QueryError(
                f"Cannot target '{meta.tenant_field}' via bulk_create()'s "
                "update_fields - ON CONFLICT DO UPDATE resolves against the table's physical "
                "unique constraint regardless of tenant scoping, so this would let a caller "
                "move an existing row into their own tenant."
            )
        if meta.optimistic_lock_field in update_fields_set:
            raise QueryError(
                f"Cannot target '{meta.optimistic_lock_field}' via bulk_create()'s "
                "update_fields - it's bumped automatically on every conflicting row."
            )
        if meta.soft_delete_field in update_fields_set:
            raise QueryError(
                f"Cannot target '{meta.soft_delete_field}' via bulk_create()'s "
                "update_fields - use .delete()/.restore() instead, a conflicting row must not "
                "be soft-deleted without its cascade or restored behind restore()'s back."
            )

    @staticmethod
    def get_db_field_names(model: type[Model], names: Iterable[str], parameter_name: str) -> list[str]:
        """Maps the field names given to ``update_fields``/``on_conflict`` onto the model fields
        that own a column - a forward FK/O2O relation name becomes its key field(s) (``owner`` ->
        ``owner_id``), ``pk`` becomes the primary key field(s).

        Args:
            model: The model being inserted into.
            names: Field names as the caller passed them.
            parameter_name: Names the parameter in the raised message.

        Returns:
            Column-owning field names, deduplicated, in the given order.

        Raises:
            FieldError: A name is not a field with a column on this model's own table.
        """
        meta = model._meta
        db_field_names: list[str] = []
        for name in names:
            field_object = meta.fields_map.get(name)
            if name == "pk" and field_object is None:
                db_field_names.extend(meta.primary_key_attribute_names)
            elif isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                db_field_names.extend(field_object.source_fields)
            elif name in meta.fields_db_projection:
                db_field_names.append(name)
            else:
                raise FieldError(
                    f'Unknown field "{name}" in bulk_create() {parameter_name} for model "{model.__name__}" - '
                    "expected a field with a column on its own table"
                )
        return list(dict.fromkeys(db_field_names))

    def _validate_dialect_dependent_options(self) -> None:
        """Checks on_conflict_constraint, conflict_where, returning and use_copy against the dialect of
        the connection the query runs on - known only once it is awaited.

        Raises:
            UnSupportedError: The dialect has no such option, or only the primary key can conflict
                on it. A ``returning`` inherited from ``Meta.returning`` falls back to False
                instead.
        """
        dialect = self._connection.dialect
        if self._on_conflict_constraint and not dialect.features.supports_conflict_constraint_names:
            raise UnSupportedError(f"on_conflict_constraint is not supported by the {dialect} dialect")
        if self._conflict_where and not dialect.features.supports_conflict_where:
            raise UnSupportedError(f"conflict_where is not supported by the {dialect} dialect")
        if (
            not dialect.features.supports_unique_constraints
            and not self._connection.features.checks_constraints_before_write
        ):
            ConflictClause.validate_primary_key_conflict_target(self)
        if (
            self._returning
            and not dialect.features.supports_returning
            and self._connection.features.returns_rows_by_reading
        ):
            # Written without RETURNING; the values the database gave the rows are read by their keys.
            self._returning = False
            self._reads_back_rows = True
        if self._returning and not dialect.features.supports_returning:
            if self._returning_explicitly_requested:
                raise UnSupportedError(f"bulk_create(returning=True) needs RETURNING, which {dialect} doesn't have")
            self._returning = False
        if (
            self._returning
            and not dialect.features.guarantees_returning_order
            and not ReturnedRowsMatching.objects_have_primary_keys(self)
        ):
            if not self._returning_explicitly_requested:
                self._returning = False
            else:
                raise UnSupportedError(
                    f"bulk_create(returning=True) on {dialect} needs every object's primary key set - "
                    "RETURNING's row order for a multi-row INSERT is not guaranteed there, so returned "
                    "rows are matched back to their objects by primary key."
                )
        if self._use_copy and not dialect.features.supports_copy:
            raise UnSupportedError(
                f"bulk_create(use_copy=True) is not supported on {dialect} - it has no bulk load of rows."
            )
        if dialect.features.copies_bulk_inserts and not (
            self._returning
            or self._ignore_conflicts
            or self._update_fields
            or self._on_conflict
            or self._on_conflict_constraint
            or self._conflict_where
        ):
            self._use_copy = True

    def _analyze_db_default_fields(self, columns: list[str]) -> set[str]:
        """The db_default fields every object leaves to the database - left out of the INSERT.

        Raises:
            QueryError: Some objects give a field a value and others leave it to its database
                default.
        """

        fields_map = self.model._meta.fields_map
        db_default_field_names = [
            candidate_name for candidate_name in columns if fields_map[candidate_name].has_db_default()
        ]
        if not db_default_field_names:
            return set()

        omit_fields: set[str] = set()
        for field_name in db_default_field_names:
            has_default = False
            has_value = False
            for instance in self._objects:
                if isinstance(getattr(instance, field_name), DatabaseDefault):
                    has_default = True
                else:
                    has_value = True
                if has_default and has_value:
                    raise QueryError(
                        f"Cannot use bulk_create() when field '{field_name}' has "
                        f"db_default and some instances provide explicit values while "
                        f"others rely on the database default. Either: "
                        f"(a) set the value explicitly on ALL instances, "
                        f"(b) omit it from ALL instances to use the database default, or "
                        f"(c) split into separate bulk_create() calls."
                    )
            if has_default and not has_value:
                omit_fields.add(field_name)

        return omit_fields

    def _get_insert_statement(self) -> InsertStatement:
        """The builder of this call's INSERTs, on the connection it runs on."""
        return InsertStatement(self.model, self._connection)

    def _filter_columns(self, omit_fields: set[str], include_generated: bool = False) -> list[str]:
        """The columns of ``_filtered_field_names()``."""
        return self._get_insert_statement().get_columns(self._filtered_field_names(omit_fields, include_generated))

    def _filtered_field_names(self, omit_fields: set[str], include_generated: bool = False) -> list[str]:
        """The fields an INSERT of this call writes.

        Args:
            omit_fields: Fields left to their database default.
            include_generated: The objects carry their primary key.

        Returns:
            The field names.
        """
        return self._get_insert_statement().get_field_names(
            with_primary_key=include_generated, omitted_field_names=omit_fields
        )

    def _make_queries(self, omit_fields: set[str] | None = None) -> tuple[str, str]:
        (insert_sql, _), (insert_sql_all, _) = TemplateInsert.make_template_statements(self, omit_fields)
        return insert_sql, insert_sql_all

    async def _execute_parameterized_inserts(
        self,
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool = False,
    ) -> None:
        """Inserts the objects by one of two strategies: a single-row INSERT run through
        ``execute_many()`` per chunk, or - where the driver's ``execute_many()`` scales poorly, or
        rows are read back - one multi-row INSERT per chunk. RETURNING is added only with
        ``populate_returned_fields``.
        """
        if not objects or not db_columns:
            return
        if (
            populate_returned_fields
            or BulkCreateReports.reports_returned_rows(self)
            or self._connection.features.execute_many_scales_poorly
        ):
            await MultiRowInsert.execute_via_multi_row_statement(
                self,
                db_columns,
                field_names,
                objects,
                omit_fields,
                populate_returned_fields=populate_returned_fields,
                column_arrays_source_sql=MultiRowInsert.get_column_arrays_rows_source_sql(self, field_names),
            )
        else:
            await TemplateInsert.execute_via_execute_many(self, db_columns, field_names, objects)

    def _get_fixed_parameter_count(self, db_columns: list[str], omit_fields: set[str]) -> int:
        """Bind parameters one multi-row ``INSERT`` of this call carries besides its rows' values -
        those of its ``ON CONFLICT ... DO UPDATE`` clause (the tenant scope, the version bump).

        Args:
            db_columns: The INSERT's columns.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            The parameter count.
        """
        if not self._update_fields:
            return 0
        probe_row = [LiteralValue(",".join("NULL" for _ in db_columns))]
        probe_query = self._get_insert_statement().get_query(
            db_columns, [probe_row], conflict=ConflictClause.get_conflict(self)
        )
        parameterizer = Parameterizer()
        probe_query.get_sql(probe_query.query_class.SQL_CONTEXT.copy(parameterizer=parameterizer))
        return len(parameterizer.values)

    async def _execute_many(
        self,
        insert_sql: str,
        insert_sql_all: str,
        effective_columns: list[str],
        effective_columns_all: list[str],
        omit_fields: set[str],
    ) -> None:
        # Two groups, each with its own column list: objects carrying a primary key write it.
        custom_pk_instances = [instance for instance in self._objects if instance._custom_generated_pk]
        regular_instances = [instance for instance in self._objects if not instance._custom_generated_pk]

        if effective_columns_all:
            await self._execute_parameterized_inserts(
                self._filter_columns(omit_fields, include_generated=True),
                effective_columns_all,
                custom_pk_instances,
                omit_fields,
                populate_returned_fields=self._returning,
            )
        elif custom_pk_instances:
            # When all columns are omitted, there's no VALUES row to parameterize at all.
            await DefaultRowsInsert.insert_default_rows(self, insert_sql_all, custom_pk_instances, omit_fields, False)

        if effective_columns:
            await self._execute_parameterized_inserts(
                self._filter_columns(omit_fields),
                effective_columns,
                regular_instances,
                omit_fields,
                populate_returned_fields=self._returning,
            )
        elif regular_instances:
            # All columns omitted (every non-generated column relies on its DB default) - no
            # VALUES row to parameterize at all.
            await DefaultRowsInsert.insert_default_rows(
                self, insert_sql, regular_instances, omit_fields, self._returning
            )

    def _runs_multiple_statements(
        self, effective_columns: list[str], effective_columns_all: list[str], omit_fields: set[str]
    ) -> bool:
        """Whether writing ``self._objects`` takes more than one statement (several batches,
        both the custom-pk and regular groups, or one DEFAULT VALUES statement per object) - the
        cases that need a transaction around them to stay all-or-nothing.

        Args:
            effective_columns: Column names of the regular group's INSERT.
            effective_columns_all: Column names of the custom-pk group's INSERT.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            True if more than one statement will run.
        """
        objects = list(self._objects)
        custom_pk_count = sum(map(attrgetter("_custom_generated_pk"), objects))
        uses_multi_row_statements = not self._use_copy and (
            self._connection.features.execute_many_scales_poorly or self._returning
        )
        statement_count = 0
        for group_size, columns in (
            (custom_pk_count, effective_columns_all),
            (len(objects) - custom_pk_count, effective_columns),
        ):
            if not group_size:
                continue
            if not columns:
                statement_count += (
                    1
                    if self._connection.dialect.parameters.get_default_rows_source_sql(group_size) is not None
                    else group_size
                )
                continue
            batch_size = self._batch_size
            if uses_multi_row_statements:
                batch_size = MultiRowInsert.get_multi_row_batch_size(self, columns, omit_fields)
            statement_count += 1 if batch_size is None else -(-group_size // max(1, batch_size))
        if (
            statement_count == 1
            and uses_multi_row_statements
            and len(ConflictClause.split_by_conflict_key(self, objects)) > 1
        ):
            return True
        return statement_count > 1

    async def _execute_writes(
        self,
        insert_sql: str,
        insert_sql_all: str,
        effective_columns: list[str],
        effective_columns_all: list[str],
        omit_fields: set[str],
    ) -> None:
        """Runs every INSERT/COPY statement for ``self._objects`` through whichever strategy
        ``use_copy`` selects.

        Args:
            insert_sql: DEFAULT VALUES/INSERT statement for the regular group.
            insert_sql_all: Same, for the custom-pk group.
            effective_columns: Column names of the regular group's INSERT.
            effective_columns_all: Column names of the custom-pk group's INSERT.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
        """
        if self._use_copy:
            await CopyInsert.execute_via_copy(
                self, insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields
            )
        else:
            await self._execute_many(insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields)

    def __await__(self) -> Generator[Any]:
        query = self._get_execution_query(True)
        query._validate_dialect_dependent_options()
        if query.model._meta.change_capture_needs is not None:
            return query._run_captured().__await__()
        return BulkCreateReports.report_bulk_create(query, query._run()).__await__()

    async def _run_captured(self) -> None:
        """Runs the insert of a model with ``Meta.change_capture`` and captures its rows, in one
        transaction."""
        async with ChangeCapturing.transaction(self._connection) as connection:
            if connection is not self._connection:
                self._apply_connection(connection)
                self._connection_explicitly_chosen = True
            await BulkCreateReports.report_bulk_create(self, self._run())

    def _scope_to_active_tenant(self) -> None:
        """Gives each object without a tenant the scope's one value and limits an ON CONFLICT DO
        UPDATE to the rows of the scope's tenants.

        Raises:
            QueryError: The model has no tenant scope, an object carries a tenant outside it, or
                an object carries none under a scope of several values.
        """
        if not self._tenant_scoped:
            return
        scope = WriteSteps.scope_to_active_tenant(
            self.model, self._objects, "bulk_create", fills_missing=True, requires_active=True
        )
        if self._update_fields:
            self._conflict_update_tenants = Tenancy.get_values(scope)

    async def _run(self) -> None:
        self._writer = InstanceWriter(self.model, self._connection)
        self._objects = list(self._objects)  # materialize for multi-pass
        if not await self._prepare_objects():
            return
        # Captured before the objects are serialized: to_db_value() stamps auto_now fields on the
        # instance.
        stamped_values = InstanceValues()
        stamped_values.capture(self._objects, WriteFields.of(self.model).stamped_on_insert_names)
        pre_write_states = self._get_pre_write_states()

        omit_fields = self._analyze_db_default_fields(self._writer.regular_columns)
        # Fields a RETURNING row sets on an object while later statements of this call can still fail.
        returned_field_names = (
            ReturnedRowsMatching.get_returned_field_names(self, omit_fields) if self._returning else []
        )
        returned_values = InstanceValues()
        returned_values.capture(self._objects, returned_field_names)

        insert_sql, insert_sql_all = self._make_queries(omit_fields)
        effective_columns = [column for column in self._writer.regular_columns if column not in omit_fields]
        effective_columns_all = [column for column in self._writer.regular_columns_all if column not in omit_fields]
        if self._use_copy:
            CopyInsert.check_copy_supported_types(self, omit_fields)
        try:
            if self._runs_multiple_statements(effective_columns, effective_columns_all, omit_fields):
                # Several statements run in one transaction - a failure leaves no row of the earlier
                # ones.
                original_connection = self._connection
                async with original_connection._in_transaction() as transaction_connection:
                    self._apply_connection(transaction_connection)
                    try:
                        await self._execute_writes(
                            insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields
                        )
                    finally:
                        self._apply_connection(original_connection)
            else:
                await self._execute_writes(
                    insert_sql, insert_sql_all, effective_columns, effective_columns_all, omit_fields
                )
        except BaseException:
            # The rows of earlier statements were rolled back with the failed one - the objects
            # they populated go back to their pre-call state too.
            for obj, pre_write_state in zip(self._objects, pre_write_states, strict=True):
                was_saved_in_db, _, _, custom_generated_pk, db_connection_name = pre_write_state
                returned_values.restore((obj,))
                stamped_values.restore((obj,))
                object.__setattr__(obj, "_saved_in_db", was_saved_in_db)
                object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)
                object.__setattr__(obj, "_connection_alias", db_connection_name)
            raise
        await self._record_written(pre_write_states, stamped_values, omit_fields)

    async def _record_written(
        self,
        pre_write_states: list[tuple[bool, Any, dict[str, Any] | None, bool, str | None]],
        stamped_values: InstanceValues,
        omit_fields: set[str],
    ) -> None:
        """Brings the objects in step with the rows just written, and registers what a rollback of
        the transaction puts back on them.

        Args:
            pre_write_states: Each object's state before the write.
            stamped_values: The values the write stamped on the objects.
            omit_fields: The fields left to the database's defaults.
        """
        track_dirty_fields = self.model._meta.track_dirty_fields

        # Every inserted object is marked saved - unless ON CONFLICT may have skipped its row: then
        # only the objects a RETURNING row was matched to are. Restores for a rollback are
        # registered only inside a transaction.
        restores_on_rollback = self._connection.is_transaction_client
        # What a rollback puts back on the objects, registered at once - one rollback and one commit
        # callback for the whole call.
        rollback_restores: list[tuple[Model, str, Any]] = []
        # What InstanceConnections.remember_connection() records, set directly on every object.
        connection_alias = self._connection.connection_alias
        if self._use_copy or not ConflictClause.may_skip_conflicting_rows(self):
            if restores_on_rollback:
                # A later rollback reverts the rows, not the instances - _saved_in_db and the
                # stamped values are registered to be put back.
                for obj in self._objects:
                    rollback_restores.append((obj, "_saved_in_db", False))
                    rollback_restores.extend(stamped_values.get_rollback_restores((obj,)))
            if self.native_rows is not None:
                self.native_rows.set_attribute_values(
                    self._objects, SAVED_OBJECT_ATTRIBUTE_NAMES, [True, connection_alias]
                )
            else:
                for obj in self._objects:
                    object.__setattr__(obj, "_saved_in_db", True)
                    object.__setattr__(obj, "_connection_alias", connection_alias)

        # The inserted values are the objects' dirty-tracking baseline - unless ON CONFLICT may have
        # skipped a row: which objects were written isn't known then, and they stay dirty.
        if track_dirty_fields and not ConflictClause.may_skip_conflicting_rows(self):
            for obj in self._objects:
                DirtyFields.snapshot_dirty_fields(obj)

        if self._reads_back_rows:
            await ReturnedRowsMatching.read_back_rows(self, omit_fields)

        if not restores_on_rollback:
            return
        for obj, pre_write_state in zip(self._objects, pre_write_states, strict=True):
            if not obj._saved_in_db:
                continue
            was_saved_in_db, old_pk, old_dirty_snapshot, _, _ = pre_write_state
            rollback_restores.append((obj, "_saved_in_db", was_saved_in_db))
            if obj.pk != old_pk:
                rollback_restores.append((obj, "pk", old_pk))
            if track_dirty_fields:
                rollback_restores.append((obj, "_dirty_snapshot", old_dirty_snapshot))
        RollbackRestores.register_rollback_restores(self._connection, rollback_restores)

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        """The INSERT statements the query runs - bound, each holds the row of the first object of
        its group (objects without and with a caller-given primary key).

        Args:
            parameters_inline: Whether values are rendered into the SQL instead of bound.

        Returns:
            ``(sql, bound values)`` of each statement.
        """
        self._validate_dialect_dependent_options()
        self._writer = InstanceWriter(self.model, self._connection)
        self._objects = list(self._objects)
        self._scope_to_active_tenant()
        omit_fields = self._analyze_db_default_fields(self._writer.regular_columns)
        if parameters_inline:
            return [(sql, []) for sql in InlineInsert.make_inline_statements(self, omit_fields)]
        statements: list[tuple[str, list[Any]]] = []
        for (sql, statement_values), custom_generated_pk in zip(
            TemplateInsert.make_template_statements(self, omit_fields), (False, True), strict=True
        ):
            group = [obj for obj in self._objects if obj._custom_generated_pk == custom_generated_pk]
            if group:
                field_names = self._filtered_field_names(omit_fields, include_generated=custom_generated_pk)
                rows = BulkWriteBatches.serialize_instances(
                    self.model, self._connection.dialect.types, group[:1], field_names
                )
                statements.append((sql, [*(rows[0] if rows else []), *statement_values]))
        return statements
