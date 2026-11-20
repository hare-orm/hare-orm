from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from hare.core.caching.model_cache import ModelCache
from hare.query.constants import RETURNING_OLD_COLUMN_ALIAS_PREFIX
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


class HareColumnNames:
    """The names of the columns hare selects itself next to a model's columns - ``hare app``, ``hare window
    filter 0``. Each holds a space, which no annotation, alias or field name holds; a model's column can still
    be named so (``source_field``), and then hare takes the first free one of ``<name> 2``, ``<name> 3``, ...
    regardless of letter case, each within the identifier length limit.
    """

    @staticmethod
    @ModelCache.fact()
    def get_spaced_column_names(binding: ModelBinding) -> frozenset[str]:
        """The columns of a model whose names hold a space - the only ones a name of hare's may equal.

        Args:
            binding: The model.

        Returns:
            The column names.
        """
        return frozenset(column for column in binding.fields_db_projection.values() if " " in column)

    @staticmethod
    @ModelCache.fact()
    def get_old_value_names(binding: ModelBinding) -> dict[str, str]:
        """The name each column of a model comes under as it was before a write (``RETURNING OLD``) - next to
        the columns as they are after it.

        Args:
            binding: The model.

        Returns:
            The names, by column.
        """
        # Each name taken, so two columns never come under one.
        taken_names = Identifiers(HareColumnNames.get_spaced_column_names(binding))
        old_value_names: dict[str, str] = {}
        for column in binding.fields_db_projection.values():
            name = taken_names.get_free_name(f"{RETURNING_OLD_COLUMN_ALIAS_PREFIX}{column}")
            taken_names.add(name)
            old_value_names[column] = name
        return old_value_names

    @staticmethod
    def get_free_name(name: str, bindings: Iterable[ModelBinding], taken_names: Collection[str] = ()) -> str:
        """A name of hare's no column of the models and none of ``taken_names`` holds - ``name`` itself unless
        one does.

        Args:
            name: The name.
            bindings: The bindings of models whose columns the statement selects alongside the new name.
            taken_names: Other names the statement selects next to it.

        Returns:
            The free name.
        """
        taken = taken_names
        for binding in bindings:
            # Almost always none - no set is made then.
            if spaced_column_names := HareColumnNames.get_spaced_column_names(binding):
                taken = {*taken, *spaced_column_names}
        return Identifiers(taken).get_free_name(name)
