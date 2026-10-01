from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from hare.core.model_cache import ModelCache

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class WriteFields:
    """The fields a write of ``model`` sets without being asked (``WriteFields.of(model)``).

    Args:
        model: The model.
    """

    __slots__ = ("auto_now_names", "stamped_on_insert_names", "optimistic_lock_field")

    def __init__(self, model: type[Model]) -> None:
        meta = model._meta
        #: ``auto_now`` fields - stamped with the current time by every write.
        self.auto_now_names: tuple[str, ...] = tuple(
            name for name, field in meta.fields_map.items() if getattr(field, "auto_now", False)
        )
        #: ``auto_now`` and ``auto_now_add`` fields - stamped by an insert.
        self.stamped_on_insert_names: tuple[str, ...] = tuple(
            name
            for name, field in meta.fields_map.items()
            if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)
        )
        #: ``Meta.optimistic_lock_field`` - bumped by every update.
        self.optimistic_lock_field: str | None = meta.optimistic_lock_field

    @staticmethod
    @ModelCache.fact()
    def of(model: type[Model]) -> WriteFields:
        """The write fields of ``model``.

        Args:
            model: The model.

        Returns:
            The fields.
        """
        return WriteFields(model)

    def get_written_with(self, field_names: Iterable[str]) -> set[str]:
        """The fields an update of ``field_names`` writes: those, every ``auto_now`` field and the
        optimistic lock field.

        Args:
            field_names: The fields the update was asked to write.

        Returns:
            The field names.
        """
        written = {*field_names, *self.auto_now_names}
        if self.optimistic_lock_field:
            written.add(self.optimistic_lock_field)
        return written
