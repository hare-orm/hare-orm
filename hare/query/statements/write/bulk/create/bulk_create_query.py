from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from copy import copy
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.core.registries import Registries
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import (
    QueryError,
    UnSupportedError,
    ValidationError,
)
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation
from hare.models.constants import WRITE_RESTORE_FAILURES_MESSAGE
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.declarations import WriteOutcome
from hare.models.write.generated_keys import GeneratedKeys
from hare.models.write.insert.insert_statement import InsertStatement
from hare.models.write.instance_snapshot import InstanceSnapshot
from hare.models.write.instance_values import InstanceValues
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.returned_values import ReturnedValues
from hare.models.write.rules.constants import (
    BULK_CREATE_METHOD_NAME,
    BULK_CREATE_ON_CONFLICT_NAME,
    BULK_CREATE_UPDATE_FIELDS_NAME,
)
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.rules.tenant_write_rules import TenantWriteRules
from hare.models.write.write_finalizer import WriteFinalizer
from hare.models.write.write_steps import WriteSteps
from hare.query.execution.write_statements import WriteCopy, WriteMany, WritePreview
from hare.query.expressions import Q
from hare.query.queryset.arguments.collection_arguments import CollectionArguments
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.constants import (
    BULK_INSERT_COPY_PREVIEW_MESSAGE,
    BULK_INSERT_DEFERRED_PREVIEW_MESSAGE,
    IGNORE_CONFLICTS_KEYLESS_REASON,
    IGNORE_CONFLICTS_MATCHING_MESSAGE,
    IGNORE_CONFLICTS_NO_RETURNING_REASON,
    MISSING_CONFLICT_TARGET_CONDITION_TEXT,
    MISSING_CONFLICT_TARGET_MESSAGE,
    PRIMARY_KEY_CONFLICT_CONSTRAINT,
)
from hare.query.statements.write.bulk.bulk_objects import BulkObjects
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.bulk_create_reports import BulkCreateReports
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.prepared_bulk_insert import PreparedBulkInsert
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.values.literal_value import LiteralValue

if TYPE_CHECKING:
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class BulkCreateQuery(AwaitableQuery[TModel], Generic[TModel]):
    """``queryset.bulk_create()`` - a row of each object inserted, by batches, conflicts handled as asked."""

    plannable = False

    __slots__ = (
        "_objects",
        "_tenant_scoped",
        "_ignore_conflicts",
        "_batch_size",
        "_writer",
        "_reported_rows",
        "_inserted_objects",
        "_existing_primary_keys",
        "_stored_primary_keys",
        "_update_fields",
        "_on_conflict",
        "_on_conflict_constraint",
        "_conflict_where",
        "_returning",
        "_returning_explicitly_requested",
        "_use_copy",
        "_conflict_update_tenants",
        "_read_before_write",
        "_on_write_complete",
    )

    def __init__(
        self,
        model: type[TModel],
        connection: DatabaseClient | None,
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
        on_conflict, on_conflict_constraint = BulkCreateQuery.get_primary_key_conflict_target(
            model, on_conflict, on_conflict_constraint
        )
        if update_fields is not None:
            update_fields = CollectionArguments.get_list(update_fields, "bulk_create() update_fields")
        if on_conflict is not None:
            on_conflict = CollectionArguments.get_list(on_conflict, "bulk_create() on_conflict")
        if update_fields is not None:
            update_fields = RowWriteRules.get_stored_field_names(model, update_fields, BULK_CREATE_UPDATE_FIELDS_NAME)
        if on_conflict is not None:
            # An encrypted field is unique through its blind index - the conflict is on the index.
            on_conflict = [
                model._meta.blind_index_fields.get(field_name, field_name)
                for field_name in BulkCreateQuery.get_conflict_field_names(model, on_conflict)
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
        BulkWriteBatches.check_batch_size(batch_size, "bulk_create")
        objects = CollectionArguments.get_list(objects, "bulk_create()")
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
        # Built per execution, by _run()/sql(), once the connection is chosen.
        self._writer: InstanceWriter = None  # type: ignore[assignment]
        #: The rows the statements returned for the report of the changes - None when the
        #: report doesn't need them.
        self._reported_rows: list[dict[str, Any]] | None = None
        #: The primary keys of the objects' rows that existed before an upsert - read where the
        #: dialect has no inserted-row flag.
        self._existing_primary_keys: set[tuple[Any, ...]] | None = None
        #: The keys of the rows a failed insert left on a database without transactions - the
        #: statements before the failing one wrote them, and they are reported.
        self._stored_primary_keys: list[Any] = []
        #: The objects a RETURNING row was matched to - those inserted, where ``ignore_conflicts`` may
        #: skip some; None otherwise.
        self._inserted_objects: list[TModel] | None = None
        #: What the report reads before the rows are written - once the objects carry the values the
        #: insert writes (their tenant, async defaults, blind indexes); None for nothing.
        self._read_before_write: Callable[[], Awaitable[None]] | None = None
        self._on_write_complete: Callable[[DatabaseClient], None] | None = None

    async def _prepare_objects(self) -> None:
        """Gets the objects ready to be written: scoped to the active tenant, their async defaults
        resolved - as save() does - their relations checked and their keys handed out."""
        self._prepare_object_values()
        for obj in self._objects:
            if obj._await_when_save:
                await InstanceSaving.set_async_default_field(obj)
        if Tenancy.has_tenant_scoped_relations(self.model):
            await Tenancy.check_objects_relation_targets(self.model, self._objects, self._connection)
        features = self._connection.features
        if not features.supports_returning:
            for obj in self._objects:
                self._writer.raise_if_key_left_to_database(obj)
        # Nothing to hand out where the database generates the keys as it writes the rows.
        if features.takes_keys_before_insert or not features.supports_generated_keys:
            # Keys the database hands out before the rows are written stay with the objects
            # whatever follows - a key of the series is never handed out again.
            await GeneratedKeys.assign(self.model, self._connection, self._objects)

    @staticmethod
    def get_primary_key_conflict_target(
        model: type[Model], on_conflict: Iterable[str] | None, on_conflict_constraint: str | None
    ) -> tuple[Iterable[str] | None, str | None]:
        """The conflict target ``on_conflict_constraint="pk"`` names - the primary key's fields as
        ``on_conflict``; the constraint of a key ``WITHOUT OVERLAPS``, which a database infers from no
        columns.

        Args:
            model: The model.
            on_conflict: The conflict target's fields given.
            on_conflict_constraint: The conflict target's constraint given.

        Returns:
            ``on_conflict`` and ``on_conflict_constraint``.

        Raises:
            QueryError: The model has no primary key.
        """
        if on_conflict_constraint != PRIMARY_KEY_CONFLICT_CONSTRAINT:
            return on_conflict, on_conflict_constraint
        meta = model._meta
        if not meta.has_primary_key:
            raise QueryError(
                f'bulk_create(on_conflict_constraint="pk") names the primary key - {model.__name__} has none'
            )
        if not meta.pk_without_overlaps:
            return list(meta.primary_key_attribute_names), None
        return None, GeneratedNames.get_index_name(
            GeneratedNamePrefix.PRIMARY_KEY,
            model,
            [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names],
        )

    @staticmethod
    def get_no_overlap_keys(model: type[Model]) -> list[tuple[frozenset[str], str | None]]:
        """The keys of the model compared ``WITHOUT OVERLAPS`` - their fields, as ``on_conflict`` names
        them, and their constraint's name.

        Args:
            model: The model.

        Returns:
            The keys: the primary key's, then the ``UniqueConstraint(without_overlaps=True)`` ones.
        """
        meta = model._meta
        keys: list[tuple[frozenset[str], str | None]] = []
        if meta.pk_without_overlaps:
            columns = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
            keys.append(
                (
                    frozenset(meta.primary_key_attribute_names),
                    GeneratedNames.get_index_name(GeneratedNamePrefix.PRIMARY_KEY, model, columns),
                )
            )
        for constraint in meta.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.without_overlaps:
                field_names = BulkCreateQuery.get_conflict_field_names(model, constraint.fields)
                keys.append((frozenset(field_names), constraint.name))
        return keys

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
            RowWriteRules.get(model, WriteType.CONFLICT_UPDATE, update_fields, BULK_CREATE_UPDATE_FIELDS_NAME)
        if conflict_where is not None:
            if not isinstance(conflict_where, (Q, RawSQLTerm)):
                raise QueryError(
                    "bulk_create(conflict_where=...) takes a Q over the model's fields or RawSQLTerm(...) of raw "
                    f"SQL, got {ValueDescriptions.get_shown_value(conflict_where)}"
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
                "use_copy takes no conflict handling - a bulk load inserts every row, it handles no conflicts."
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
    def get_conflict_field_names(model: type[Model], names: Iterable[str]) -> list[str]:
        """The fields holding the columns a conflict target names - a forward relation its key fields,
        ``pk`` the primary key's; a generic foreign key names none, each branch keeping its own
        uniqueness.

        Args:
            model: The model inserted into.
            names: The names as the caller gave them.

        Returns:
            The field names, each once, in the given order.

        Raises:
            FieldError: A name is no field stored in the model's table, or a generic foreign key.
        """
        return RowWriteRules.get_stored_field_names(
            model, names, BULK_CREATE_ON_CONFLICT_NAME, expands_generic_keys=False
        )

    def _validate_dialect_dependent_options(self) -> None:
        """Checks on_conflict_constraint, conflict_where, returning and use_copy against the dialect of
        the connection the query runs on - known only once it is awaited.

        Raises:
            UnSupportedError: The dialect has no such option, or only the primary key can conflict
                on it. A ``returning`` inherited from ``Meta.returning`` falls back to False
                instead.
        """
        dialect = self._connection.dialect
        if (
            self._ignore_conflicts or self._update_fields or self._on_conflict or self._on_conflict_constraint
        ) and not dialect.features.supports_insert_conflicts:
            raise UnSupportedError(
                "bulk_create() with ignore_conflicts, update_fields or on_conflict needs INSERT ... ON CONFLICT, "
                f"which {dialect.name} doesn't have"
            )
        if (self._on_conflict or self._update_fields) and (
            no_overlap_keys := BulkCreateQuery.get_no_overlap_keys(self.model)
        ):
            dialect.clauses.raise_if_conflict_target_unusable(
                self.model.__name__,
                no_overlap_keys,
                self._update_fields,
                self._on_conflict,
                self._on_conflict_constraint,
            )
        if self._on_conflict_constraint and not dialect.features.supports_conflict_constraint_names:
            raise UnSupportedError(f"on_conflict_constraint is not supported by the {dialect} dialect")
        if self._conflict_where and not dialect.features.supports_conflict_where:
            raise UnSupportedError(f"conflict_where is not supported by the {dialect} dialect")
        if not dialect.features.supports_unique_constraints:
            ConflictClause.validate_primary_key_conflict_target(self)
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
        if self._ignore_conflicts:
            # The objects inserted are those RETURNING gives a row of - skipped ones have none.
            if not dialect.features.supports_returning:
                raise UnSupportedError(
                    IGNORE_CONFLICTS_MATCHING_MESSAGE.format(
                        model=self.model.__name__, reason=IGNORE_CONFLICTS_NO_RETURNING_REASON.format(dialect=dialect)
                    )
                )
            self._returning = True
        if dialect.features.copies_bulk_inserts and not (
            self._returning
            or self._ignore_conflicts
            or self._update_fields
            or self._on_conflict
            or self._on_conflict_constraint
            or self._conflict_where
        ):
            self._use_copy = True

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

    def _get_fixed_parameter_count(self, db_columns: list[str]) -> int:
        """Bind parameters one multi-row ``INSERT`` of this call carries besides its rows' values -
        those of its ``ON CONFLICT ... DO UPDATE`` clause (the tenant scope, the version bump).

        Args:
            db_columns: The INSERT's columns.

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

    async def _execute_bound(self) -> list[TModel]:
        query = self._get_execution_query(True)
        query._validate_dialect_dependent_options()
        if query.model._meta.change_capture_needs is not None:
            return await WriteSteps.run_whole(query._connection, query._run_captured())
        return await WriteSteps.get_whole_run(query._connection, query._run_reported())

    async def _run_captured(self) -> list[TModel]:
        """Runs the insert of a model with ``Meta.change_capture`` and captures its rows, in one
        transaction.

        Returns:
            The objects, as given.
        """
        async with ChangeCapturing.transaction(self._connection) as connection:
            if connection is not self._connection:
                self._apply_connection(connection)
                self._connection_explicitly_chosen = True
            return await self._run_reported()

    async def _run_reported(self) -> list[TModel]:
        """Runs the insert and reports its rows.

        Returns:
            The objects, as given.
        """
        return await BulkCreateReports.report_bulk_create(self, self._run())

    def _prepare_object_values(self) -> None:
        """Keeps given primary keys and scopes the objects to the active tenant.

        A loaded or reassigned generated key reaches the INSERT as a key supplied to a new
        instance does. Hydration does not set the explicit-key flag used by insert grouping;
        normalize it at the write boundary without adding work to every row read.
        Objects without a tenant receive the scope's one value, and ON CONFLICT DO UPDATE is
        limited to the rows of the scope's tenants.

        Raises:
            QueryError: The model has no tenant scope, an object carries a tenant outside it, or
                an object carries none under a scope of several values.
        """
        primary_key_name = self.model._meta.generated_pk_field_name
        if primary_key_name is not None:
            for obj in self._objects:
                if not obj._custom_generated_pk and getattr(obj, primary_key_name, None) is not None:
                    object.__setattr__(obj, "_custom_generated_pk", True)
        if not self._tenant_scoped:
            if self.model._meta.tenant_field:
                # .all_tenants() widens the rows a query reads, not the rows an object points at - each
                # object is checked under the active scope, as create() checks it.
                TenantWriteRules.scope_objects(
                    self.model, self._objects, "bulk_create", fills_missing=True, requires_active=False
                )
            return
        scope = TenantWriteRules.scope_objects(
            self.model, self._objects, "bulk_create", fills_missing=True, requires_active=True
        )
        if self._update_fields:
            self._conflict_update_tenants = Tenancy.get_values(scope)

    async def _run(self) -> list[TModel]:
        """Inserts the objects.

        Returns:
            The supplied objects, or only those inserted when ``ignore_conflicts`` skips rows.
        """
        self._writer = InstanceWriter(self.model, self._connection)
        given_objects = self._objects
        self._objects = list(given_objects)
        try:
            return await self._write_prepared_objects(given_objects)
        finally:
            if self._on_write_complete is not None:
                self._on_write_complete(self._connection)

    async def _write_prepared_objects(self, given_objects: list[TModel]) -> list[TModel]:
        """Prepares the objects - their tenant, defaults and keys - and writes them.

        Args:
            given_objects: The objects as given.

        Returns:
            The objects, as given - with ``ignore_conflicts``, those inserted.

        Raises:
            UnSupportedError: ``ignore_conflicts`` with objects whose keys the database generates, and no
                ``on_conflict`` to match them by.
        """
        # The stamps and blind indexes of the insert - one moment for every object, put back when the call
        # fails.
        stamped_values = InstanceValues()
        snapshots = [InstanceSnapshot.capture_persistence(obj) for obj in self._objects]
        returned_values = InstanceValues()
        omit_fields: set[str] = set()
        write_started = False
        try:
            await self._prepare_objects()
            self._inserted_objects = [] if ConflictClause.may_skip_conflicting_rows(self) else None
            if (
                self._ignore_conflicts
                and not self._on_conflict
                and not ReturnedRowsMatching.objects_have_primary_keys(self)
            ):
                raise UnSupportedError(
                    IGNORE_CONFLICTS_MATCHING_MESSAGE.format(
                        model=self.model.__name__, reason=IGNORE_CONFLICTS_KEYLESS_REASON
                    )
                )
            RowWriteRules.get(self.model, WriteType.INSERT, None, BULK_CREATE_METHOD_NAME).stamp(
                self._objects, stamped_values
            )
            if self._read_before_write is not None:
                await self._read_before_write()
            prepared = PreparedBulkInsert(self)
            omit_fields = prepared.omitted_fields
            # Capture before executing any batch, including the values set by RETURNING.
            returned_field_names = (
                ReturnedRowsMatching.get_returned_field_names(self, omit_fields) if self._returning else []
            )
            returned_values.capture(self._objects, returned_field_names)
            WriteFinalizer.register_snapshots(snapshots, self._connection)
            if prepared.bound.requires_transaction:
                # Several statements run in one transaction - a failure leaves no row of the earlier
                # ones.
                original_connection = self._connection
                async with original_connection._in_transaction() as transaction_connection:
                    self._apply_connection(transaction_connection)
                    try:
                        WriteFinalizer.register_snapshots(snapshots, transaction_connection)
                        write_started = True
                        await prepared.execute(self)
                    finally:
                        self._apply_connection(original_connection)
            else:
                write_started = True
                await prepared.execute(self)
        except BaseException as error:
            cleanup_errors: list[BaseException] = []
            if write_started and not self._connection.features.supports_transactions:
                # Nothing rolled the earlier statements back - their rows stay, read before the objects
                # give their keys back.
                try:
                    self._stored_primary_keys = await BulkCreateReports.read_stored_primary_keys(self)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            # The rows of earlier statements were rolled back with the failed one - the objects
            # they populated go back to their pre-call state too.
            restore_snapshots = list(snapshots)
            restore_snapshots.extend(
                InstanceSnapshot(obj, values) for obj, values in stamped_values.old_values.values()
            )
            restore_snapshots.extend(
                InstanceSnapshot(obj, values) for obj, values in returned_values.old_values.values()
            )
            try:
                InstanceSnapshot.restore_all(restore_snapshots)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise BaseExceptionGroup(WRITE_RESTORE_FAILURES_MESSAGE, [error, *cleanup_errors]) from None
            if isinstance(error, (ValidationError, TypeError)):
                RowWriteRules.get(self.model, WriteType.INSERT, None, BULK_CREATE_METHOD_NAME).raise_if_unbound_values(
                    self._objects, self._filtered_field_names(omit_fields, include_generated=True), error
                )
            if self._on_conflict and self._connection.is_missing_conflict_target_error(error):
                raise QueryError(
                    MISSING_CONFLICT_TARGET_MESSAGE.format(
                        model=self.model.__name__,
                        fields=", ".join(self._on_conflict),
                        condition=MISSING_CONFLICT_TARGET_CONDITION_TEXT if self._conflict_where is not None else "",
                    )
                ) from error
            raise
        self._record_written(snapshots, stamped_values)
        if not self._ignore_conflicts:
            return given_objects
        inserted_object_ids = {id(obj) for obj in self._inserted_objects or ()}
        return [obj for obj in given_objects if id(obj) in inserted_object_ids]

    def _record_written(self, snapshots: list[InstanceSnapshot], stamped_values: InstanceValues) -> None:
        """Publish only objects known written; skipped conflicts keep their dirty state.

        Persistence snapshots were registered before any returned field could be set.
        A caller's loaded object keeps its original saved state on rollback, just as
        an unsaved object becomes unsaved again. Bulk reporting remains a single event.
        """
        written = self._objects if self._inserted_objects is None else self._inserted_objects
        if self.model._meta.recomputed_db_fields and not self._returning:
            ReturnedValues.leave_unloaded(
                self.model, written, self.model._meta.recomputed_db_fields, connection=self._connection
            )
        written_ids = {id(obj) for obj in written}
        stamped_values.restore(obj for obj in self._objects if id(obj) not in written_ids)
        stamped_values.keep(self._connection, written)
        outcome = WriteOutcome(RowOperation.INSERT, None)
        for snapshot in snapshots:
            if id(snapshot.instance) in written_ids:
                WriteFinalizer.publish_saved(snapshot, self._connection, outcome)

    def _prepare_preview_objects(self) -> tuple[str, ...]:
        """Copy input objects and identify values that cannot be obtained by inspection.

        Automatic stamps, blind indexes and pending async defaults are not evaluated.
        A key allocated before insertion also needs execution. Unknown values may
        change default/key grouping, so no exact commands are promised for them.
        """
        self._objects = [copy(obj) for obj in self._objects]
        self._prepare_object_values()
        rules = RowWriteRules.get(self.model, WriteType.INSERT, None, BULK_CREATE_METHOD_NAME)
        deferred = set(rules.saved_attribute_names) if self._objects else set()
        for obj in self._objects:
            deferred.update(obj._await_when_save)
        key_name = self.model._meta.generated_pk_field_name
        if (
            key_name
            and self._connection.features.takes_keys_before_insert
            and any(not obj._custom_generated_pk for obj in self._objects)
        ):
            deferred.add(key_name)
        return tuple(sorted(deferred))

    def preview(self) -> WritePreview:
        """Inspect the write without executing defaults, allocating keys or changing objects.

        A deferred preview has no command list or known transaction boundary. Otherwise
        it contains the exact strategies, ordered batches and parameters bound from
        the current inputs. It does not reserve these values for a later query run.
        """
        with Registries.bind():
            query = self._get_execution_query(True)
            deferred = query._prepare_preview_objects()
            if deferred:
                return WritePreview(None, None, deferred)
            query._validate_dialect_dependent_options()
            query._writer = InstanceWriter(query.model, query._connection)
            prepared = PreparedBulkInsert(query)
            return WritePreview(prepared.bound.commands, prepared.bound.requires_transaction)

    def _get_statements(self, parameters_inline: bool) -> list[tuple[str, list[Any]]]:
        """Render actual bound batches, or refuse to invent values that need execution."""
        deferred = self._prepare_preview_objects()
        if deferred:
            raise QueryError(BULK_INSERT_DEFERRED_PREVIEW_MESSAGE.format(fields=", ".join(deferred)))
        self._validate_dialect_dependent_options()
        self._writer = InstanceWriter(self.model, self._connection)
        prepared = PreparedBulkInsert(self, parameters_inline=parameters_inline)
        statements: list[tuple[str, list[Any]]] = []
        for batch in prepared.batches:
            command = batch.command
            if isinstance(command, WriteCopy):
                raise UnSupportedError(BULK_INSERT_COPY_PREVIEW_MESSAGE)
            if parameters_inline:
                statements.extend((sql, []) for sql in batch.inline_statements or (command.sql,))
            elif isinstance(command, WriteMany):
                statements.extend((command.sql, row) for row in command.rows)
            else:
                statements.append((command.sql, command.parameters))
        return statements
