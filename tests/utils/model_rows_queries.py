from typing import Any

from hare.query.queryset import QuerySet
from hare.query.statements.select.model_rows_query import ModelRowsQuery


def get_model_rows_query(queryset: QuerySet[Any, Any]) -> ModelRowsQuery[Any]:
    """The query building and running a queryset's model instances once, bound to the connection
    it runs on."""
    query = ModelRowsQuery(queryset)
    query._is_execution_query = True
    if query._connection is None:
        query._apply_connection(query.get_connection())
    return query
