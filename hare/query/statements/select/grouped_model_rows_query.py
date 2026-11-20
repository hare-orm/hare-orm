from __future__ import annotations

from typing import Any

from hare.query.rows.model_rows.grouped_model_rows import GroupedModelRows
from hare.query.statements.select.model_rows_query import ModelRowsQuery


class GroupedModelRowsQuery(ModelRowsQuery[Any]):
    """Use the ordinary model SELECT executor with shared-instance result reading.

    Used for a many-to-many join selecting one owner-key annotation and no joined
    models, nested prefetch or result-transforming extensions. Query execution, SQL
    preparation, tenant checks, retries and field decoding retain their common paths.
    """

    __slots__ = ()

    rows_reader_class = GroupedModelRows
