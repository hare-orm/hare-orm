from __future__ import annotations

#: RowVisibility.tenant when no tenant scope was captured - the active one is read instead. A
#: captured scope may itself be None.
AMBIENT_TENANT_NOT_OVERRIDDEN = object()


#: What a plan lookup returns on a miss - a plan's values may be None.
PLAN_CACHE_MISS = object()


#: .get()/Model.objects.get() fetch this many rows, not 1, so a second row's
#: presence can raise MultipleObjectsReturned without a separate COUNT query.
GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK = 2


#: The columns a union()/intersection()/difference() of model querysets selects to tell which
#: model each row is of - the model's app and class name.
COMBINED_QUERY_APP_COLUMN = "hare_app"
COMBINED_QUERY_MODEL_COLUMN = "hare_model"


#: The RETURNING alias of a column's value before the write - the column with this prefix.
RETURNING_OLD_COLUMN_ALIAS_PREFIX = "hare_old_"


#: The name ``merge()``'s source rows are read under, the name of the ``VALUES`` table a list of rows
#: is, and the column the action that wrote a returned row comes back in.
MERGE_SOURCE_ALIAS = "hare_merge_source"
#: The name a branch of ``merge()`` reads the source row's columns by - ``merge_source__<column>``.
MERGE_SOURCE_KEY = "merge_source"


#: Why aggregate() refuses a .distinct(<fields>) queryset.
AGGREGATE_OVER_DISTINCT_ON_MESSAGE = (
    "aggregate() on a .distinct(<fields>) queryset is not supported - its result would be "
    "silently wrong. Use a plain .distinct() or aggregate over a subquery instead."
)
