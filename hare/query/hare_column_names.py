from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from hare.core.caching.model_cache import ModelCache
from hare.query.constants import RETURNING_OLD_COLUMN_ALIAS_PREFIX
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.models import Model


class HareColumnNames:
    """The names of the columns hare selects itself next to a model's columns - ``hare app``, ``hare window
    filter 0``. Each holds a space, which no annotation, alias or field name holds; a model's column can still
    be named so (``source_field``), and then hare takes the first free one of ``<name> 2``, ``<name> 3``, ...
    regardless of letter case, each within the identifier length limit.
    """

    @staticmethod
    @ModelCache.fact()
    def get_spaced_column_names(model: type[Model]) -> frozenset[str]:
        """The columns of a model whose names hold a space - the only ones a name of hare's may equal.

        Args:
            model: The model.

        Returns:
            The column names.
        """
        return frozenset(column for column in model._meta.fields_db_projection.values() if " " in column)

    @staticmethod
    @ModelCache.fact()
    def get_old_value_names(model: type[Model]) -> dict[str, str]:
        """The name each column of a model comes under as it was before a write (``RETURNING OLD``) - next to
        the columns as they are after it.

        Args:
            model: The model.

        Returns:
            The names, by column.
        """
        # Each name taken, so two columns never come under one.
        taken_names = set(HareColumnNames.get_spaced_column_names(model))
        old_value_names: dict[str, str] = {}
        for column in model._meta.fields_db_projection.values():
            name = HareColumnNames.get_free_name(f"{RETURNING_OLD_COLUMN_ALIAS_PREFIX}{column}", (), taken_names)
            taken_names.add(name)
            old_value_names[column] = name
        return old_value_names

    @staticmethod
    def get_free_name(name: str, models: Iterable[type[Model]], taken_names: Collection[str] = ()) -> str:
        """A name of hare's no column of the models and none of ``taken_names`` holds - ``name`` itself unless
        one does.

        Args:
            name: The name.
            models: The models whose columns the statement selects next to it.
            taken_names: Other names the statement selects next to it.

        Returns:
            The free name.
        """
        taken = taken_names
        for model in models:
            # Almost always none - no set is made then.
            if spaced_column_names := HareColumnNames.get_spaced_column_names(model):
                taken = {*taken, *spaced_column_names}
        return Identifiers.get_free_name(name, taken)
