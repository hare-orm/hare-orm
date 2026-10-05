from __future__ import annotations

from hare.dialects.base.clauses.enums import MergeAction
from hare.instrumentation.enums import RowOperation
from hare.query.enums import Lookup

#: Alias of the derived table `AggregateQuery` joins to the base table when `.aggregate()` has to
#: run on top of aggregate annotations (one row per surviving base row, carrying their values).
AGGREGATE_SUBQUERY_ALIAS = "hare_aggregate_groups"

#: Prefix of the alias each primary-key column of the base table gets inside that derived table -
#: the outer query joins the base table back to it on these columns.
AGGREGATE_SUBQUERY_PRIMARY_KEY_ALIAS_PREFIX = "hare_pk_"

#: Prefix of the alias each `.group_by()` field gets inside the derived table `AggregateQuery`
#: computes `.aggregate()` over for a `.group_by()` queryset - one row per group.
AGGREGATE_SUBQUERY_GROUP_BY_ALIAS_PREFIX = "hare_group_"

#: Alias of the VALUES table `bulk_update()` joins into its `UPDATE ... FROM (VALUES ...)` - a
#: `_` is appended while it equals the updated table's own name.
BULK_UPDATE_VALUES_ALIAS = "hare_bulk_update_values"

#: Alias of the 0/1 column `bulk_update()` selects to tell an object its queryset filter excluded
#: from one whose `Meta.optimistic_lock_field` was stale.
BULK_UPDATE_MATCHES_FILTER_ALIAS = "hare_matches_filter"

#: Prefix of the annotation `iterator()` selects an ordering column under when `.only()`/`.defer()`
#: leaves that column unloaded - its value is the keyset cursor of the next page.
ITERATOR_CURSOR_ANNOTATION_PREFIX = "hare_iterator_cursor_"

#: Prefix of the alias an ORDER BY expression is SELECTed under by a plain `.distinct()` query - the
#: ORDER BY then names that alias, since the expression rendered a second time would bind its
#: literals under new placeholders that a database doesn't match to the selected one.
DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX = "hare_distinct_ordering_"

#: Prefix of the alias a term gets in the derived table a `.values()`/`.values_list()` query is
#: wrapped in to filter on a window function - the outer query filters and orders by these columns.
WINDOW_FILTER_COLUMN_ALIAS_PREFIX = "hare_window_filter_"

#: Alias of the derived table of a `.values()`/`.values_list()` set operation's combined rows.
VALUES_SET_OPERATION_ALIAS = "hare_values_set_operation"

#: Lookup suffixes whose condition is never true for a NULL column - a filter with one of them (and a
#: value other than None) guarantees the column isn't NULL on every row it keeps. An empty suffix is a
#: plain equality.
NULL_REJECTING_LOOKUP_SUFFIXES = frozenset(
    {
        Lookup.EXACT,
        "exact",
        Lookup.IEXACT,
        Lookup.GT,
        Lookup.GTE,
        Lookup.LT,
        Lookup.LTE,
        Lookup.IN,
        Lookup.RANGE,
        Lookup.CONTAINS,
        Lookup.ICONTAINS,
        Lookup.STARTSWITH,
        Lookup.ISTARTSWITH,
        Lookup.ENDSWITH,
        Lookup.IENDSWITH,
    }
)

#: The RETURNING alias of an upsert's inserted-row flag (``QueryClauses.get_upsert_inserted_flag_sql()``).
UPSERT_INSERTED_FLAG_ALIAS = "hare_row_inserted"

#: The key of a returned row's values before the write (``returning(old=...)``).
RETURNING_OLD_VALUES_KEY = "old"

#: The prefix of the annotation ``insert_from()`` selects a value every row gets under - an ``auto_now``
#: field's moment, the active tenant.
INSERT_FROM_CONSTANT_PREFIX = "hare_insert_from_"

MERGE_VALUES_ALIAS = "hare_merge_values"

MERGE_ACTION_ALIAS = "hare_merge_action"

#: The key of a row ``merge().returning()`` gives holding the action that wrote it.
MERGE_ACTION_KEY = "merge_action"

#: The row operation a ``merge()`` branch's action reports.
ROW_OPERATION_BY_MERGE_ACTION = {
    MergeAction.UPDATE: RowOperation.UPDATE,
    MergeAction.INSERT: RowOperation.INSERT,
    MergeAction.DELETE: RowOperation.DELETE,
}


#: The error of a bulk insert whose ``on_conflict`` names columns no unique index of the table has.
MISSING_CONFLICT_TARGET_MESSAGE = (
    "{model}.objects.bulk_create(on_conflict=...): the table has no unique index or constraint on {fields}"
    "{condition} - name the fields of one, or declare it on the model"
)
#: The tail of MISSING_CONFLICT_TARGET_MESSAGE when ``conflict_where`` is given.
MISSING_CONFLICT_TARGET_CONDITION_TEXT = " matching conflict_where"
#: The ``on_conflict_constraint`` naming the model's primary key, whatever name its constraint has.
PRIMARY_KEY_CONFLICT_CONSTRAINT = "pk"

#: What a metric of aggregate() named as a column of the model is selected as - its own name would be the
#: column's alias, which a database resolving aliases in WHERE reads for the column.
AGGREGATE_FIELD_NAMED_METRIC_PREFIX = "hare_metric_"

#: An update moving rows of a model with Meta.change_capture to keys an expression gives - their values read
#: before it can't be told which row they were.
MOVED_KEY_EXPRESSION_MESSAGE = (
    "update() of {model} sets {field!r}, a column of its primary key, from an expression - Meta.change_capture "
    "reads the rows before the update by their keys and can't follow them to the keys the expression gives; set "
    "it to a value, or capture on a database returning the rows as they were (Features.supports_returning_old_new)"
)
