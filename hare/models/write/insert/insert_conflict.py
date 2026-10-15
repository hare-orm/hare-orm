from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from hare.query.expressions import RawSQL

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.sql.builder.queries.query_builder import QueryBuilder


@dataclass(frozen=True, slots=True)
class InsertConflict:
    """What an INSERT does with a row conflicting with an existing one - skips it
    (``ON CONFLICT DO NOTHING``), or updates the existing row (``ON CONFLICT DO UPDATE``).

    Args:
        target_field_names: The fields whose unique constraint the conflict is on - any
            constraint when empty.
        constraint_name: The constraint the conflict is on, instead of the fields.
        condition_sql: The ``WHERE`` of a partial unique index the target matches.
        update_field_names: The fields an existing row takes from the inserted one - skipped
            when empty.
        update_tenants: The tenant values whose rows an update reaches - every row when None.
    """

    target_field_names: tuple[str, ...] = ()
    constraint_name: str | None = None
    condition_sql: str | None = None
    update_field_names: tuple[str, ...] = ()
    update_tenants: tuple[Any, ...] | None = None

    def apply(self, model: type[Model], types: TypeRegistry, query: QueryBuilder) -> QueryBuilder:
        """Adds the ``ON CONFLICT`` clause to an INSERT. An updated row also bumps the optimistic lock
        field and takes the inserted row's ``auto_now`` fields.

        Args:
            model: The model.
            types: The connection's type registry.
            query: The INSERT.

        Returns:
            The INSERT with the clause.
        """
        if not self.update_field_names:
            return self._apply_target(model, query).do_nothing()
        meta = model._meta
        fields_db_projection = meta.fields_db_projection
        table = meta.basetable
        query = self._apply_target(model, query.as_(f"new_{meta.db_table}"))
        update_field_names = list(self.update_field_names)
        update_field_names.extend(
            field_name
            for field_name, field_object in meta.fields_map.items()
            if getattr(field_object, "auto_now", False)
            and field_name in fields_db_projection
            and field_name not in update_field_names
        )
        for update_field_name in update_field_names:
            query = query.do_update(fields_db_projection[update_field_name])
        if optimistic_lock_field := meta.optimistic_lock_field:
            # No old value to compare for an inserted row - the version is bumped where it is.
            version_column = fields_db_projection[optimistic_lock_field]
            query = query.do_update(version_column, table[version_column] + 1)
        if self.update_tenants is not None:
            tenant_field = cast("str", meta.tenant_field)
            tenant_column = table[fields_db_projection[tenant_field]]
            tenant_db_values = [
                types.get_db_value(meta.fields_map[tenant_field], tenant, model) for tenant in self.update_tenants
            ]
            query = query.where(
                tenant_column == tenant_db_values[0]
                if len(tenant_db_values) == 1
                else tenant_column.isin(tenant_db_values)
            )
        return query

    def _apply_target(self, model: type[Model], query: QueryBuilder) -> QueryBuilder:
        """Adds ``ON CONFLICT`` with its target - a named constraint, or columns and the
        condition of the partial unique index they match."""
        if self.constraint_name:
            return query.on_conflict_constraint(self.constraint_name)
        fields_db_projection = model._meta.fields_db_projection
        query = query.on_conflict(*(fields_db_projection[name] for name in self.target_field_names))
        if self.condition_sql:
            query = query.where(RawSQL(self.condition_sql))
        return query
