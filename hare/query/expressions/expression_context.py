from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Any

from hare.exceptions import UnSupportedError
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql import Table

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
    from hare.query.expressions.conditions.q import Q
    from hare.query.expressions.expression_result import ExpressionResult


@dataclass(frozen=True)
class ExpressionContext:
    """What an expression resolves against: the query's model, table and annotations, the dialect and the
    state the expressions of one build share."""

    binding: ModelBinding
    table: Table
    annotations: dict[str, Any]
    # Set by a query recording its plan: every filter kwarg, literal and cursor value resolved
    # appends `(key, value reference | None)` - a reference when the built term took a form a plan
    # can bind a new value into, None otherwise (the query is then built in full every time). None
    # here means nothing is recorded.
    value_wrapper_references: RecordedValueReferences | None = None
    # A nested relation filter receives its value already built against the root query. Rebuilding
    # a Subquery would change its outer scope and replace the terms the root plan recorded.
    filter_value_is_built: bool = False
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
    #: Bind a grouped annotation through the statement's existing expression bindings.
    grouping_annotation_result: Callable[[str, ExpressionContext], ExpressionResult] | None = None
    #: The dialect the expressions compile for.
    dialect: Dialect = dataclass_field(kw_only=True)
    #: The connection the query runs on; None when compiling DDL or SQL for no particular database.
    connection: DatabaseClient | None = dataclass_field(kw_only=True)
    #: A neutral probe inspects term structure before an execution dialect is chosen.
    checks_dialect_admission: bool = dataclass_field(default=True, kw_only=True)
    #: The source application; None for an explicitly offline query or schema expression.
    application_context: HareContext | None = dataclass_field(default=None, kw_only=True)
    #: Defaults of a computed search vector; explicit configurations on expressions take precedence.
    text_search_config: str | None = None
    requires_search_config: bool = False

    def require_connection_feature(self, name: str, feature: str) -> None:
        """Reject an expression on a bound connection lacking a required feature."""
        connection = self.connection
        if connection is not None and not getattr(connection.features, feature):
            raise UnSupportedError(
                f"{name} can't run on the {connection.connection_alias!r} connection: it needs features.{feature}, "
                "which the connection doesn't have"
            )

    @property
    def model(self) -> type[Model]:
        """The Python class used by field hooks and diagnostics."""
        return self.binding._model
