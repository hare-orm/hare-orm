from __future__ import annotations

import dataclasses
import math
from typing import Any

from hare import fields
from tests.contracts.field_sample import FieldSample


class QueryFieldContract:
    """Small field samples with repeated values and a composite key, expected directly from Python inputs."""

    @staticmethod
    def with_composite_key(sample: FieldSample) -> FieldSample:
        def make_fields():
            return {
                **sample.make_fields(),
                "id": fields.IntField(),
                "part": fields.IntField(),
                "pk": fields.CompositePrimaryKey("id", "part"),
                "marker": fields.IntField(default=0),
            }

        return dataclasses.replace(sample, make_fields=make_fields)

    @staticmethod
    def comparable(value: Any) -> Any:
        if isinstance(value, float) and math.isnan(value):
            return "NaN"
        if isinstance(value, dict):
            return {key: QueryFieldContract.comparable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [QueryFieldContract.comparable(item) for item in value]
        return value

    @staticmethod
    async def read(query, shape: str, *, iterate: bool = False) -> list[Any]:
        if shape == "dict":
            query = query.values("result")
        elif shape == "flat":
            query = query.values_list("result", flat=True)
        rows = [row async for row in query.iterator(chunk_size=2)] if iterate else await query
        if shape == "model":
            return [row.result for row in rows]
        if shape == "dict":
            return [row["result"] for row in rows]
        return rows
