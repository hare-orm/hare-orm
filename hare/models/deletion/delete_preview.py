from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
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
        m2m_through: Rows removed from auto-generated M2M through tables, by table name.
        m2m_through_nulled: Auto-generated through rows whose key column is set to NULL, by table
            name.
        protected_by: Rows blocking the delete through ``on_delete=PROTECT``.
        restricted_by: Rows blocking the delete through ``on_delete=RESTRICT``/``NO_ACTION``.
    """

    deleted: dict[type[Model], int] = field(default_factory=dict)
    soft_deleted: dict[type[Model], int] = field(default_factory=dict)
    nulled: dict[type[Model], int] = field(default_factory=dict)
    set_default: dict[type[Model], int] = field(default_factory=dict)
    m2m_through: dict[str, int] = field(default_factory=dict)
    m2m_through_nulled: dict[str, int] = field(default_factory=dict)
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
