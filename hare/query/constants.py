from __future__ import annotations

import os
import re

from hare.query.enums import Lookup

#: The connection feature (a ``Features`` attribute) a lookup needs beyond its dialect: SQLite runs
#: the regular expression lookups only on a connection that installed its ``REGEXP`` functions
#: (DB_URL ``?install_regexp_functions=true``).
LOOKUP_REQUIRED_FEATURES: dict[str, str] = {
    Lookup.POSIX_REGEX: "supports_posix_regex",
    Lookup.IPOSIX_REGEX: "supports_posix_regex",
}

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
#: literals under new placeholders that Postgres doesn't match to the selected one.
DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX = "hare_distinct_ordering_"

#: Prefix of the alias a term gets in the derived table a `.values()`/`.values_list()` query is
#: wrapped in to filter on a window function - the outer query filters and orders by these columns.
WINDOW_FILTER_COLUMN_ALIAS_PREFIX = "hare_window_filter_"


#: Prefix of the alias an ORDER BY term is selected under in the inner derived table of a plain
#: `.distinct()` `.values()`/`.values_list()` query ordered by a field it doesn't select.
DISTINCT_FIRST_OCCURRENCE_ORDERING_ALIAS_PREFIX = "hare_first_occurrence_ordering_"

#: Alias of the `ROW_NUMBER()` column numbering the rows of each distinct combination of the selected
#: columns in that query's ordering - only the first of them is kept.
DISTINCT_FIRST_OCCURRENCE_ROW_NUMBER_ALIAS = "hare_first_occurrence_row_number"

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

#: Separates a ``values()`` output key from the index of one column of a composite key it selects
#: - ``values(target=...)`` over a composite foreign key selects ``target__hare_key_component_0``,
#: ``..._1`` and returns them combined as one tuple under ``target``.
COMPOSITE_KEY_COMPONENT_ALIAS_SEPARATOR = "__hare_key_component_"


#: Characters and sequences an annotation, alias or aggregate name may not contain - quotes,
#: brackets, a semicolon, whitespace or an SQL comment, like Django's own check.
FORBIDDEN_ANNOTATION_NAME_PATTERN = re.compile(r"['`\"\]\[;\s]|--|/\*|\*/")


#: RowVisibility.tenant when no tenant scope was captured - the active one is read instead. A
#: captured scope may itself be None.
AMBIENT_TENANT_NOT_OVERRIDDEN = object()


#: What a plan lookup returns on a miss - a plan's values may be None.
PLAN_CACHE_MISS = object()

#: The calls of a queryset's call signature whose filter values a plan binds (``filter()``,
#: ``exclude()``) - the other calls are part of the plan key as they are.
CALL_SIGNATURE_FILTER_CALLS = frozenset({"filter", "exclude"})

#: The query options ``select_for_update()`` sets - a direct ``get()`` takes them in its plan key.
SELECT_FOR_UPDATE_OPTION_NAMES = (
    "select_for_update",
    "select_for_update_nowait",
    "select_for_update_skip_locked",
    "select_for_update_of",
    "select_for_update_no_key",
)


#: .get()/.get_or_none()/Model.objects.get() fetch this many rows, not 1, so a second row's
#: presence can raise MultipleObjectsReturned without a separate COUNT query.
GET_FETCH_LIMIT_FOR_MULTIPLICITY_CHECK = 2


#: The directory of the hare package - a query's creation site is the first frame outside it.
HARE_PACKAGE_DIRECTORY = os.path.normcase(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

#: The columns a union()/intersection()/difference() of model querysets selects to tell which
#: model each row is of - the model's app and class name.
COMBINED_QUERY_APP_COLUMN = "hare_app"
COMBINED_QUERY_MODEL_COLUMN = "hare_model"

#: The RETURNING alias of an upsert's inserted-row flag (``Dialect.get_upsert_inserted_flag_sql()``).
UPSERT_INSERTED_FLAG_ALIAS = "hare_row_inserted"

#: The annotation a many-to-many prefetch read in one query carries each related row's owner key
#: in - removed from the instances once they are laid out.
PREFETCH_OWNER_KEY_ANNOTATION = "hare_prefetch_owner_key"

#: How many namedtuple classes of ``values_list(named=True)`` selections are kept.
ROW_CLASS_CACHE_SIZE = 256

#: How many relation classes made for a manager's own queryset class are kept.
RELATION_CLASS_CACHE_SIZE = 256
