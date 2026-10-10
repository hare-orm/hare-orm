from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from hare.lazy_loading.pydantic_classes import PydanticClasses

SchemaT = TypeVar("SchemaT")


@dataclass(frozen=True)
class SqlQueryResult(Generic[SchemaT]):
    rows: list[SchemaT]
    rows_affected: int
    """
    For SELECT (and a write with RETURNING), the number of rows fetched; for INSERT/UPDATE/DELETE,
    the rows the statement itself changed - rows changed by triggers or foreign-key cascades it set
    off are not counted, on any backend.
    """

    @classmethod
    def validate_rows(cls, rows: list[dict[str, Any]], schema: type[SchemaT] | Any) -> list[SchemaT]:
        """The rows validated by a pydantic model or type adapter; as they are for any other schema.

        Args:
            rows: The fetched rows.
            schema: The schema.

        Returns:
            The rows.
        """
        if PydanticClasses.is_type_adapter(schema):
            return [cast("SchemaT", schema.validate_python(row)) for row in rows]
        if PydanticClasses.is_model_class(schema):
            return [cast("SchemaT", schema.model_validate(row)) for row in rows]
        return cast("list[SchemaT]", rows)
