from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.core.connections import Connections
from hare.dialects.identifiers import Identifiers
from hare.exceptions import (
    IntegrityError,
    QueryError,
    ValidationError,
)
from hare.fields.base.field import Field
from hare.fields.constants import M2M_WRITE_BIND_PARAMS_HEADROOM
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.enums import RelationType
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.write.write_steps import WriteSteps
from hare.query.composite import KeyColumns
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.queries.tables.table import Table
from hare.sql.terms.star import Star
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
    from hare.models import Model
    from hare.sql.terms.criteria.criterion import Criterion
    from hare.sql.terms.field import Field as SqlField

TModel = TypeVar("TModel", bound="Model")


class ManyToManyRelation(RelatedQuerySet[TModel]):
    """
    The relation of a ``ManyToManyField``.
    """

    __slots__ = ("field", "_rollback_reset_layers")

    def __init__(self, instance: Model, m2m_field: ManyToManyFieldInstance[TModel]) -> None:
        super().__init__(m2m_field.related_model, (m2m_field.related_name,), instance, ("pk",))
        self.field = m2m_field
        self._rollback_reset_layers: list[dict[str, Any]] = []

    def __getstate__(self) -> dict[str, Any]:
        """Leaves out the live connections ``_reset_cache_on_rollback`` keeps, too - they belong
        to transactions still open in this process."""
        state = super().__getstate__()
        state["field"] = self.field
        state["_rollback_reset_layers"] = []
        return state

    def _reset_cache_on_rollback(self, db: DatabaseClient) -> None:
        """Registers a reset of this relation's fetched rows if the transaction or savepoint ``db`` is
        in rolls back - one registration per open savepoint span. A no-op outside a transaction.
        """
        from hare.dialects.base.client.transaction_client import TransactionClient
        from hare.dialects.base.savepoint_span import current_savepoint_span
        from hare.transactions.atomic import Atomic
        from hare.transactions.transactions import Transactions

        if not isinstance(db, TransactionClient):
            return
        registered_on = Atomic.get_connection(db.connection_name)
        if not isinstance(registered_on, TransactionClient):
            return
        current_span = current_savepoint_span.get()
        while self._rollback_reset_layers and self._rollback_reset_layers[-1]["client"]._finalized:
            self._rollback_reset_layers.pop()
        if self._rollback_reset_layers and self._rollback_reset_layers[-1]["span"] is current_span:
            return
        Transactions.on_rollback(self._reset_cache, using=db.connection_name)
        self._rollback_reset_layers.append({"span": current_span, "client": registered_on})

    def _reset_cache(self) -> None:
        self._invalidate_local_cache()

    def _through_table_db(self, for_write: bool) -> DatabaseClient:
        """The connection the through table lives on: that of the model declaring the forward side of
        the field - the instance's model from the forward side, the related model from the backward
        one.
        """
        owning_model = self.model if self.field._generated else type(self.instance)
        return self.instance._get_connection_for_instance(for_write, model=owning_model)

    def _get_visible_target_criterion(
        self,
        through_table: Table,
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> Criterion | None:
        """Builds an ``EXISTS`` condition on ``through_table`` that holds only for rows linking to a
        related row the related model's default scope (soft-delete/tenant/``Meta.manager`` filters)
        shows, so ``clear()``/``set()`` leave links to other tenants' or hidden rows untouched.

        Args:
            through_table: The through table the condition is built against.
            visibility: Which rows of the related model its default scope shows.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on; None when compiling for no particular
                database.

        Returns:
            The condition, or ``None`` if the related model has no default scope.

        Raises:
            QueryError: If the related model has ``Meta.tenant_field`` set and no tenant is
                active.
        """
        from hare.query.expressions.exists_term import ExistsTerm
        from hare.query.scopes.row_scopes import RowScopes

        related_meta = self.model._meta
        target_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{related_meta.db_table}__visible_target")
        )
        visible_target_criterion = RowScopes.of(self.model).get_criterion(
            target_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if visible_target_criterion is None:
            return None
        link_criterion = KeyColumns.row_equality(
            [through_table[column] for column in self.field.forward_keys],
            [target_table[column] for column in KeyColumns.get_source_columns(related_meta)],
        )
        return ExistsTerm(
            QueryBuilder().from_(target_table).select(Star()).where(link_criterion & visible_target_criterion)
        )

    async def _check_tenant_scope(self, instances: tuple[TModel, ...] | None, db: DatabaseClient) -> None:
        """Verifies ``self.instance`` and every one of ``instances`` belong to the active tenant, on
        each side of the relation that has ``Meta.tenant_field``.

        Args:
            instances: The related instances added or removed - None for ``clear()``.
            db: The connection the write runs on.
        """
        owning_tenant_field = type(self.instance)._meta.tenant_field
        if owning_tenant_field:
            await self._check_instance_tenant(self.instance, owning_tenant_field, db)
        if not instances:
            return
        related_tenant_field = type(instances[0])._meta.tenant_field
        if related_tenant_field:
            for instance in instances:
                await self._check_instance_tenant(instance, related_tenant_field, db)

    async def _check_visible_scope(self, instances: tuple[TModel, ...], db: DatabaseClient) -> None:
        """Verifies, against the database, that ``self.instance`` isn't soft-deleted and that the
        related model's default scope shows every one of ``instances`` - an instance in hand may be
        stale or built by hand.

        Args:
            instances: The related instances being added.
            db: Connection to read on.

        Raises:
            IntegrityError: ``self.instance`` is soft-deleted, or one of ``instances`` is hidden.
        """
        from hare.query.scopes.row_scopes import RowScopes

        owning_meta = type(self.instance)._meta
        if owning_meta.soft_delete_field:
            owning_table = owning_meta.basetable
            soft_delete_column = owning_meta.fields_db_projection[owning_meta.soft_delete_field]
            query = (
                db.query_class.from_(owning_table)
                .where(
                    KeyColumns.row_equality(
                        [owning_table[column] for column in KeyColumns.get_source_columns(owning_meta)],
                        KeyColumns.get_db_values(owning_meta, self.instance, db.dialect.types),
                    )
                    & owning_table[soft_delete_column].isnull()
                )
                .select(*KeyColumns.get_source_columns(owning_meta))
            )
            _, rows = await db.execute(*query.get_parameterized_sql())
            if not rows:
                raise IntegrityError(
                    f"{type(self.instance).__name__} instance (pk={self.instance.pk!r}) is soft-deleted - "
                    "can't add a relation from it."
                )
        related_meta = self.model._meta
        related_table = related_meta.basetable
        visible_criterion = RowScopes.of(self.model).get_criterion(related_table, dialect=db.dialect, connection=db)
        if visible_criterion is None:
            return
        pk_columns = KeyColumns.get_source_columns(related_meta)
        instances_by_pk_values = {
            KeyColumns.get_db_values(related_meta, instance, db.dialect.types): instance for instance in instances
        }
        query = (
            db.query_class.from_(related_table)
            .where(
                db.dialect.filter_operators.get_row_membership_criterion(
                    [related_table[column] for column in pk_columns],
                    list(instances_by_pk_values),
                    self._get_forward_key_fields(),
                )
                & visible_criterion
            )
            .select(*pk_columns)
        )
        _, rows = await db.execute(*query.get_parameterized_sql())
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        visible_pk_values = {
            tuple(
                db.dialect.types.get_db_value(pk_field, row[column], self.model)
                for pk_field, column in zip(related_pk_fields, pk_columns, strict=True)
            )
            for row in rows
        }
        for pk_values, instance in instances_by_pk_values.items():
            if pk_values not in visible_pk_values:
                raise IntegrityError(
                    f"{type(instance).__name__} instance (pk={instance.pk!r}) is not shown by "
                    f"{type(instance).__name__}'s default scope (soft-deleted, or filtered out by its "
                    "Meta.manager) - can't add it to this relation."
                )

    @staticmethod
    async def _check_instance_tenant(instance: Model, tenant_field: str, db: DatabaseClient) -> None:
        # Local import: hare.models (Tenancy's own package) imports back from hare.fields.
        # relational at package-init time - importing it back at module level here would be
        # circular (same reasoning as QuerySet.get_distinct()'s own local Manager import).
        from hare.models.tenancy import Tenancy

        active_tenant = Tenancy.get_scope(type(instance))
        if active_tenant is None:
            raise QueryError(
                f"{type(instance).__name__} has Meta.tenant_field '{tenant_field}' set but no "
                "tenant is active - wrap this call in Tenancy.scope(...)."
            )
        if active_tenant is Tenancy.ALL:
            return
        # The tenant is read from the database by primary key - an instance built by hand can claim
        # any tenant.
        meta = type(instance)._meta
        table = meta.basetable
        tenant_db_column = meta.fields_db_projection[tenant_field]
        pk_columns = [table[column] for column in KeyColumns.get_source_columns(meta)]
        condition = KeyColumns.row_equality(pk_columns, KeyColumns.get_db_values(meta, instance, db.dialect.types))
        query = db.query_class.from_(table).where(condition).select(tenant_db_column)
        _, rows = await db.execute(*query.get_parameterized_sql())
        stored_tenant = (
            db.dialect.types.get_python_value(meta.fields_map[tenant_field], rows[0][tenant_db_column])
            if rows
            else None
        )
        if not rows or not Tenancy.allows(type(instance), active_tenant, stored_tenant):
            raise QueryError(
                f"{type(instance).__name__} instance (pk={instance.pk!r}) belongs to a tenant "
                f"other than the active one ({active_tenant!r}) according to the database."
            )

    def _validate_owner_saved(self) -> None:
        """Checks the instance owning this relation is saved.

        Raises:
            QueryError: ``self.instance`` is not saved yet.
        """
        if not self.instance._saved_in_db:
            raise QueryError(f"You should first call .save() on {self.instance!r}")

    def _validate_related_instances(self, instances: tuple[Any, ...]) -> None:
        """Checks every one of ``instances`` is a saved instance of this relation's related model.

        Raises:
            ValidationError: An instance is not of the related model's type.
            QueryError: An instance is not saved yet.
        """
        for instance in instances:
            if type(instance) is not self.model:
                raise ValidationError(
                    f"Invalid instance for relationship field '{self.field.model_field_name}'. "
                    f"Expected model type '{self.model.__name__}', but got '{type(instance).__name__}'."
                )
            if not instance._saved_in_db:
                raise QueryError(f"You should first call .save() on {instance!r}")

    async def add(
        self,
        *instances: Any,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """Adds related instances, or their primary key values (a tuple for a composite key), to the
        relation. A pair already linked is left as is; one whose through row was soft-deleted gets a
        new row.

        Args:
            instances: Related instances or their primary key values.
            through_defaults: Values for the extra fields of a ``through=Model`` model, by field
                name - applied to the rows this call inserts.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: An object isn't saved; ``through_defaults`` doesn't fit the through model;
                or a tenant-scoped side has no active tenant or belongs to another one.
            ValidationError: One of ``instances`` is not an instance of the related model.
            IntegrityError: ``self.instance`` is soft-deleted, or the related model's default scope
                doesn't show one of ``instances``.
        """
        if not instances:
            return
        related_instances = await self._add_links(instances, through_defaults, using)
        if ChangeEvents.is_observed():
            await self._report_links(
                Connections.get_client(using) or self._through_table_db(for_write=True),
                related_instances,
                RowOperation.INSERT,
            )

    async def _add_links(
        self, instances: tuple[Any, ...], through_defaults: dict[str, Any] | None, using: str | DatabaseClient | None
    ) -> list[Any]:
        """Everything ``add()`` does but reporting the change.

        Args:
            instances: Related instances or their primary key values.
            through_defaults: See ``add()``.
            using: See ``add()``.

        Returns:
            The related instances.
        """
        self._validate_owner_saved()
        instances = await self._get_related_instances(instances, Connections.get_client(using))
        self._validate_related_instances(instances)
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        self._reset_cache_on_rollback(db)
        pk_b_values = KeyColumns.get_db_values(type(self.instance)._meta, self.instance, db.dialect.types)
        related_meta = type(instances[0])._meta
        await self._check_tenant_scope(instances, db)
        await self._check_visible_scope(instances, db)
        # The fetched rows are dropped up front - every exit below then leaves no stale snapshot.
        self._invalidate_local_cache()
        pks_f: list[tuple[Any, ...]] = [
            KeyColumns.get_db_values(related_meta, instance_to_add, db.dialect.types) for instance_to_add in instances
        ]
        through_table = Table(self.field.through, schema=self.field.through_schema)
        backward_columns = [through_table[c] for c in self.field.backward_keys]
        forward_columns = [through_table[c] for c in self.field.forward_keys]
        through_defaults = self._through_defaults_with_active_tenant(through_defaults)
        extra_columns, extra_values = self._through_defaults_columns_and_values(through_defaults, db.dialect.types)

        if self.field.unique and self.field.through_model is None and db.dialect.supports_unique_constraints:
            # The through table's unique index lets one INSERT ... ON CONFLICT DO NOTHING skip the
            # pairs already linked. Not for a through=Model table, which has no such index of
            # hare's, nor on a database without unique constraints.
            unique_pks_f = set(pks_f)
            if len(unique_pks_f) == 1:
                # The single-row statement of .add(one_instance) is the same on every call -
                # rendered once per field and dialect.
                statement_key = (self.field.model, self.field.model_field_name, db.query_class)
                cached_sql = StatementPlans.m2m_add_statements.get(statement_key)
                if cached_sql is None:
                    query = db.query_class.into(through_table).columns(*forward_columns, *backward_columns)
                    placeholder_row = (0,) * (len(forward_columns) + len(backward_columns))
                    query = query.insert(*placeholder_row)  # placeholder values - only the SQL text is kept
                    query = query.on_conflict(*backward_columns, *forward_columns).do_nothing()
                    cached_sql, _ = query.get_parameterized_sql()
                    StatementPlans.m2m_add_statements[statement_key] = cached_sql
                (single_pk_f,) = unique_pks_f
                await db.execute(cached_sql, [*single_pk_f, *pk_b_values])
                return list(instances)
            rows = [(*pk_f, *pk_b_values) for pk_f in unique_pks_f]
            await self._insert_through_rows(
                db,
                through_table,
                [*forward_columns, *backward_columns],
                rows,
                conflict_target_columns=[*backward_columns, *forward_columns],
            )
            return list(instances)

        select_query = db.query_class.from_(through_table).where(
            KeyColumns.row_equality(backward_columns, pk_b_values)
        )
        select_query = select_query.select(*self.field.forward_keys)
        criterion = (
            KeyColumns.row_equality(forward_columns, pks_f[0])
            if len(pks_f) == 1
            else db.dialect.filter_operators.get_row_membership_criterion(
                forward_columns, pks_f, self._get_forward_key_fields()
            )
        )
        select_query = select_query.where(criterion)
        through_model = self.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            # A soft-deleted through row is no active link - the pair is added again.
            soft_delete_column = through_model._meta.fields_map[soft_delete_field].source_field or soft_delete_field
            select_query = select_query.where(through_table[soft_delete_column].isnull())

        _, already_existing_relations_raw = await db.execute(*select_query.get_parameterized_sql())
        # The related model's key fields convert a related instance's values - instances[0], not
        # self.instance.
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        already_existing_forward_pks = {
            tuple(
                db.dialect.types.get_db_value(field, db.dialect.types.get_python_value(field, r[col]), instances[0])
                for field, col in zip(related_pk_fields, self.field.forward_keys, strict=True)
            )
            for r in already_existing_relations_raw
        }

        if pks_f_to_insert := set(pks_f) - already_existing_forward_pks:
            default_fields = self._through_model_default_fields(through_defaults)
            default_columns = [through_table[column_name] for column_name, _ in default_fields]
            rows = [
                (
                    *pk_f,
                    *pk_b_values,
                    *extra_values,
                    *await self._through_model_default_values(default_fields, db.dialect.types),
                )
                for pk_f in pks_f_to_insert
            ]
            await self._insert_through_rows(
                db, through_table, [*forward_columns, *backward_columns, *extra_columns, *default_columns], rows
            )
        return list(instances)

    async def _report_links(
        self, db: DatabaseClient, related_instances: Sequence[Any] | None, operation: RowOperation
    ) -> None:
        """Reports links added or removed (``ChangeEvents``): the owner's and the related rows'
        relation changed, and the rows of a through model were inserted or deleted.

        Args:
            db: The connection the links were written on.
            related_instances: The related rows, None for every one.
            operation: ``INSERT`` for added links, ``DELETE`` for removed ones.
        """
        await WriteSteps.report(
            db,
            type(self.instance),
            RowOperation.UPDATE,
            instances=[self.instance],
            fields=[self.field.model_field_name],
        )
        related_fields = [self.field.related_name] if self.field.related_name else None
        if related_instances is None:
            await WriteSteps.report(db, self.model, RowOperation.UPDATE, fields=related_fields)
        else:
            await WriteSteps.report(
                db, self.model, RowOperation.UPDATE, instances=list(related_instances), fields=related_fields
            )
        through_model = self.field.through_model_class
        if through_model is not None:
            if operation is RowOperation.DELETE and through_model._meta.soft_delete_field:
                await WriteSteps.report(
                    db, through_model, RowOperation.UPDATE, fields=[through_model._meta.soft_delete_field]
                )
            else:
                await WriteSteps.report(db, through_model, operation)

    def _get_forward_key_fields(self) -> list[Field[Any] | None]:
        """The field of each forward key column - the related model's primary key field(s) - for
        binding a long list of their values.

        Returns:
            One field per forward key column.
        """
        related_meta = self.model._meta
        return list(related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,))

    @staticmethod
    async def _insert_through_rows(
        db: DatabaseClient,
        through_table: Table,
        columns: list[SqlField],
        rows: list[tuple[Any, ...]],
        conflict_target_columns: list[SqlField] | None = None,
    ) -> None:
        """Inserts through-table rows in as few multi-row INSERTs as the backend's bind-parameter
        ceiling allows - in one transaction when it takes more than one.

        Args:
            db: Connection to write on.
            through_table: The through table.
            columns: The inserted columns, in row order.
            rows: One value tuple per inserted row.
            conflict_target_columns: The ``ON CONFLICT DO NOTHING`` target, or None to insert
                without a conflict clause.
        """
        rows_per_statement = max(1, (db.features.max_bind_parameters - M2M_WRITE_BIND_PARAMS_HEADROOM) // len(columns))
        statements = []
        for start in range(0, len(rows), rows_per_statement):
            query = db.query_class.into(through_table).columns(*columns)
            for row in rows[start : start + rows_per_statement]:
                query = query.insert(*row)
            if conflict_target_columns is not None:
                query = query.on_conflict(*conflict_target_columns).do_nothing()
            statements.append(query.get_parameterized_sql())
        if len(statements) == 1:
            await db.execute(*statements[0])
            return
        async with db._in_transaction() as transaction_db:
            for sql, values in statements:
                await transaction_db.execute(sql, values)

    def _through_defaults_with_active_tenant(self, through_defaults: dict[str, Any] | None) -> dict[str, Any] | None:
        """Adds the through model's one tenant to ``through_defaults`` when it has
        ``Meta.tenant_field`` set and the caller didn't give it.

        Raises:
            QueryError: ``through_defaults`` gives a tenant outside the through model's scope, or
                gives none under a scope of several values.
        """
        # Local import: same package-init cycle as _check_instance_tenant().
        from hare.models.tenancy import Tenancy

        through_model = self.field.through_model_class
        tenant_field = through_model._meta.tenant_field if through_model is not None else None
        if tenant_field is None:
            return through_defaults
        through_model = cast("type[Model]", through_model)
        active_tenant = Tenancy.get_scope(through_model)
        if active_tenant is None:
            return through_defaults
        if through_defaults and tenant_field in through_defaults:
            if not Tenancy.allows(through_model, active_tenant, through_defaults[tenant_field]):
                raise QueryError(
                    f"through_defaults sets '{tenant_field}'={through_defaults[tenant_field]!r}, which "
                    f"differs from the active tenant ({active_tenant!r})."
                )
            return through_defaults
        if not Tenancy.has_one_value(active_tenant):
            raise QueryError(
                f"{through_model.__name__} rows need '{tenant_field}' and the active tenant scope "
                f"({active_tenant!r}) has no single value to give them - name it in through_defaults"
            )
        return {**(through_defaults or {}), tenant_field: active_tenant}

    def _through_model_default_fields(self, through_defaults: dict[str, Any] | None) -> list[tuple[str, Field[Any]]]:
        """The through model's fields filled from a Python-side default - ``default=``,
        ``auto_now``/``auto_now_add`` - as ``(column name, field)`` pairs: ``add()`` inserts through
        rows with a raw INSERT. A ``db_default`` is left to the database.
        """
        through_model = self.field.through_model_class
        if through_model is None:
            return []
        relation_columns = set(self.field.forward_keys) | set(self.field.backward_keys)
        provided = set(through_defaults or ())
        default_fields: list[tuple[str, Field[Any]]] = []
        for field_name, field_object in through_model._meta.fields_map.items():
            column_name = field_object.source_field or field_name
            if (
                not field_object.has_db_field
                or field_object.generated
                or column_name in relation_columns
                or field_name in provided
            ):
                continue
            if (
                field_object.default is not None
                or field_object._default_is_coroutine
                or getattr(field_object, "auto_now", False)
                or getattr(field_object, "auto_now_add", False)
            ):
                default_fields.append((column_name, field_object))
        return default_fields

    async def _through_model_default_values(
        self, default_fields: list[tuple[str, Field[Any]]], types: TypeRegistry
    ) -> list[Any]:
        """One through row's DB-ready values for ``_through_model_default_fields()``'s fields,
        computed per row (a callable default such as ``uuid4`` must produce a fresh value for each
        inserted row) by letting a real, never-saved through-model instance apply its own defaults.
        """
        if not default_fields:
            return []
        through_model = self.field.through_model_class
        assert through_model is not None  # nosec B101
        row_instance = through_model()
        for field_name, awaitable_default in row_instance._await_when_save.items():
            setattr(row_instance, field_name, await awaitable_default())
        return [
            types.get_db_value(field_object, getattr(row_instance, field_object.model_field_name), row_instance)
            for _, field_object in default_fields
        ]

    def _through_defaults_columns_and_values(
        self, through_defaults: dict[str, Any] | None, types: TypeRegistry
    ) -> tuple[list[SqlField], list[Any]]:
        """Resolves ``through_defaults`` into the through table's own extra ``Column`` objects and
        their already-DB-converted values, ready to append onto ``.add()``'s own INSERT column
        list/values.

        Raises:
            QueryError: ``through_defaults`` names a field that doesn't exist on the
                through model, one of the through model's own FK fields making up the relation
                itself, or a ``GeneratedField`` (computed by the database, not written to).
        """
        if not through_defaults:
            return [], []
        through_model = self.field.through_model_class
        if through_model is None:
            raise QueryError(
                f"through_defaults isn't supported on '{self.field.model_field_name}' - it has no "
                "real through model (through=Model) to hold extra fields."
            )
        through_table = Table(self.field.through, schema=self.field.through_schema)
        relation_columns = set(self.field.forward_keys) | set(self.field.backward_keys)
        columns = []
        values = []
        for field_name, value in through_defaults.items():
            field_object = through_model._meta.fields_map.get(field_name)
            if field_object is None:
                raise QueryError(
                    f"through_defaults names '{field_name}', which isn't a field on through model "
                    f"'{through_model.__name__}'."
                )
            column_name = field_object.source_field or field_object.model_field_name
            field_column_names = (
                set(cast("ForeignKeyFieldInstance[Any]", field_object).db_column_names)
                if field_object.relation_type in (RelationType.FOREIGN_KEY, RelationType.ONE_TO_ONE)
                else {column_name}
            )
            if field_column_names & relation_columns:
                raise QueryError(
                    f"through_defaults cannot set '{field_name}' - it's one of the through model's "
                    "own FK fields making up the relation itself."
                )
            if field_object.generated:
                raise QueryError(
                    f"through_defaults cannot set '{field_name}' - it's a generated field, "
                    "computed by the database, not written to."
                )
            columns.append(through_table[column_name])
            values.append(types.get_db_value(field_object, value, through_model))
        return columns, values

    async def clear(self, using: str | DatabaseClient | None = None, *, all_tenants: bool = False) -> None:
        """Clears every link of the relation. The rows of a ``through=Model`` model with
        ``Meta.soft_delete_field`` are soft-deleted instead.

        Args:
            using: Specific DB connection to use instead of default bound.
            all_tenants: Clear the links to rows of every tenant - by default only those to the
                active tenant's rows.
        """
        await self._remove_or_clear(
            using=using, check_tenant_scope=not all_tenants, visibility=RowVisibility(all_tenants=all_tenants)
        )

    async def remove(self, *instances: Any, using: str | DatabaseClient | None = None) -> None:
        """Removes related instances, or their primary key values, from the relation - every through
        row of each pair. A link named by a primary key value is removed only when the related
        model's default scope shows its row. The rows of a soft-delete through model are
        soft-deleted.

        Raises:
            QueryError: No instances were given, or ``self.instance`` or one of ``instances`` is not
                saved.
            ValidationError: One of ``instances`` is not an instance of the related model.
        """
        if not instances:
            raise QueryError("remove() called on no instances")
        related_instances = tuple(member for member in instances if self._is_model_instance(member))
        if len(related_instances) == len(instances):
            await self._remove_or_clear(related_instances, using)
            return
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        forward_pk_values = [
            self._get_member_pk_db_values(member, db.dialect.types)
            for member in instances
            if not self._is_model_instance(member)
        ]
        async with db._in_transaction() as transaction_db:
            if related_instances:
                await self._remove_or_clear(related_instances, transaction_db)
            await self._remove_or_clear(using=transaction_db, forward_pk_values=forward_pk_values)

    async def create(
        self,
        *,
        using: str | DatabaseClient | None = None,
        through_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> TModel:
        """
        Creates an object of the related model and adds it to the relation, in one transaction.

        Args:
            using: Specific DB connection to use instead of default bound.
            through_defaults: Passed on to ``add()``.
            kwargs: Model parameters for the new object.

        Returns:
            The created object.

        Raises:
            QueryError: ``self.instance`` is not saved.
        """
        self._validate_owner_saved()
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        async with db._in_transaction() as transaction_db:
            created_instance = await self._get_model_queryset(transaction_db).create(**kwargs)
            await self.add(created_instance, through_defaults=through_defaults, using=transaction_db)
        return created_instance

    async def set(
        self,
        *instances: Any,
        through_defaults: dict[str, Any] | None = None,
        clear: bool = False,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """Replaces the relation's members with ``instances``, in one transaction: removes the missing
        members and adds the new ones; the through rows of members that stay are untouched.

        Args:
            instances: Related instances or their primary key values - as arguments, or one iterable
                or queryset.
            through_defaults: Passed on to ``add()`` for the added members.
            clear: Clear the whole relation first and add every one of ``instances`` afresh.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: ``self.instance`` or one of ``instances`` is not saved.
            ValidationError: One of ``instances`` is not an instance of the related model.
        """
        members = await self._get_set_members(instances)
        self._validate_owner_saved()
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        members_by_pk_values = {self._get_member_pk_db_values(member, db.dialect.types): member for member in members}
        async with db._in_transaction() as transaction_db:
            if clear or not members:
                await self.clear(using=transaction_db)
                if members:
                    await self.add(*members, through_defaults=through_defaults, using=transaction_db)
                return
            linked_pk_values = await self._get_linked_pk_values(transaction_db)
            if unlinked_pk_values := [
                pk_values for pk_values in linked_pk_values if pk_values not in members_by_pk_values
            ]:
                await self._remove_or_clear(using=transaction_db, forward_pk_values=unlinked_pk_values)
            if new_members := [
                member for pk_values, member in members_by_pk_values.items() if pk_values not in linked_pk_values
            ]:
                await self.add(*new_members, through_defaults=through_defaults, using=transaction_db)

    async def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        *,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]:
        """
        Fetches the member matching ``kwargs``, else creates an object of the related model and
        adds it to the relation, in one transaction - like Django.

        Args:
            defaults: Values for a created object, on top of ``kwargs``' exact values.
            through_defaults: Passed on to ``add()``.
            using: Specific DB connection to use instead of default bound.
            kwargs: Query parameters.

        Raises:
            QueryError: ``self.instance`` is not saved.
            MultipleObjectsReturned: More than one member matches ``kwargs``.
            QueryError: ``defaults`` conflicts with ``kwargs``.
        """
        self._validate_owner_saved()
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        async with db._in_transaction() as transaction_db:
            if (member := await self.filter(**kwargs).using(transaction_db).get_or_none()) is not None:
                return member, False
            return (
                await self.create(
                    using=transaction_db,
                    through_defaults=through_defaults,
                    **self._get_create_values(defaults or {}, kwargs),
                ),
                True,
            )

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        *,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]:
        """
        Updates the member matching ``kwargs`` with ``defaults``, else creates an object of the
        related model and adds it to the relation, in one transaction - like Django.

        Args:
            defaults: Values to update the member with, or for a created object on top of
                ``kwargs``' exact values when ``create_defaults`` isn't given.
            through_defaults: Passed on to ``add()``.
            using: Specific DB connection to use instead of default bound.
            create_defaults: Values for a created object instead of ``defaults``.
            kwargs: Query parameters.

        Raises:
            QueryError: ``self.instance`` is not saved.
            MultipleObjectsReturned: More than one member matches ``kwargs``.
            QueryError: ``defaults`` conflicts with ``kwargs``.
        """
        self._validate_owner_saved()
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        async with db._in_transaction() as transaction_db:
            if (member := await self.filter(**kwargs).using(transaction_db).get_or_none()) is not None:
                updated_member, _ = await self._get_model_queryset(transaction_db).update_or_create(
                    defaults, pk=member.pk
                )
                self._invalidate_local_cache()
                return updated_member, False
            creation_values = defaults if create_defaults is None else create_defaults
            return (
                await self.create(
                    using=transaction_db,
                    through_defaults=through_defaults,
                    **self._get_create_values(creation_values or {}, kwargs),
                ),
                True,
            )

    @staticmethod
    def _is_model_instance(member: Any) -> bool:
        """Whether a member is given as a model instance, not as a primary key value."""
        from hare.models import Model

        return isinstance(member, Model)

    def _get_member_pk_db_values(self, member: Any, types: TypeRegistry) -> tuple[Any, ...]:
        """The DB-ready primary key of a member - a related instance, or its primary key value (a
        tuple for a composite key).

        Raises:
            ValidationError: ``member`` is None or an instance of another model, or a composite key value
                that isn't a tuple of one value per key field.
            QueryError: ``member`` is an unsaved instance.
        """
        related_meta = self.model._meta
        if member is None or self._is_model_instance(member):
            self._validate_related_instances((member,))
            return KeyColumns.get_db_values(related_meta, member, types)
        if not related_meta.has_composite_primary_key:
            return (types.get_db_value(related_meta.pk, member, self.model),)
        if not isinstance(member, tuple) or len(member) != len(related_meta.pk_fields):
            raise ValidationError(
                f"Invalid value for relationship field '{self.field.model_field_name}': {member!r} - "
                f"{self.model.__name__} has a composite primary key, pass an instance or a tuple of "
                f"{len(related_meta.pk_fields)} values"
            )
        return tuple(
            types.get_db_value(field, value, self.model)
            for field, value in zip(related_meta.pk_fields, member, strict=True)
        )

    async def _get_related_instances(self, members: tuple[Any, ...], db: DatabaseClient | None) -> tuple[TModel, ...]:
        """``members`` as related instances - a primary key value is fetched through the related
        model's default scope.

        Args:
            members: Related instances or their primary key values.
            db: Connection to fetch on, None for the related model's own.

        Raises:
            IntegrityError: No row the default scope shows has one of the primary key values.
        """
        if all(self._is_model_instance(member) for member in members):
            return members
        types = (db or self.model.get_connection()).dialect.types
        requested_values = {
            self._get_member_pk_db_values(member, types): member
            for member in members
            if not self._is_model_instance(member)
        }
        related_meta = self.model._meta
        fetched_instances = await (
            self.model._meta.manager.get_queryset().filter(pk__in=list(requested_values.values())).using(db)
        )
        fetched_by_pk_values = {
            KeyColumns.get_db_values(related_meta, instance, types): instance for instance in fetched_instances
        }
        if missing_values := [
            value for pk_values, value in requested_values.items() if pk_values not in fetched_by_pk_values
        ]:
            raise IntegrityError(
                f"Can't add {', '.join(repr(value) for value in missing_values)} to "
                f"'{self.field.model_field_name}' - no {self.model.__name__} with that primary key"
            )
        return tuple(
            member
            if self._is_model_instance(member)
            else fetched_by_pk_values[self._get_member_pk_db_values(member, types)]
            for member in members
        )

    async def _get_linked_pk_values(self, db: DatabaseClient) -> frozenset[tuple[Any, ...]]:
        """The DB-ready primary key values of every related row actively linked to ``self.instance``.

        Args:
            db: Connection to read the through table on.
        """
        through_table = Table(self.field.through, schema=self.field.through_schema)
        backward_columns = [through_table[column_name] for column_name in self.field.backward_keys]
        pk_b_values = KeyColumns.get_db_values(type(self.instance)._meta, self.instance, db.dialect.types)
        query = (
            db.query_class.from_(through_table)
            .where(KeyColumns.row_equality(backward_columns, pk_b_values))
            .select(*self.field.forward_keys)
        )
        through_model = self.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            soft_delete_column = through_model._meta.fields_map[soft_delete_field].source_field or soft_delete_field
            query = query.where(through_table[soft_delete_column].isnull())
        if (
            visible_target_criterion := self._get_visible_target_criterion(
                through_table, dialect=db.dialect, connection=db
            )
        ) is not None:
            query = query.where(visible_target_criterion)
        _, rows = await db.execute(*query.get_parameterized_sql())
        related_meta = self.model._meta
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        linked_pk_values: list[tuple[Any, ...]] = []
        for row in rows:
            if any(row[column_name] is None for column_name in self.field.forward_keys):
                continue
            linked_pk_values.append(
                tuple(
                    db.dialect.types.get_db_value(pk_field, row[column_name], self.model)
                    for pk_field, column_name in zip(related_pk_fields, self.field.forward_keys, strict=True)
                )
            )
        return frozenset(linked_pk_values)

    async def _remove_or_clear(
        self,
        instances: tuple[TModel, ...] | None = None,
        using: str | DatabaseClient | None = None,
        *,
        check_tenant_scope: bool = True,
        forward_pk_values: list[tuple[Any, ...]] | None = None,
        visibility: RowVisibility = RowVisibility.DEFAULT,
    ) -> None:
        """Removes the through rows linking ``self.instance`` to ``instances`` (all of them for
        ``None``) and reports the change (``ChangeEvents``) - shared by ``remove()``/``clear()``/
        ``set()`` and the ``on_delete=CASCADE`` cascade of a deleted owner.

        Args:
            instances: The related instances to unlink, or ``None`` to unlink every one.
            using: Connection to use instead of the through table's own.
            check_tenant_scope: See ``_unlink()``.
            forward_pk_values: See ``_unlink()``.
            visibility: See ``_unlink()``.
        """
        await self._unlink(
            instances,
            using,
            check_tenant_scope=check_tenant_scope,
            forward_pk_values=forward_pk_values,
            visibility=visibility,
        )
        if ChangeEvents.is_observed():
            related_instances = list(instances) if instances else None
            await self._report_links(
                Connections.get_client(using) or self._through_table_db(for_write=True),
                related_instances,
                RowOperation.DELETE,
            )

    async def _unlink(
        self,
        instances: tuple[TModel, ...] | None = None,
        using: str | DatabaseClient | None = None,
        *,
        check_tenant_scope: bool = True,
        forward_pk_values: list[tuple[Any, ...]] | None = None,
        visibility: RowVisibility = RowVisibility.DEFAULT,
    ) -> None:
        """Removes the through rows linking ``self.instance`` to ``instances`` (all of them for
        ``None``).

        Args:
            instances: The related instances to unlink, or ``None`` to unlink every one.
            using: Connection to use instead of the through table's own.
            check_tenant_scope: ``False`` for a cascade, which follows the through rows of a row
                it is deleting regardless of the active tenant scope, and for ``clear(all_tenants=True)``.
            forward_pk_values: DB-ready primary key values of the related rows to unlink, in
                place of ``instances``.
            visibility: Which related rows the related model's default scope shows when unlinking
                every row - every tenant's for ``clear(all_tenants=True)``.
        """
        self._validate_owner_saved()
        if instances:
            self._validate_related_instances(instances)
        db = Connections.get_client(using) or self._through_table_db(for_write=True)
        self._reset_cache_on_rollback(db)
        # remove()/clear() had no tenant check at all (unlike add()'s own _check_tenant_scope) -
        # nothing stopped removing/soft-deleting a through-table row linking a DIFFERENT tenant's
        # rows just because the caller happened to hold Python references to them.
        if check_tenant_scope:
            await self._check_tenant_scope(instances, db)
        # Same staleness as add() (see its own comment) - a relation already fetched onto this
        # instance kept showing removed members after remove()/clear().
        self._invalidate_local_cache()
        through_table = Table(self.field.through, schema=self.field.through_schema)
        backward_columns = [through_table[c] for c in self.field.backward_keys]
        pk_b_values = KeyColumns.get_db_values(type(self.instance)._meta, self.instance, db.dialect.types)

        condition = KeyColumns.row_equality(backward_columns, pk_b_values)
        if instances:
            related_meta = type(instances[0])._meta
            forward_pk_values = [
                KeyColumns.get_db_values(related_meta, instance, db.dialect.types) for instance in instances
            ]
        if forward_pk_values:
            forward_columns = [through_table[c] for c in self.field.forward_keys]
            condition &= (
                KeyColumns.row_equality(forward_columns, forward_pk_values[0])
                if len(forward_pk_values) == 1
                else db.dialect.filter_operators.get_row_membership_criterion(
                    forward_columns, forward_pk_values, self._get_forward_key_fields()
                )
            )
        if (check_tenant_scope or visibility.all_tenants) and not instances:
            # clear()/set() name no related rows: only the links to rows the related model's default
            # scope shows are theirs to remove. remove() names its rows, and can drop a link to a
            # hidden one.
            visible_target_criterion = self._get_visible_target_criterion(
                through_table, visibility=visibility, dialect=db.dialect, connection=db
            )
            if visible_target_criterion is not None:
                condition &= visible_target_criterion
        through_model = self.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            # A through model with Meta.soft_delete_field is soft-deleted, like any other model.
            soft_delete_field_obj = through_model._meta.fields_map[soft_delete_field]
            soft_delete_column = soft_delete_field_obj.source_field or soft_delete_field
            now = Timezone.now()
            # Only live rows - a row soft-deleted before keeps its deletion time.
            condition &= through_table[soft_delete_column].isnull()
            query = db.query_class.update(through_table).where(condition)
            query = query.set(
                soft_delete_column, db.dialect.types.get_db_value(soft_delete_field_obj, now, through_model)
            )
            # The optimistic lock field and the auto_now fields are bumped, as every other write
            # does.
            if optimistic_lock_field := through_model._meta.optimistic_lock_field:
                version_column = through_model._meta.fields_db_projection[optimistic_lock_field]
                query = query.set(version_column, through_table[version_column] + 1)
            for field_name, field_object in through_model._meta.fields_map.items():
                if (
                    isinstance(field_object, DatetimeField | TimeField)
                    and field_object.auto_now
                    and field_name != soft_delete_field
                ):
                    query = query.set(
                        through_model._meta.fields_db_projection[field_name],
                        db.dialect.types.get_db_value(
                            field_object, field_object.get_auto_now_value(now), through_model
                        ),
                    )
            await db.execute(*query.get_parameterized_sql())
            return
        query = db.query_class.from_(through_table).where(condition).delete()
        await db.execute(*query.get_parameterized_sql())


# The Python type of a many-to-many field's value - set here: this module imports the field
# modules, not the other way round.
ManyToManyFieldInstance.field_type = ManyToManyRelation
