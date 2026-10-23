from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.models import Model


@dataclass(slots=True)
class DeletePreview:
    """What ``Model.delete()`` would do to the database, computed without writing anything. Every count
    is a number of distinct rows, the instance itself included.

    Attributes:
        deleted: Rows physically removed, per model.
        soft_deleted: Rows marked deleted, per model - not the ones soft-deleted already.
        nulled: Rows whose FK is set to NULL (``SET_NULL``), per model.
        set_default: Rows whose FK is reset to its default (``SET_DEFAULT``), per model.
        many_to_many_through: Rows removed from auto-generated M2M through tables, by table name.
        many_to_many_through_nulled: Auto-generated through rows whose key column is set to NULL, by table
            name.
        protected_by: Rows blocking the delete through ``on_delete=PROTECT``.
        restricted_by: Rows blocking the delete through ``on_delete=RESTRICT``/``NO_ACTION``.
    """

    deleted: dict[type[Model], int] = field(default_factory=dict)
    soft_deleted: dict[type[Model], int] = field(default_factory=dict)
    nulled: dict[type[Model], int] = field(default_factory=dict)
    set_default: dict[type[Model], int] = field(default_factory=dict)
    many_to_many_through: dict[str, int] = field(default_factory=dict)
    many_to_many_through_nulled: dict[str, int] = field(default_factory=dict)
    protected_by: list[Model] = field(default_factory=list)
    restricted_by: list[Model] = field(default_factory=list)

    @property
    def can_delete(self) -> bool:
        """Whether ``delete()`` would go through - nothing protects or restricts it."""
        return not self.protected_by and not self.restricted_by

    def add_blocking_rows(self, blocking_rows: list[Model], rows: list[Any]) -> None:
        """Appends ``rows`` to ``blocking_rows``, skipping ones already listed.

        Args:
            blocking_rows: ``protected_by`` or ``restricted_by``.
            rows: Newly found blocking instances.
        """
        listed_keys = {(type(row), row.pk) for row in blocking_rows}
        for row in rows:
            key = (type(row), row.pk)
            if key not in listed_keys:
                listed_keys.add(key)
                blocking_rows.append(row)

    def add(self, other: DeletePreview) -> None:
        """Adds what ``other`` counts and lists - the preview of another tenant's schema, whose rows
        are others than these even with the same keys.

        Args:
            other: The preview.
        """
        for counts, other_counts in (
            (self.deleted, other.deleted),
            (self.soft_deleted, other.soft_deleted),
            (self.nulled, other.nulled),
            (self.set_default, other.set_default),
        ):
            for model, count in other_counts.items():
                counts[model] = counts.get(model, 0) + count
        for table_counts, other_table_counts in (
            (self.many_to_many_through, other.many_to_many_through),
            (self.many_to_many_through_nulled, other.many_to_many_through_nulled),
        ):
            for table, count in other_table_counts.items():
                table_counts[table] = table_counts.get(table, 0) + count
        self.protected_by.extend(other.protected_by)
        self.restricted_by.extend(other.restricted_by)

    def keep_tenant_schema_rows(self, tenant_schema_tables: set[str]) -> None:
        """Leaves out every row but those of ``Meta.tenant_schema`` models and of their relations'
        tables.

        Args:
            tenant_schema_tables: The relation tables kept in each tenant's schema.
        """
        for counts in (self.deleted, self.soft_deleted, self.nulled, self.set_default):
            for model in [model for model in counts if not model._meta.tenant_schema]:
                del counts[model]
        for table_counts in (self.many_to_many_through, self.many_to_many_through_nulled):
            for table in [table for table in table_counts if table not in tenant_schema_tables]:
                del table_counts[table]
        self.protected_by[:] = [row for row in self.protected_by if row._meta.tenant_schema]
        self.restricted_by[:] = [row for row in self.restricted_by if row._meta.tenant_schema]
