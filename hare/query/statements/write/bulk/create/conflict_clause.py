from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import UnSupportedError
from hare.models.write.insert.insert_conflict import InsertConflict

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
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
        return InsertConflict(
            target_field_names=tuple(bulk_create._on_conflict or ()),
            constraint_name=bulk_create._on_conflict_constraint,
            condition_sql=(
                None
                if bulk_create._conflict_where is None
                else ConstraintCondition.get_sql(
                    bulk_create._conflict_where, bulk_create.model, bulk_create._connection
                )
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
        conflict_field_names = bulk_create.get_db_field_names(
            bulk_create.model, bulk_create._on_conflict, "on_conflict"
        )
        if set(conflict_field_names) != set(bulk_create.model._meta.primary_key_attribute_names):
            raise UnSupportedError(
                f"bulk_create(on_conflict={list(bulk_create._on_conflict)!r}) is not supported by the {dialect} "
                "dialect - it has no unique constraints, so only the primary key can conflict"
            )

    @staticmethod
    def get_conflict_update_key_field_names(bulk_create: BulkCreateQuery[Any]) -> list[str]:
        """The conflict target's field names of an ``ON CONFLICT ... DO UPDATE``.

        Args:
            bulk_create: The bulk insert.

        Returns:
            The field names, empty without ``update_fields`` or when a named constraint isn't
            one of the model's own ``UniqueConstraint``s.
        """
        if not bulk_create._update_fields:
            return []
        if bulk_create._on_conflict:
            return list(bulk_create._on_conflict)
        for constraint in bulk_create.model._meta.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.name == bulk_create._on_conflict_constraint:
                return bulk_create.get_db_field_names(bulk_create.model, constraint.fields, "on_conflict_constraint")
        return []

    @staticmethod
    def split_by_conflict_key(bulk_create: BulkCreateQuery[Any], objects: list[TModel]) -> list[list[TModel]]:
        """Splits ``objects`` into consecutive groups that never repeat a conflict key - one ``ON
        CONFLICT DO UPDATE`` can't update a row twice, so a later object with the same key goes into
        the next statement and its values win.

        Args:
            bulk_create: The bulk insert.
            objects: The objects of one multi-row INSERT, in order.

        Returns:
            The groups, in order.
        """
        key_field_names = ConflictClause.get_conflict_update_key_field_names(bulk_create)
        if not key_field_names:
            return [objects]
        groups: list[list[TModel]] = [[]]
        seen_keys: set[Any] = set()
        for obj in objects:
            key: Any = tuple(getattr(obj, field_name, None) for field_name in key_field_names)
            if any(value is None for value in key):
                # A NULL never conflicts with anything.
                key = None
            else:
                try:
                    hash(key)
                except TypeError:
                    key = repr(key)
            if key is not None and key in seen_keys:
                groups.append([])
                seen_keys = set()
            groups[-1].append(obj)
            if key is not None:
                seen_keys.add(key)
        return groups
