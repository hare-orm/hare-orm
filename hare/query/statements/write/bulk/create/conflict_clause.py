from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import UnSupportedError
from hare.models.write.insert.insert_conflict import InsertConflict

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding
    from hare.models.write.bound_rows import BoundRows
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class ConflictClause:
    """The ON CONFLICT clause of a bulk insert: what a conflicting row does, its target, whether it can
    leave rows unwritten, and the groups of objects that never repeat a conflict key within one DO
    UPDATE."""

    @staticmethod
    def get_conflict(bulk_create: BulkCreateQuery[Any]) -> InsertConflict | None:
        """What a conflicting row does - None without ``ignore_conflicts``/``update_fields``.

        Args:
            bulk_create: The bulk insert.
        """
        if not (bulk_create._update_fields or bulk_create._ignore_conflicts):
            return None
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        return InsertConflict(
            target_field_names=tuple(bulk_create._on_conflict or ()),
            constraint_name=bulk_create._on_conflict_constraint,
            condition_sql=(
                None
                if bulk_create._conflict_where is None
                else ConstraintCondition.get_sql(bulk_create._conflict_where, model_binding, bulk_create._connection)
            ),
            update_field_names=tuple(bulk_create._update_fields or ()),
            update_tenants=bulk_create._conflict_update_tenants,
        )

    @staticmethod
    def may_skip_conflicting_rows(bulk_create: BulkCreateQuery[Any]) -> bool:
        """Whether an ``ON CONFLICT`` clause can leave some objects' rows unwritten - ``DO
        NOTHING``, or a ``DO UPDATE`` limited to the tenant scope's rows.

        Args:
            bulk_create: The bulk insert.
        """
        return bulk_create._ignore_conflicts or (
            bool(bulk_create._update_fields) and bulk_create._conflict_update_tenants is not None
        )

    @staticmethod
    def validate_primary_key_conflict_target(bulk_create: BulkCreateQuery[Any]) -> None:
        """Rejects a conflict target other than the primary key, on a database without unique
        constraints - the only rows that can conflict there share a primary key.

        Args:
            bulk_create: The bulk insert.

        Raises:
            UnSupportedError: ``on_conflict_constraint`` is given, or ``on_conflict`` names other
                columns than the primary key's.
        """
        dialect = bulk_create._connection.dialect
        if bulk_create._on_conflict_constraint:
            raise UnSupportedError(
                f"on_conflict_constraint is not supported by the {dialect} dialect - it has no unique constraints"
            )
        if not bulk_create._on_conflict:
            return
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        conflict_field_names = bulk_create.get_conflict_field_names(model_binding, bulk_create._on_conflict)
        if set(conflict_field_names) != set(model_binding.primary_key_attribute_names):
            raise UnSupportedError(
                f"bulk_create(on_conflict={list(bulk_create._on_conflict)!r}) is not supported by the {dialect} "
                "dialect - it has no unique constraints, so only the primary key can conflict"
            )

    @staticmethod
    def get_conflict_key_field_names(bulk_create: BulkCreateQuery[Any]) -> list[str]:
        """The declared conflict target's fields for updating or skipping conflicts.

        Args:
            bulk_create: The bulk insert.

        Returns:
            The field names, empty without conflict handling or when a named constraint isn't
            one of the model's own ``UniqueConstraint``s.
        """
        if not (bulk_create._update_fields or bulk_create._ignore_conflicts):
            return []
        if bulk_create._on_conflict:
            return list(bulk_create._on_conflict)
        for constraint in bulk_create._binding.constraints:  # type: ignore[union-attr]
            if isinstance(constraint, UniqueConstraint) and constraint.name == bulk_create._on_conflict_constraint:
                return bulk_create.get_conflict_field_names(bulk_create._binding, constraint.fields)  # type: ignore[arg-type]
        return []

    @staticmethod
    def split_by_conflict_key(bulk_create: BulkCreateQuery[Any], rows: BoundRows) -> list[slice]:
        """Keeps unknown database-chosen conflict keys in single-object statements. For ``ON
        CONFLICT DO UPDATE``, a statement cannot update a row twice, so a repeated key goes into
        the next statement and its values win.

        Args:
            bulk_create: The bulk insert.
            rows: The converted rows and their written fields, in input order.

        Returns:
            The groups, in order.
        """
        key_field_names = ConflictClause.get_conflict_key_field_names(bulk_create)
        if not key_field_names and not bulk_create._update_fields:
            return [slice(None)]
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        types = bulk_create.dialect.types
        if (
            not key_field_names
            or any(name not in rows.field_names for name in key_field_names)
            or (
                bulk_create._update_fields
                and len(rows.values) > 1
                and any(
                    types.get_comparison_key(model_binding.fields_map[name], database_only=True) is not None
                    for name in key_field_names
                )
            )
        ):
            # Each statement can update the target once. If the model does not declare
            # the key, or the database chooses or matches it, bind one row per statement.
            return [slice(index, index + 1) for index in range(len(rows.values))]
        if not bulk_create._update_fields:
            return [slice(None)]
        nulls_distinct = ConflictClause.has_distinct_nulls(bulk_create, key_field_names)
        keys = rows.get_keys(model_binding, types, key_field_names)
        groups = []
        start = 0
        seen_keys: set[Any] = set()
        for index, key in enumerate(keys):
            if nulls_distinct and any(value is None for value in key):
                continue
            if key in seen_keys:
                groups.append(slice(start, index))
                start = index
                seen_keys = set()
            seen_keys.add(key)
        groups.append(slice(start, len(keys)))
        return groups

    @staticmethod
    def has_distinct_nulls(bulk_create: BulkCreateQuery[Any], field_names: list[str]) -> bool:
        """Whether NULL keys can share an upsert statement without conflicting.

        A named target follows that constraint. Column inference can select more than one
        unique index: split NULL keys when any matching declaration compares NULLs as equal.
        For a partial index this may split more rows than necessary, while preserving order.
        """
        field_name_set: set[str] | None = None
        for constraint in bulk_create._binding.constraints:  # type: ignore[union-attr]
            if not isinstance(constraint, UniqueConstraint) or constraint.nulls_distinct is not False:
                continue
            if bulk_create._on_conflict_constraint:
                if constraint.name == bulk_create._on_conflict_constraint:
                    return False
            else:
                if field_name_set is None:
                    field_name_set = set(field_names)
                if (
                    set(bulk_create.get_conflict_field_names(bulk_create._binding, constraint.fields))  # type: ignore[arg-type]
                    == field_name_set
                ):
                    return False
        return True
