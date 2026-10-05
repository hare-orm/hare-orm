from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.enums import RowLockStrength
from hare.exceptions import QueryError, UnSupportedError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.scopes.row_scopes import RowScopes
from hare.sql import JoinType, Table
from hare.sql.builder.joins.join import Join
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.awaitable_query import AwaitableQuery


class RowLocks:
    """The row locks of a query - FOR UPDATE / NO KEY UPDATE / SHARE / KEY SHARE, NOWAIT, SKIP LOCKED
    and OF - as the dialect writes them."""

    @staticmethod
    def apply_select_for_update(query: AwaitableQuery[Any]) -> None:
        """Adds ``FOR UPDATE`` to the query, once every JOIN is built.

        Each ``of`` name - ``"self"``, the model's table name or a forward relation path - is
        rendered as the alias the query joins that table under, and its JOIN chain is made INNER:
        Postgres doesn't lock the nullable side of an outer join. With no ``of`` and a JOIN, only
        the base table is locked.

        Args:
            query: The query.

        Raises:
            QueryError: An ``of`` name is not a forward relation path of the model, is not joined,
                or crosses a relation that can't be INNER JOINed.
            QueryError: The query aggregates, groups, removes duplicates or computes a window
                function.
            UnSupportedError: The database has no SELECT ... FOR UPDATE.

        A database locking rows by their keys (``Features.locks_rows_by_key``) takes the same checks and
        writes no lock in the statement.
        """
        RowLocks.check_lockable(query)
        base_table_name = query._effective_basetable().get_table_name()
        # Rows locked by their keys are locked whatever JOIN reads them, a relation with no row none.
        locks_rows_by_key = query.features.locks_rows_by_key
        table_names: set[str] = set()
        inner_join_aliases: set[str] = set()
        joined_aliases = {table.get_table_name() for table in query._joined_tables}
        for name in sorted(query._select_for_update_of):
            if name in ("self", query.model._meta.db_table):
                table_names.add(base_table_name)
                continue
            alias = base_table_name
            path = ""
            path_hops: list[tuple[str, str, RelationalField[Model]]] = []
            for part, field in zip(name.split("__"), RowLocks.get_relation_path_fields(query, name), strict=True):
                path = f"{path}__{part}" if path else part
                alias = Identifiers.get_within_limit(f"{alias}__{part}")
                path_hops.append((alias, path, field))
            if alias not in joined_aliases:
                raise QueryError(
                    f"select_for_update(of=...) got {name!r}, but this query doesn't join that relation - "
                    f"add .select_related({name!r})."
                )
            if not locks_rows_by_key:
                inner_join_aliases.update(RowLocks.get_inner_join_aliases(query, name, path_hops))
            table_names.add(alias)
        if locks_rows_by_key:
            RowLocks.get_row_lock_strength(query)
            return
        if not query._select_for_update_of and query._joined_tables:
            table_names.add(base_table_name)
        if inner_join_aliases:
            inner_joins: list[Join] = []
            for join in query.query._joins:
                if isinstance(join.item, Table) and join.item.get_table_name() in inner_join_aliases:
                    join = copy(join)
                    join.how = JoinType.INNER
                inner_joins.append(join)
            query.query._joins = inner_joins
        query.query = query.query.for_update(
            query._select_for_update_nowait,
            query._select_for_update_skip_locked,
            table_names,
            RowLocks.get_row_lock_strength(query),
        )

    @staticmethod
    def get_inner_join_aliases(
        query: AwaitableQuery[Any], name: str, path_hops: list[tuple[str, str, RelationalField[Model]]]
    ) -> list[str]:
        """The aliases of the JOINs a locked relation path is read through - each made INNER: Postgres
        doesn't lock the nullable side of an outer join.

        Args:
            query: The query.
            name: The ``of`` name.
            path_hops: The alias, the path and the relation of each JOIN of the path.

        Returns:
            The aliases.

        Raises:
            QueryError: A relation of the path can't be INNER JOINed without changing the result.
        """
        for _hop_alias, hop_path, hop_field in path_hops:
            if hop_field.null:
                raise QueryError(
                    f"select_for_update(of=...) can't lock {name!r}: {hop_path!r} is a nullable relation, "
                    "so it's joined with LEFT OUTER JOIN, whose nullable side can't be locked."
                )
            ambient_condition = RowScopes.of(hop_field.related_model).get_condition(visibility=query._visibility)
            if (
                not hop_field.has_database_constraint
                or ambient_condition is not None
                or hop_path in query._select_related_extra_conditions
            ):
                raise QueryError(
                    f"select_for_update(of=...) can't lock {name!r}: {hop_path!r} has no database "
                    "constraint or carries an extra JOIN condition (soft delete, tenant, "
                    "Select(extra_condition=...)), so it can't be switched from LEFT OUTER JOIN to "
                    "INNER JOIN without changing the result."
                )
        return [hop_alias for hop_alias, _hop_path, _hop_field in path_hops]

    @staticmethod
    def check_lockable(query: AwaitableQuery[Any]) -> None:
        """Raises when the rows of a query can't be locked.

        Args:
            query: The query.

        Raises:
            UnSupportedError: The database has no SELECT ... FOR UPDATE.
            QueryError: The query aggregates, groups, removes duplicates or computes a window
                function.
        """
        if not query.features.supports_select_for_update:
            raise UnSupportedError(
                f"select_for_update() is not supported on the {query._connection.connection_alias!r} connection "
                f"({query.dialect.name}) - no row lock can be taken there. "
                "Silently ignoring the call would let a caller believe rows are locked when they "
                "aren't - remove this call instead of relying on it."
            )
        if query._has_aggregate or query.query._groupbys:
            raise QueryError(
                "select_for_update() can't be combined with an aggregate annotation or .group_by() - "
                "a grouped row stands for several table rows, so SQL can't lock it (FOR UPDATE is not "
                "allowed with GROUP BY). Lock the rows in a separate query, e.g. "
                "Model.objects.filter(pk__in=...).select_for_update(), then aggregate."
            )
        if query._distinct or query._distinct_on:
            raise QueryError(
                "select_for_update() can't be combined with .distinct() - a deduplicated row can stand for "
                "several table rows, so SQL can't lock it (FOR UPDATE is not allowed with DISTINCT). Lock "
                "the rows in a separate query, e.g. Model.objects.filter(pk__in=...).select_for_update()."
            )
        if RowMultiplication.reads_window_function(query):
            raise QueryError(
                "select_for_update() can't be combined with a window function (Window(...)) - SQL computes it "
                "over every matched row, so it can't lock them (FOR UPDATE is not allowed with window "
                "functions). Lock the rows in a separate query, e.g. "
                "Model.objects.filter(pk__in=...).select_for_update()."
            )

    @staticmethod
    def get_relation_path_fields(query: AwaitableQuery[Any], name: str) -> list[ForeignKeyFieldInstance[Any]]:
        """The relations of an ``of`` name of ``select_for_update()`` that is not the model's own table.

        Args:
            query: The query.
            name: The relation path.

        Returns:
            The relation of each part of the path.

        Raises:
            QueryError: The name is not a forward relation path of the model.
        """
        model = query.model
        fields: list[ForeignKeyFieldInstance[Any]] = []
        for part in name.split("__"):
            field = model._meta.fields_map.get(part)
            if not isinstance(field, ForeignKeyFieldInstance):
                raise QueryError(
                    f"select_for_update(of=...) got {name!r}, which is not a forward relation path of "
                    f'{query.model.__name__} - pass "self" or a select_related()-style path such as "author".'
                )
            fields.append(field)
            model = field.related_model
        return fields

    @staticmethod
    def get_row_lock_strength(query: AwaitableQuery[Any]) -> RowLockStrength | None:
        """The lock ``select_for_update()`` takes on this database - None for ``FOR UPDATE``.

        Args:
            query: The query.

        Returns:
            The strength; ``no_key=True`` on a database without ``FOR NO KEY UPDATE`` is
            ``FOR UPDATE``, the stronger lock.

        Raises:
            UnSupportedError: The database has no ``FOR SHARE``/``FOR KEY SHARE`` asked for.
        """
        strength: RowLockStrength | None = query._select_for_update_strength
        features = query.features
        if strength is RowLockStrength.NO_KEY_UPDATE:
            return strength if features.supports_select_for_no_key_update else None
        if (strength is RowLockStrength.SHARE and not features.supports_select_for_share) or (
            strength is RowLockStrength.KEY_SHARE and not features.supports_select_for_key_share
        ):
            raise UnSupportedError(
                f"select_for_update({strength}=True) has no lock on the {query.dialect.name} backend"
            )
        return strength
