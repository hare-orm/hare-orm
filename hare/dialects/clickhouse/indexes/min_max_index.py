from __future__ import annotations

from hare.ddl.indexes.index import Index
from hare.dialects.clickhouse.indexes.clickhouse_index import ClickhouseIndex


class MinMaxIndex(ClickhouseIndex):
    """``TYPE minmax`` - the least and the greatest value of each granule: skips the granules a
    comparison or a range of the key can't match. A plain ``Index`` is one of granularity 1."""

    INDEX_TYPE = "minmax"

    def get_stored_form(self) -> Index:
        """The plain ``Index`` for granularity 1 - the database keeps the two alike.

        Returns:
            The index the database keeps.
        """
        if self.granularity != 1:
            return self
        _path, args, kwargs = self.deconstruct()
        return Index(*args, **kwargs)
