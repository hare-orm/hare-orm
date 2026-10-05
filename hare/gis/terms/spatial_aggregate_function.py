from __future__ import annotations

from typing import Any

from hare.exceptions import UnSupportedError
from hare.gis.enums import SpatialFunctionType
from hare.sql.functions.distinct_option_function import DistinctOptionFunction
from hare.sql.sql_context import SqlContext


class SpatialAggregateFunction(DistinctOptionFunction):
    """A spatial aggregate - rendered as an aggregate (``DISTINCT``, ``FILTER``) under the name each
    dialect gives it.

    Args:
        name: The aggregate's ``SpatialFunctionType`` value.
        arguments: The aggregated geometry.
    """

    def __init__(self, name: str, *arguments: Any, **kwargs: Any) -> None:
        super().__init__(name, *arguments, **kwargs)
        self.function_type = SpatialFunctionType(name)

    def get_dialect_special_name(self, sql_context: SqlContext) -> str:
        name = super().get_dialect_special_name(sql_context)
        if not name:
            raise UnSupportedError(
                f"The {self.function_type.value} aggregate has no SQL for the {sql_context.dialect} dialect"
            )
        return name
