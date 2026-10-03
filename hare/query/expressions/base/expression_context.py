from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any

from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
    from hare.query.expressions.q import Q


@dataclass(frozen=True)
class ExpressionContext:
    model: type[Model]
    table: Table
    annotations: dict[str, Any]
    # Set by a query recording its plan: every filter kwarg, literal and cursor value resolved
    # appends `(key, value reference | None)` - a reference when the built term took a form a plan
    # can bind a new value into, None otherwise (the query is then built in full every time). None
    # here means nothing is recorded.
    value_wrapper_refs: RecordedValueRefs | None = None
    # Select(relation, extra_condition=Q(...)) conditions by dotted relation path - a nested filter
    # crossing such a relation builds its JOIN with the condition, since the JOIN built first is the
    # one kept.
    select_related_extra_conditions: Mapping[str, Q] | None = None
    # The dotted relation path walked to reach this resolve - a multi-hop nested filter matches an
    # extra_condition registered under the full path.
    select_related_path_prefix: str = ""
    # Whether a filter on a Window(...) annotation resolves to its window function term instead
    # of raising - set for a query that applies such filters to itself wrapped as a derived table
    # (.values()/.values_list()), and for a When(...) condition, which is valid in SELECT.
    window_function_filter_allowed: bool = False
    # Shared by the filters of one build: a to-many relation's dotted path to the .filter() call
    # generation that took its plain join alias. A later, different generation over the same path
    # gets a JOIN of its own.
    multi_valued_join_generations: dict[str, int] | None = None
    # Shared by the same build: the to-many relations the aggregate annotations crossed. Annotations
    # resolve before filters, so a filter about to split such a relation into a second JOIN - which
    # the aggregate wouldn't see - is detected.
    aggregated_multi_valued_paths: AggregatedMultiValuedPaths | None = None
    # The visibility of the query being resolved - every JOIN to a related model is scoped by it. A
    # context built further down passes it on unchanged.
    visibility: RowVisibility = RowVisibility.DEFAULT
    # The annotations being resolved up the chain, shared by it - an annotation referencing itself
    # through another raises QueryError instead of recursing forever.
    annotation_names_in_progress: set[str] = dataclass_field(default_factory=set)
    #: The dialect the expressions compile for.
    dialect: Dialect = dataclass_field(kw_only=True)
    #: The connection the query runs on; None when compiling DDL or SQL for no particular database.
    connection: DatabaseClient | None = dataclass_field(kw_only=True)
