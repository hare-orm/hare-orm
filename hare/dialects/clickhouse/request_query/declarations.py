from __future__ import annotations

from typing import ClassVar

from hare.contrib.request_query.request_query import RequestQuery
from hare.dialects.base.dialect import Dialect
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.models import Model


class ClickhouseRequestQuery[ModelType: Model](RequestQuery[ModelType]):
    """A request query of a model on a ClickHouse connection: its filters and search are checked
    against ClickHouse only, so they may use what only ClickHouse has - the fields of the dialect
    (``Int128Field``, ``LowCardinalityField``, ``DynamicField``, ...) and a ``MapField``, ``TupleField``
    or ``NestedField``. The model's connection must be a ClickHouse one."""

    dialect: ClassVar[Dialect | None] = CLICKHOUSE_DIALECT
