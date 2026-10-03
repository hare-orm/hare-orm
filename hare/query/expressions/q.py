from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from copy import copy
from datetime import timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.enums import ParameterPosition
from hare.dialects.identifiers import Identifiers
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields.base.field import Field
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.json.json_path_field import JSONPathField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_delta_field import TimeDeltaField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.fields.generated import GeneratedField
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.composite import KeyColumns
from hare.query.constants import LOOKUP_REQUIRED_FEATURES, PLAN_CACHE_MISS
from hare.query.enums import Connector, Lookup, LookupTarget
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.expression_context import ExpressionContext
from hare.query.expressions.base.expression_result import ExpressionResult, TableCriterionTuple
from hare.query.expressions.base.value import Value
from hare.query.expressions.constants import (
    DATE_TIMESTAMP_COMPARISON_LOOKUPS,
    ISNULL_LOOKUP_SUFFIXES,
    LIST_LOOKUP_SUFFIXES,
    LONG_IN_LIST_STRUCTURE,
    MANY_TO_MANY_EXISTS_LOOKUPS,
    MIRRORED_COMPARISON_LOOKUPS,
    TO_MANY_RELATION_SHORTCUT_LOOKUPS,
)
from hare.query.expressions.exists import Exists
from hare.query.expressions.exists_term import ExistsTerm
from hare.query.expressions.f import F
from hare.query.expressions.modifier import QueryModifier
from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.subquery import Subquery
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value_refs.array_value_ref import ArrayValueRef
from hare.query.expressions.value_refs.encoded_value_ref import EncodedValueRef
from hare.query.expressions.value_refs.like_value_ref import LikeValueRef
from hare.query.expressions.value_refs.list_parameter_value_ref import ListParameterValueRef
from hare.query.expressions.value_refs.list_value_ref import ListValueRef
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.range_value_ref import RangeValueRef
from hare.query.expressions.value_refs.related_key_value_ref import RelatedKeyValueRef
from hare.query.expressions.value_refs.related_value_ref import RelatedValueRef
from hare.query.expressions.value_refs.row_list_value_ref import RowListValueRef
from hare.query.expressions.value_refs.scalar_value_ref import ScalarValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.filters import FieldLookups, Like, ValueEncoders
from hare.query.filters.json_path_lookups import JsonPathLookups
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.sql import JoinType, Table
from hare.sql.functions.date_as_timestamp import DateAsTimestamp
from hare.sql.functions.declarations import JsonComparand
from hare.sql.functions.timestamp_comparand import TimestampComparand
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.terms.array import Array
from hare.sql.terms.base.parameterized_value_wrapper import ParameterizedValueWrapper
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.between_criterion import BetweenCriterion
from hare.sql.terms.criteria.contains_criterion import ContainsCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.json_attribute_criterion import JSONAttributeCriterion
from hare.sql.terms.criteria.not_criterion import Not
from hare.sql.terms.functions.function import Function
from hare.sql.terms.list_parameter import ListParameter
from hare.sql.terms.star import Star
from hare.sql.terms.tuple import Tuple
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.filters.field_lookup import FieldLookup
    from hare.query.lookup_info.lookup_info import LookupInfo


class Q(Plannable):
    """A condition to filter by, composed from smaller ones. ``Q(...)`` joins its conditions with AND;
    ``Q.with_connector(Connector.OR, ...)`` joins them with OR. The constructor takes nothing but
    conditions, so a filter on any field name fits in it.

    Args:
        conditions: Inner ``Q`` expressions or ``Exists(...)`` conditions.
        filters: The filters of this node.
    """

    __slots__ = (
        "children",
        "filters",
        "expression",
        "connector",
        "_is_negated",
        "_filter_call_generation",
        "_needs_double_negation_rewrite",
    )

    def __init__(self, *conditions: Q | Exists, **filters: Any) -> None:
        self._set_up(Connector.AND, conditions, filters)

    @classmethod
    def with_connector(cls, connector: Connector, /, *conditions: Q | Exists, **filters: Any) -> Q:
        """Returns a ``Q`` joining its conditions with ``connector`` - ``Connector.OR`` for "any one
        holds": ``Q.with_connector(Connector.OR, name="Ursula", title="Wizard")``.

        Args:
            connector: ``Connector.AND`` or ``Connector.OR``.
            conditions: Inner ``Q`` expressions, or ``Exists(...)`` conditions.
            filters: Filter statements.

        Returns:
            The ``Q``.

        Raises:
            QueryError: ``connector`` isn't a ``Connector``.
        """
        condition = cls.__new__(cls)
        condition._set_up(connector, conditions, filters)
        return condition

    def _set_up(self, connector: Connector, args: tuple[Q | Exists, ...], kwargs: dict[str, Any]) -> None:
        """Sets up a new ``Q`` from its conditions, joined by ``connector``.

        Args:
            connector: How the conditions join.
            args: Inner ``Q`` expressions, or ``Exists(...)`` conditions.
            kwargs: Filter statements.

        Raises:
            QueryError: ``connector`` isn't a ``Connector``, or a positional condition is
                neither a ``Q`` nor an ``Exists(...)``.
        """
        if type(connector) is not Connector:
            if connector not in Connector:
                raise QueryError("A Q's connector must be Connector.AND or Connector.OR")
            connector = Connector(connector)
        #: The boolean expression this node matches on - set only on a ``Q(Exists(...))`` node,
        #: which has no kwargs and no children.
        self.expression: Exists | None = None
        children: list[Q] = []
        if args:
            if len(args) == 1 and not kwargs and isinstance(args[0], Exists):
                self.expression = args[0]
            else:
                nodes = [Q(arg) if isinstance(arg, Exists) else arg for arg in args]
                if kwargs:
                    nodes.insert(0, Q.with_connector(connector, **kwargs))
                    kwargs = {}
                for node in nodes:
                    if not isinstance(node, Q):
                        raise QueryError("All ordered arguments must be Q nodes or Exists(...) conditions")
                    children.append(node)
        if kwargs:
            filters = {}
            for key, value in kwargs.items():
                if type(value) is Value:
                    value = self.get_literal_value(value)
                if key.endswith(LIST_LOOKUP_SUFFIXES):
                    value = self.get_list_lookup_value(key, value)
                filters[key] = value
            kwargs = filters
        #: Contains the sub-Q's that this Q is made up of
        self.children: tuple[Q, ...] = tuple(children)
        #: The filters of this Q, as passed to Q(...)/`.filter(...)`: {"id": 5}, {"name__icontains": "x"}.
        self.filters: dict[str, Any] = kwargs
        #: Specifies if this Q does an AND or OR on its children
        self.connector = connector
        self._is_negated = False
        #: 0 by default ("not part of a distinguishable QuerySet.filter()/.exclude() call") -
        #: see _with_filter_call_generation()'s own docstring for what this is for.
        self._filter_call_generation = 0
        #: False by default - see __invert__()'s own docstring for what this tracks and why.
        self._needs_double_negation_rewrite = False

    @staticmethod
    def get_literal_value(value: Any) -> Any:
        """Unwraps a ``Value(...)`` filter value into its literal, so it is converted and validated
        by the filtered field like the same plain value.

        Args:
            value: The filter value.

        Returns:
            The literal, or ``value`` unchanged for anything else, ``Value(None)`` (a comparison
            with NULL, not an ``__isnull`` lookup) or a ``Value`` wrapping an expression.
        """
        if type(value) is not Value or value.value is None or isinstance(value.value, (Term, Expression)):
            return value
        return value.value

    @staticmethod
    def get_list_lookup_value(key: str, value: Any) -> Any:
        """Materializes an ``__in``/``__not_in`` value given as any iterable (a generator, a
        ``range``, a ``frozenset``, ``dict.keys()``, ...) into a list, consuming it exactly once.

        Args:
            key: The filter kwarg name.
            value: The filter value.

        Returns:
            The list, or ``value`` unchanged for any other key or a list/tuple/set, subquery or
            expression value.

        Raises:
            UnSupportedError: ``value`` is a string or bytes, which would otherwise be matched
                character by character.
        """
        if not key.endswith(LIST_LOOKUP_SUFFIXES) or isinstance(value, (list, tuple, set)):
            return value
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            raise UnSupportedError(
                f"{key}: expected an iterable of values, got a {type(value).__name__} - wrap a single value in a list"
            )
        from hare.models import Model

        if isinstance(value, Iterable) and not isinstance(value, (Term, Model)):
            return list(value)
        return value

    def __and__(self, other: Q | Exists) -> Q:
        """
        Returns a binary AND of Q objects, use ``AND`` operator.

        Raises:
            QueryError: AND operation requires a Q node
        """
        if not isinstance(other, (Q, Exists)):
            raise QueryError("AND operation requires a Q node")
        return Q(self, other)

    def __or__(self, other: Q | Exists) -> Q:
        """
        Returns a binary OR of Q objects, use ``OR`` operator.

        Raises:
            QueryError: OR operation requires a Q node
        """
        if not isinstance(other, (Q, Exists)):
            raise QueryError("OR operation requires a Q node")
        return Q.with_connector(Connector.OR, self, other)

    def __rand__(self, other: Exists) -> Q:
        return Q(other, self)

    def __ror__(self, other: Exists) -> Q:
        return Q.with_connector(Connector.OR, other, self)

    def __invert__(self) -> Q:
        """
        Returns a negated instance of the Q object, use ``~`` operator.
        """
        q = Q.with_connector(self.connector, *self.children, **self.filters)
        q.expression = self.expression
        # A fresh Q() always starts _is_negated=False - toggling THIS instance's own state
        # (not q's just-initialized one) is what makes ~~q restore the original rather than
        # staying negated forever.
        q._is_negated = not self._is_negated
        # The generation .filter()/.exclude() stamped survives the negation.
        q._filter_call_generation = self._filter_call_generation
        # Negating an already negated Q. For a condition over a to-many relation `~q` is a
        # correlated NOT EXISTS, so `~~q` can't fall back to the plain JOIN - marked here, decided
        # in _finalize_modifier(), where the model is known. Once set, it stays set.
        q._needs_double_negation_rewrite = self._needs_double_negation_rewrite or self._is_negated
        return q

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Q):
            return False
        return (
            self.children == other.children
            and self.connector == other.connector
            and self.filters == other.filters
            and self.expression is other.expression
            and self._is_negated == other._is_negated
            and self._needs_double_negation_rewrite == other._needs_double_negation_rewrite
        )

    def __hash__(self) -> int:
        return hash(self.get_structure_key())

    def __repr__(self) -> str:
        arguments = [repr(child) for child in self.children]
        if self.expression is not None:
            arguments.append(repr(self.expression))
        arguments += [f"{key}={value!r}" for key, value in self.filters.items()]
        if self.connector == Connector.OR:
            return f"{self.get_negation_prefix()}Q.with_connector({', '.join(['Connector.OR', *arguments])})"
        return f"{self.get_negation_prefix()}Q({', '.join(arguments)})"

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The join type, the negation, the ``.filter()``/``.exclude()`` call the node came from, and
        either the children's descriptions or each filter's key with its value's structure.

        Args:
            context: The context the condition is resolved in.

        Returns:
            The description, None for an empty node, and when a child, an ``Exists(...)`` or a value
            keeps no plan.
        """
        head = (self.connector, self._is_negated, self._needs_double_negation_rewrite, self._filter_call_generation)
        if self.children:
            return PlanDescription.combine(head, (child.get_plan_description(context) for child in self.children))
        if self.expression is not None:
            return PlanDescription.combine(head, (self.expression.get_plan_description(context),))
        values: list[Any] = []
        if not self.filters:
            return None
        annotations = context.annotations
        filter_structures: list[tuple[str, Any]] = []
        for key, value in self.filters.items():
            if annotations:
                values.extend(Expression.get_referenced_annotation_values(key, context))
            if isinstance(value, Plannable):
                value_description = self._get_filter_value_plan_description(value, context)
                if value_description is None:
                    return None
                filter_structures.append((key, value_description.structure))
                values.extend(value_description.values)
            else:
                filter_structures.append(
                    (
                        key,
                        self.describe_value_filter(key, value, values, context.single_parameter_in_list_min_length),
                    )
                )
        return PlanDescription((head, tuple(filter_structures)), values)

    @classmethod
    def describe_value_filter(
        cls, key: str, value: Any, values: list[Any], single_parameter_in_list_min_length: int | None = None
    ) -> Any:
        """Describes a filter whose value is no part of a query - a plain value or a list.

        Args:
            key: The filter key.
            value: The filter value.
            values: The values the plan binds - the filter's own are appended.
            single_parameter_in_list_min_length: The length from which an ``__in`` list binds as one
                parameter, None to describe every list by its length.

        Returns:
            The structure of the value, part of the plan key.
        """
        if cls.is_constant_filter(key, value):
            return type(value), value
        if isinstance(value, (list, tuple, set)):
            if key.endswith(LIST_LOOKUP_SUFFIXES):
                # An __in/__not_in list renders a parameter per value other than None - a None ORs
                # in an IS NULL test; a list of none but None is a constant condition binding
                # nothing.
                has_none = None in value
                bound_count = sum(item is not None for item in value) if has_none else len(value)
                if bound_count:
                    values.append(value)
                if (
                    single_parameter_in_list_min_length is not None
                    and bound_count >= single_parameter_in_list_min_length
                    # Key rows bind by their count.
                    and type(next(iter(value))) is not tuple
                ):
                    # One parameter whatever the length.
                    return LONG_IN_LIST_STRUCTURE, has_none
                return bound_count, has_none
            # A list of another length renders another number of parameters.
            values.append(value)
            return len(value)
        # The lookup builds its criterion from a value of this type - a float or a Decimal is
        # cast, a date part is an integer.
        values.append(value)
        return type(value)

    @staticmethod
    def is_constant_filter(key: str, value: Any) -> bool:
        """Whether a filter's value is rendered into the SQL text rather than bound - a None (a
        NULL test, or a comparison with NULL) or the boolean of an ``__isnull`` lookup. Its value is
        part of the plan key; the build records no reference for it.

        Args:
            key: The filter key.
            value: The filter value.

        Returns:
            True for such a filter.
        """
        return value is None or (type(value) is bool and key.endswith(ISNULL_LOOKUP_SUFFIXES))

    @staticmethod
    def _get_filter_value_plan_description(value: Plannable, context: PlanContext) -> PlanDescription | None:
        """Describes a filter value that is a part of a query: a query compared with (built as a
        subquery), an expression, or a ``RawSQL`` fragment.

        Args:
            value: The value.
            context: The context the filter is resolved in.

        Returns:
            The description, None for a value keeping no plan.
        """
        # Deferred import: hare.query.queryset imports this module at import time.
        from hare.query.queryset import QuerySet
        from hare.query.statements import AwaitableQuery

        if isinstance(value, QuerySet):
            # The values it selects, else its primary key - or the rows it combines, whose
            # primary key is selected from them built into the filter.
            value = value._get_filter_value_query() if value._combination is None else value._get_compiler()
        if isinstance(value, AwaitableQuery):
            filter_value_query = value._get_filter_value_query()
            return PlanDescription.combine(
                ("query", filter_value_query.get_pinned_connection_name()),
                (filter_value_query.get_plan_description(context),),
            )
        return value.get_plan_description(context)

    @staticmethod
    def _is_constant_list_condition(key: str, value: Any) -> bool:
        """Whether an ``__in``/``__not_in`` filter resolves to a constant condition - its list
        holds no value but None (``1=0``, ``IS NULL``, and their negations).

        Args:
            key: The filter key.
            value: The filter value.

        Returns:
            True for such a filter.
        """
        return (
            key.endswith(LIST_LOOKUP_SUFFIXES)
            and isinstance(value, (list, tuple, set))
            and all(item is None for item in value)
        )

    @staticmethod
    def _is_union_query(value: Any) -> bool:
        """Whether a filter value is a set operation of querysets (``a.union(b)``).

        Args:
            value: The value.

        Returns:
            True for a queryset combining querysets of model instances.
        """
        # Deferred import: hare.query.queryset imports this module at import time.
        from hare.query.queryset import QuerySet

        return isinstance(value, QuerySet) and value._combination is not None and not value._selects_values()

    def get_negation_prefix(self) -> str:
        """The ``~`` operators that rebuild this node's negation from a plain ``Q(...)``."""
        return ("~~" if self._needs_double_negation_rewrite else "") + ("~" if self._is_negated else "")

    def get_structure_key(self) -> tuple[Any, ...]:
        """A hashable key two trees equal by ``__eq__`` share.

        Returns:
            The join type, negation, children's keys and filters of this tree.
        """
        return (
            self.connector,
            self._is_negated,
            self._needs_double_negation_rewrite,
            tuple(child.get_structure_key() for child in self.children),
            tuple(sorted(((key, self.get_hashable_value(value)) for key, value in self.filters.items()), key=repr)),
            id(self.expression) if self.expression is not None else None,
        )

    @staticmethod
    def get_hashable_value(value: Any) -> Any:
        """A filter value as a hashable one equal for equal values - a list/set/dict by its items."""
        if isinstance(value, (list, tuple)):
            return type(value).__name__, tuple(Q.get_hashable_value(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return type(value).__name__, frozenset(Q.get_hashable_value(item) for item in value)
        if isinstance(value, dict):
            return "dict", frozenset((key, Q.get_hashable_value(item)) for key, item in value.items())
        try:
            hash(value)
        except TypeError:
            return type(value).__name__, repr(value)
        return value

    def get_referenced_field_names(self) -> set[str]:
        """The fields this condition reads - each lookup's and each ``F()`` value's first path part.

        Returns:
            The field names.
        """
        names: set[str] = set()
        for child in self.children:
            names |= child.get_referenced_field_names()
        for key, value in self.filters.items():
            names.add(key.partition("__")[0])
            names |= {reference.name.partition("__")[0] for reference in self.get_field_references(value)}
        return names

    @staticmethod
    def get_field_references(value: Any) -> list[F]:
        """The ``F()`` references a filter value holds, itself or inside a list/tuple."""
        if isinstance(value, F):
            return [value]
        if isinstance(value, (list, tuple)):
            return [reference for item in value for reference in Q.get_field_references(item)]
        return []

    def with_renamed_field(self, old_name: str, new_name: str) -> Q:
        """A copy reading the field ``new_name`` wherever this condition reads ``old_name``.

        Args:
            old_name: The field's former name.
            new_name: The field's new name.

        Returns:
            The renamed copy - this tree itself is left unchanged.
        """

        def rename_path(path: str) -> str:
            first, separator, rest = path.partition("__")
            return f"{new_name}{separator}{rest}" if first == old_name else path

        def rename_value(value: Any) -> Any:
            if isinstance(value, F):
                return F(rename_path(value.name))
            if isinstance(value, (list, tuple)):
                return type(value)(rename_value(item) for item in value)
            return value

        renamed = copy(self)
        renamed.children = tuple(child.with_renamed_field(old_name, new_name) for child in self.children)
        renamed.filters = {rename_path(key): rename_value(value) for key, value in self.filters.items()}
        return renamed

    def __bool__(self) -> bool:
        if self.filters or self.expression is not None:
            return True
        return any(self.children)

    def negate(self) -> None:
        """
        Negates the current Q object. (mutation)
        """
        # See __invert__()'s own docstring for what this tracks - kept in lockstep with it since
        # this mutates the SAME state __invert__() otherwise returns a fresh copy with.
        self._needs_double_negation_rewrite = self._needs_double_negation_rewrite or self._is_negated
        self._is_negated = not self._is_negated

    def _with_filter_call_generation(self, generation: int) -> Q:
        """Returns a copy of the whole Q tree stamped with ``generation`` - the number of the
        ``.filter()``/``.exclude()`` call it came from. A lookup over a to-many relation from
        another call gets a JOIN of its own: ``.filter(tags__name="A").filter(tags__name="B")``
        matches two related rows.
        """
        copy = Q.with_connector(
            self.connector,
            *[child._with_filter_call_generation(generation) for child in self.children],
            **self.filters,
        )
        copy.expression = self.expression
        copy._is_negated = self._is_negated
        copy._filter_call_generation = generation
        # See __invert__()'s own docstring - must survive stamping the same way _is_negated does,
        # or a `~Q(rel__x)` argument re-negated by exclude()'s own `~stamped` (QuerySet.
        # _append_filters(), stamping happens BEFORE that second negation) would lose the mark.
        copy._needs_double_negation_rewrite = self._needs_double_negation_rewrite
        return copy

    def _stamp_filter_call_generation(self, generation: int) -> Q:
        """Stamps ``generation`` on this Q tree in place, like ``_with_filter_call_generation()``
        without the copy - only for a tree built for the one ``.filter()``/``.exclude()`` call and
        shared with nothing else.

        Args:
            generation: The filter call's generation.

        Returns:
            This Q.
        """
        self._filter_call_generation = generation
        for child in self.children:
            child._stamp_filter_call_generation(generation)
        return self

    @staticmethod
    def _get_relation_path(expression_context: ExpressionContext, related_field_name: str) -> str:
        """The path of a relation from the queried model.

        Args:
            expression_context: The context the filter is resolved in.
            related_field_name: The relation's name on the filtered model.

        Returns:
            The ``__``-separated path.
        """
        if expression_context.select_related_path_prefix:
            return f"{expression_context.select_related_path_prefix}__{related_field_name}"
        return related_field_name

    def _get_relation_joins(
        self, expression_context: ExpressionContext, related_field_name: str, table: Table
    ) -> tuple[RelationalField[Model], str, list[TableCriterionTuple]]:
        """The JOINs a filter through a relation adds - a separate JOIN of a to-many relation for
        each ``.filter()``/``.exclude()`` call, scoped by the related model's default scope and a
        ``Select(relation, extra_condition=...)`` of the same path.

        Args:
            expression_context: The context the filter is resolved in.
            related_field_name: The relation's name on the filtered model.
            table: The table the filtered model is read through.

        Returns:
            The relation, its path from the queried model and the JOINs, the related table's last.

        Raises:
            QueryError: An aggregate over the same to-many relation would see only one of
                several JOINs, or an extra condition reads a further relation.
        """
        related_field = cast("RelationalField[Model]", expression_context.model._meta.fields_map[related_field_name])
        full_path = self._get_relation_path(expression_context, related_field_name)
        # The first generation to touch a to-many path takes the unsuffixed alias; only a later,
        # different generation gets a JOIN of its own.
        effective_filter_call_generation = 0
        if (
            related_field.is_multi_valued
            and self._filter_call_generation
            and expression_context.multi_valued_join_generations is not None
        ):
            seen_generation = expression_context.multi_valued_join_generations.get(full_path)
            if seen_generation is None or seen_generation == self._filter_call_generation:
                expression_context.multi_valued_join_generations[full_path] = self._filter_call_generation
            else:
                effective_filter_call_generation = self._filter_call_generation
                # An aggregate that crossed this relation reads only one of the JOINs, while the
                # WHERE spans both - its value would be wrong, so this raises.
                aggregated_paths = expression_context.aggregated_multi_valued_paths
                if aggregated_paths and full_path in aggregated_paths:
                    raise QueryError(
                        f"Combining an aggregate (Count/Sum/Avg/...) over '{full_path}' with two or "
                        "more separate .filter()/.exclude() calls on that same to-many relation produces an "
                        "ambiguous result - the aggregate only sees one of the resulting JOINs. Combine the "
                        f"filters into a single call (e.g. .filter({full_path}__a=x, "
                        f"{full_path}__b=y) or Q(...) & Q(...)) instead, or move the aggregate into "
                        "a separate query."
                    )
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        if related_field.is_multi_valued and tracker is not None:
            tracker.record_path(full_path, effective_filter_call_generation)
        # A `Select(relation, extra_condition=Q(...))` of the same relation is folded into this
        # JOIN, not left for _join_select_related() - the first JOIN added for a table wins.
        required_joins = LookupPaths.get_scoped_joins(
            table,
            related_field,
            related_field_name,
            visibility=expression_context.visibility,
            extra_condition=(
                expression_context.select_related_extra_conditions.get(full_path)
                if expression_context.select_related_extra_conditions
                else None
            ),
            filter_call_generation=effective_filter_call_generation,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        return related_field, full_path, required_joins

    def _get_nested_filter(
        self, expression_context: ExpressionContext, key: str, value: Any, table: Table
    ) -> tuple[QueryModifier, RelatedValueRef | None]:
        """Resolves a ``related__field=value`` kwarg by resolving ``Q(**{forwarded_fields: value})``
        against the related model. When a plan is recorded, the nested filter's value reference is
        folded into one ``RelatedValueRef`` under the caller's key - None when it recorded nothing
        bindable.
        """
        related_field_name, __, forwarded_fields = key.partition("__")
        related_field, full_path, required_joins = self._get_relation_joins(
            expression_context, related_field_name, table
        )
        q = Q(**{forwarded_fields: value})
        # The generation goes one hop deeper, for a multi-hop path.
        q._filter_call_generation = self._filter_call_generation
        # Typed identically to ExpressionContext.value_wrapper_refs itself (list is invariant, so a
        # narrower element type here would mismatch what ExpressionContext expects below).
        nested_refs: RecordedValueRefs | None = [] if expression_context.value_wrapper_refs is not None else None
        modifier = q.get_result(
            ExpressionContext(
                model=related_field.related_model,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
                table=required_joins[-1][0],
                annotations=expression_context.annotations,
                value_wrapper_refs=nested_refs,
                # Passed on, so a further hop matches an extra_condition registered under the full
                # path.
                select_related_extra_conditions=expression_context.select_related_extra_conditions,
                select_related_path_prefix=full_path,
                # Forwarded so a further to-many hop of this same lookup is recorded too, and gets
                # a separate JOIN per .filter() call like a first hop does.
                aggregated_multi_valued_paths=expression_context.aggregated_multi_valued_paths,
                multi_valued_join_generations=expression_context.multi_valued_join_generations,
                visibility=expression_context.visibility,
            )
        )
        ref: RelatedValueRef | None = None
        if nested_refs is not None and len(nested_refs) == 1:
            _nested_key, nested_ref = nested_refs[0]
            if isinstance(nested_ref, RelatedValueRef):
                # A multi-hop filter - converted for the model at its far end.
                ref = nested_ref
            elif nested_ref is not None:
                ref = RelatedValueRef(nested_ref, related_field.related_model)
        return QueryModifier(joins=required_joins) & modifier, ref

    def _get_custom_kwarg(
        self,
        expression_context: ExpressionContext,
        annotation_name: str,
        key: str,
        value: Any,
        table: Table,
        compared_value_field: Field[Any] | None = None,
        records_value: bool = False,
    ) -> tuple[QueryModifier, LiteralValueRef | ListValueRef | ListParameterValueRef | None]:
        """Resolves a filter on an annotation (``.annotate(n=...).filter(n__gte=...)``) - with the
        lookups of the annotation's output field where it is known, the lookups of a value with no
        field otherwise.

        Args:
            expression_context: The context the filter resolves in.
            annotation_name: The annotation the key starts with.
            key: The filter key.
            value: The filter value.
            table: The table filtered.
            compared_value_field: The field an expression value reads, when there is one.
            records_value: Whether the value is the one given (not rewritten) - only then is a
                reference returned.

        Returns:
            The modifier, and the reference a later query binds its value through - None when
            the value isn't a single parameter of a plain comparison.

        Raises:
            FieldError: The key names no lookup of the annotation's value, or one its field
                doesn't support.
        """
        raw_value = value
        # How the given value becomes the bound one - the same conversion for a later value.
        parameter_converter: Callable[[Any], Any] | None = None
        suffix = key.removeprefix(annotation_name).removeprefix("__")
        field_lookup = FieldLookups.get(None).get(suffix)
        annotation = expression_context.annotations[annotation_name]
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            annotation_info = ExpressionResult(term=annotation)
        else:
            annotation_info = annotation.get_result(expression_context)

        if (
            getattr(annotation_info.term, "is_analytic", False)
            and not expression_context.window_function_filter_allowed
        ):
            # A window function is only valid in SELECT/ORDER BY - SQL rejects it outright in
            # WHERE and HAVING. A .values()/.values_list() query applies such a filter to itself
            # wrapped as a derived table instead (window_function_filter_allowed).
            raise QueryError(
                f"Cannot filter on '{key}' - it is a window function (Window(...)) annotation, "
                "and SQL does not allow filtering on window functions directly. Filter a "
                ".values()/.values_list() query instead, which applies the filter to the query "
                "wrapped in a subquery, e.g. "
                # <pk field>, not 'pk': .values() doesn't take the `pk` alias.
                f"Model.objects.filter(pk__in=Subquery(queryset.filter({key}=...).values(<pk field>)))."
            )

        # A field's own lookups (containment of a range, array or JSON value) apply once the
        # annotation's output field is known. An aggregate's output field is its argument's, so only
        # an aggregate keeping a container or date/time value takes them.
        annotation_output_field = annotation_info.output_field  # type: ignore[call-overload]
        value_field = annotation.get_value_field(annotation_info) if isinstance(annotation, Expression) else None
        if annotation_info.term.contains_aggregate:
            # An aggregate collecting values into an array/JSON/range value (ArrayAgg, JSONBAgg)
            # is filtered with that value's own lookups (`__contains`, `__overlap`, `__len`, ...);
            # one keeping its argument's date/time type (Max/Min) compares values of that type.
            annotation_output_field = (
                value_field
                if self._is_container_field(value_field) or self._is_date_or_time_field(value_field)
                else None
            )
        elif annotation_output_field is None and (
            self._is_date_or_time_field(value_field) or isinstance(value_field, TimeDeltaField)
        ):
            # A date/time/duration literal (Value(...)) compares values of its own type.
            annotation_output_field = value_field
        use_field_aware_filters = annotation_output_field is not None
        if annotation_output_field is not None:
            field_lookup = FieldLookups.get(annotation_output_field).get(suffix, field_lookup)
            if field_lookup is not None and not FieldLookups.is_supported(annotation_output_field, suffix):
                raise FieldError(annotation_output_field.get_unsupported_lookup_message())
        if field_lookup is None:
            raise self._get_unknown_key_error(expression_context, key)
        operator = expression_context.dialect.filter_operators.get_operator(field_lookup)
        encoder_field: Field[Any] | None = None
        if use_field_aware_filters and field_lookup.value_encoder is not None and not isinstance(value, Term):
            # A field-aware lookup takes an encoded value, as for a model field. The field-blind
            # __in/__isnull entries get the raw value.
            encoder_field = (
                annotation_output_field.output_field
                if isinstance(annotation_output_field, GeneratedField)
                else annotation_output_field
            )
            parameter_converter = partial(
                self._encode_annotation_filter_value,
                field_lookup.value_encoder,
                expression_context.model,
                encoder_field,
                expression_context.dialect,
            )
            value = parameter_converter(value)
        elif (
            annotation_output_field is not None
            and self._is_container_field(annotation_output_field)
            and not isinstance(value, Term)
        ):
            # An array/JSON/range lookup with no value_encoder of its own (`__contains` of a
            # JSONBAgg) binds the value in the field's stored form, as the plain-field branch does.
            parameter_converter = partial(
                expression_context.dialect.types.get_lookup_value,
                annotation_output_field,
                instance=expression_context.model,
            )
            value = parameter_converter(value)
        elif (
            use_field_aware_filters
            and isinstance(annotation_output_field, TimeDeltaField)
            and isinstance(value, timedelta)
        ):
            # A comparison operator with no value_encoder of its own binds the raw value - a
            # timedelta is stored as whole microseconds, and no driver accepts it as a parameter
            # for that column type (`.annotate(span=F("end") - F("start")).filter(span__gt=...)`).
            parameter_converter = partial(
                expression_context.dialect.types.get_db_value,
                TemporalArithmetic.TIMEDELTA_OUTPUT_FIELD,  # type: ignore[arg-type]
                instance=None,
            )
            value = parameter_converter(value)
        elif (
            annotation_output_field is not None
            and self._is_date_or_time_field(annotation_output_field)
            and value is not None
            and not isinstance(value, Term)
        ):
            # A comparison operator with no value_encoder of its own binds the value as the
            # annotation's date/time field writes it - a naive datetime in the configured zone, a
            # date's string parsed, a naive time with the configured offset.
            parameter_converter = partial(
                expression_context.dialect.types.get_lookup_value,
                self._get_effective_field(annotation_output_field),
                instance=expression_context.model,
            )
            value = parameter_converter(value)
        # An annotation has no field to tell whether it can be NULL - treated as nullable.
        # A Decimal binds as TEXT on SQLite and an annotation has no column affinity: the annotation
        # is cast to NUMERIC, or compared as exact decimal text when every bound value is a Decimal.
        compared_term = annotation_info.term
        if isinstance(annotation, Value):
            # A bare `$1 = $2` can't be typed - the dialect types the literal where it has to.
            compared_term = Value.get_typed_term(
                compared_term, annotation.value, ParameterPosition.COMPARED_VALUE, expression_context.dialect
            )
        if field_lookup.compares_json_path_text and isinstance(compared_term, JSONAttributeCriterion):
            compared_term = compared_term.get_text_term()
        elif isinstance(annotation_output_field, JSONPathField) and isinstance(value, Term):
            value = self._get_json_comparand(value, compared_value_field, expression_context.dialect)
        if self._holds_decimal_value(value):
            compared_term = expression_context.dialect.get_decimal_compared_term(
                compared_term, only_decimals=self._holds_only_decimal_values(value)
            )
        # HAVING whenever either side reads an aggregate - the annotation itself, one mixing an
        # aggregate with a column (Count(...) + F("id")), or an F() value naming an aggregate.
        criterion = operator(compared_term, value)
        ref: LiteralValueRef | ListValueRef | ListParameterValueRef | None = None
        if records_value and raw_value is not None and not isinstance(raw_value, (Term, Expression)):
            if isinstance(raw_value, (list, tuple, set)):
                if use_field_aware_filters and field_lookup.value_encoder is not None and encoder_field is not None:
                    list_ref = self._get_value_ref(
                        expression_context,
                        criterion,
                        operator,
                        raw_value,
                        value,
                        encoder_field,
                        field_lookup.value_encoder,
                    )
                    if isinstance(list_ref, (ListValueRef, ListParameterValueRef)):
                        ref = list_ref
            else:
                ref = self._get_annotation_value_ref(criterion, value, parameter_converter)
        if criterion.contains_aggregate:
            return QueryModifier(having_criterion=criterion, has_nullable_column=True), ref
        return QueryModifier(where_criterion=criterion, has_nullable_column=True), ref

    @staticmethod
    def _get_annotation_value_ref(
        criterion: Criterion, value: Any, parameter_converter: Callable[[Any], Any] | None
    ) -> LiteralValueRef | None:
        """The reference a later query binds the value of a filter on an annotation through - the
        one parameter holding the converted value, or the one parameter of the term it was
        converted into (a JSON path's JSON text cast to the JSON type).

        Args:
            criterion: The filter's criterion.
            value: The converted value it was built with.
            parameter_converter: How the given value was converted, None when it wasn't.

        Returns:
            The reference, None when the value isn't one parameter of the criterion.
        """
        if isinstance(value, Term):
            if parameter_converter is None or not any(node is value for node in criterion.find_(Term)):
                return None
            if len(value.find_(ValueWrapper)) != 1:
                return None
            return LiteralValueRef(
                value.find_(ValueWrapper)[0], partial(Q._get_converted_term_parameter, parameter_converter)
            )
        value_wrappers = [node for node in criterion.find_(ValueWrapper) if node.value is value]
        if len(value_wrappers) != 1:
            return None
        return LiteralValueRef(value_wrappers[0], parameter_converter)

    @staticmethod
    def _get_converted_term_parameter(parameter_converter: Callable[[Any], Any], value: Any) -> Any:
        """The one parameter of the term a value of a filter on an annotation converts into.

        Args:
            parameter_converter: The conversion.
            value: The value.

        Returns:
            The parameter's value; ``PLAN_CACHE_MISS`` - of no type a plan binds - when the value
            converts into anything else.
        """
        term = parameter_converter(value)
        if not isinstance(term, Term):
            return PLAN_CACHE_MISS
        value_wrappers = term.find_(ValueWrapper)
        return value_wrappers[0].value if len(value_wrappers) == 1 else PLAN_CACHE_MISS

    @staticmethod
    def _encode_annotation_filter_value(
        value_encoder: Callable[..., Any], model: Any, field: Field[Any] | None, dialect: Any, value: Any
    ) -> Any:
        """Runs a lookup's ``value_encoder`` on a value of a filter on an annotation.

        Args:
            value_encoder: The lookup's encoder.
            model: The model queried.
            field: The field the annotation resolves to.
            dialect: The dialect the query runs on.
            value: The value.

        Returns:
            The encoded value.
        """
        return value_encoder(value, model, field, dialect)

    @staticmethod
    def _get_effective_field(field: Field[Any]) -> Field[Any]:
        """A field, or the real field a GeneratedField wraps."""
        return field.output_field if isinstance(field, GeneratedField) else field

    @classmethod
    def _is_date_or_time_field(cls, field: Field[Any] | None) -> bool:
        """Whether a field holds a datetime, date or time value.

        Args:
            field: The field, possibly a GeneratedField, or None.

        Returns:
            True for a DatetimeField, DateField or TimeField.
        """
        return field is not None and isinstance(cls._get_effective_field(field), (DatetimeField, DateField, TimeField))

    @staticmethod
    def _is_container_field(field: Field[Any] | None) -> bool:
        """Whether a field holds an array, JSON or range value.

        Args:
            field: The field, possibly a GeneratedField, or None.

        Returns:
            True for a field whose ``holds_container_value`` is set.
        """
        effective_field = field.output_field if isinstance(field, GeneratedField) else field
        return effective_field is not None and effective_field.holds_container_value

    @staticmethod
    def _holds_only_decimal_values(value: Any) -> bool:
        """Whether a filter value is a Decimal, or a list/tuple/set of Decimals and Nones.

        Args:
            value: The raw or encoded filter value.

        Returns:
            True when every bound value is a Decimal.
        """
        if isinstance(value, Decimal):
            return True
        return isinstance(value, (list, tuple, set)) and all(
            item is None or isinstance(item, Decimal) for item in value
        )

    @staticmethod
    def _holds_decimal_value(value: Any) -> bool:
        """Whether a filter value is a Decimal, or a list/tuple/set holding one.

        Args:
            value: The raw or encoded filter value.

        Returns:
            True when a Decimal is bound for the comparison.
        """
        if isinstance(value, Decimal):
            return True
        return isinstance(value, (list, tuple, set)) and any(isinstance(item, Decimal) for item in value)

    @staticmethod
    def _get_filter_join_table(model: type[Model], key: str, table: Table, filter_table: Table) -> Table:
        """Aliases the joined table of a backward FK/O2O lookup, so a self-referential relation
        (or a chain whose base table is the related table) can tell the two occurrences apart.

        Args:
            model: The model the lookup key is resolved against.
            key: The lookup key, e.g. ``team_members__isnull``.
            table: The table `model` is currently read through.
            filter_table: The unaliased related table registered for the lookup.

        Returns:
            The aliased table for a backward relation, `filter_table` unchanged otherwise.
        """
        relation_name = key.partition("__")[0]
        if not isinstance(model._meta.fields_map.get(relation_name), BackwardFKRelation):
            return filter_table
        return filter_table.as_(Identifiers.get_within_limit(f"{table.get_table_name()}__{relation_name}__direct"))

    def _get_m2m_isnull_kwarg(
        self, expression_context: ExpressionContext, key: str, value: Any, table: Table
    ) -> QueryModifier | None:
        """Resolves ``<m2m>__isnull``/``<m2m>__not_isnull`` into a correlated ``[NOT] EXISTS`` over the
        through table joined to the related model, each under its default scope.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.
            table: The table the filtered model is read through.

        Returns:
            The modifier, None if ``key`` isn't such a lookup on a many-to-many field.

        Raises:
            UnSupportedError: ``value`` isn't a bool.
        """
        relation_name, _, lookup = key.partition("__")
        if lookup not in (Lookup.ISNULL, Lookup.NOT_ISNULL):
            return None
        m2m_field = expression_context.model._meta.fields_map.get(relation_name)
        if not isinstance(m2m_field, ManyToManyFieldInstance):
            return None
        if not isinstance(value, bool):
            raise UnSupportedError(f"__{lookup} expects a bool, got {value!r}")
        joins = LookupPaths.get_scoped_joins(
            table,
            m2m_field,
            relation_name,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        (through_table, correlation), (related_table, related_criterion) = joins
        inner_query = (
            QueryBuilder()
            .from_(through_table)
            .join(related_table, how=JoinType.INNER)
            .on(related_criterion)
            .select(Star())
            .where(correlation)
        )
        is_empty = value if lookup == Lookup.ISNULL else not value
        exists_criterion = ExistsTerm(inner_query)
        return QueryModifier(where_criterion=Not(exists_criterion) if is_empty else exists_criterion)

    @staticmethod
    def _join_visible_m2m_target(
        expression_context: ExpressionContext,
        m2m_field: ManyToManyFieldInstance[Any],
        relation_name: str,
        joins: list[TableCriterionTuple],
        table: Table,
    ) -> Criterion | None:
        """Appends to ``joins`` a join from the through table to the related model under its default
        scope, so a bare relation filter counts only the links to visible rows.

        Args:
            expression_context: The context the filter is resolved in.
            m2m_field: The many-to-many field the filter names.
            relation_name: The field's name on the filtered model.
            joins: The filter's joins, holding the through-table join; changed in place.
            table: The table the filtered model is read through.

        Returns:
            A criterion holding only when the linked row is visible, None if the related model has
            no default scope.
        """
        from hare.query.scopes.row_scopes import RowScopes

        related_meta = m2m_field.related_model._meta
        target_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{table.get_table_name()}__{relation_name}__visible")
        )
        visible_target_criterion = RowScopes.of(m2m_field.related_model).get_criterion(
            target_table,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        if visible_target_criterion is None:
            return None
        through_table = joins[0][0]
        target_pk_columns = KeyColumns.get_source_columns(related_meta)
        joins.append(
            (
                target_table,
                KeyColumns.row_equality(
                    [through_table[column] for column in m2m_field.forward_keys],
                    [target_table[column] for column in target_pk_columns],
                )
                & visible_target_criterion,
            )
        )
        return target_table[target_pk_columns[0]].notnull()

    @staticmethod
    def _get_forward_relation_isnull_kwarg(
        expression_context: ExpressionContext, key: str, value: Any
    ) -> QueryModifier | None:
        """Resolves ``<fk>__isnull``/``<fk>__not_isnull``/``<fk>=None`` on a forward relation whose
        target has a default scope: a hidden target counts as no target. The key column
        (``<fk>_id``) keeps comparing the stored value.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.

        Returns:
            The modifier, None if ``key`` isn't such a lookup or its target has no default scope
            left.
        """
        from hare.query.scopes.row_scopes import RowScopes

        model = expression_context.model
        relation_name, _, lookup = key.partition("__")
        if lookup == Lookup.EXACT:
            if value is not None:
                return None
            is_empty = True
        elif lookup in (Lookup.ISNULL, Lookup.NOT_ISNULL) and isinstance(value, bool):
            is_empty = value if lookup == Lookup.ISNULL else not value
        else:
            return None
        if relation_name not in model._meta.fk_fields and relation_name not in model._meta.o2o_fields:
            return None
        relation_field = cast("RelationalField[Model]", model._meta.fields_map[relation_name])
        related_model = relation_field.related_model
        related_meta = related_model._meta
        table = expression_context.table
        related_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{table.get_table_name()}__{relation_name}__live")
        )
        visible_target_criterion = RowScopes.of(related_model).get_criterion(
            related_table,
            visibility=expression_context.visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        if visible_target_criterion is None:
            return None
        # One column per key component - a composite key is null exactly when every column is.
        source_columns = [
            table[model._meta.fields_db_projection[source_field]] for source_field in relation_field.source_fields
        ]
        target_columns = [
            related_table[related_meta.fields_db_projection[to_field_instance.model_field_name]]
            for to_field_instance in relation_field.to_field_instances
        ]
        key_matches: Criterion = visible_target_criterion
        for target_column, source_column in zip(target_columns, source_columns, strict=True):
            key_matches = (target_column == source_column) & key_matches
        visible_target_exists = ExistsTerm(QueryBuilder().from_(related_table).select(Star()).where(key_matches))
        if not is_empty:
            return QueryModifier(where_criterion=visible_target_exists)
        key_is_null: Criterion = source_columns[0].isnull()
        for source_column in source_columns[1:]:
            key_is_null &= source_column.isnull()
        return QueryModifier(where_criterion=key_is_null | Not(visible_target_exists))

    def _process_filter_kwarg(
        self,
        expression_context: ExpressionContext,
        lookup_info: LookupInfo,
        value: Any,
        table: Table,
        join_table: Table | None = None,
    ) -> tuple[
        Criterion,
        tuple[Table, Criterion] | None,
        Field[Any] | None,
        Field[Any] | None,
        Callable[..., Any] | None,
        Callable[..., Any],
        Any,
    ]:
        """Builds the criterion of a lookup on a field's value, or on a many-to-many or backward
        relation itself (joined to its through or related table).

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The filter key's description.
            value: The filter value.
            table: The table the filtered model is read through.
            join_table: The table a relation's lookup compares on, when it is joined already.

        Returns:
            The criterion; the join a relation's lookup adds; the field compared (None for a
            relation, or for a value that is a SQL term); the field the value was converted by;
            the lookup's value encoder (None for the field's own conversion); the operator; and
            the converted value the criterion embeds.
        """
        if lookup_info.target == LookupTarget.RELATION:
            criterion, join, op, value = self._get_relation_lookup_criterion(
                expression_context, lookup_info, value, table, join_table
            )
            return criterion, join, None, None, None, op, value
        model = expression_context.model
        field_lookup = cast("FieldLookup", lookup_info.field_lookup)
        model_field = cast("Field[Any]", lookup_info.field)
        value_field = cast("Field[Any]", lookup_info.value_field)  # type: ignore[call-overload]
        # Like Django, `field=None` and `field__iexact=None` mean `field__isnull=True`.
        if (
            value is None
            and lookup_info.lookup in (Lookup.EXACT, Lookup.IEXACT)
            and (lookup_info.term_transforms or not lookup_info.transforms)
            and (isnull_lookup := FieldLookups.get(value_field).get(Lookup.ISNULL)) is not None
        ):
            field_lookup = isnull_lookup
            value = True
        field_object: Field[Any] | None = None
        value_encoder: Callable[..., Any] | None = None
        encoder_field = FieldLookups.get_effective_field(value_field)
        if not isinstance(value, Term):
            field_object = model_field
            value_encoder = field_lookup.value_encoder
            if value_encoder is not None:
                # A GeneratedField is encoded as its output_field - an encoder reaches for that
                # field's own attributes (a range's element type, an array's base_field).
                value = value_encoder(value, model, encoder_field, expression_context.dialect)
            else:
                value = expression_context.dialect.types.get_lookup_value(value_field, value, model)
        op = expression_context.dialect.filter_operators.get_operator(field_lookup)
        term: Term = table[
            model_field.source_field
            or model._meta.fields_db_projection.get(model_field.model_field_name)
            or model_field.model_field_name
        ]
        if field_object is not None:
            func = field_object.get_function_cast(expression_context.dialect)
            if func is not None:
                term = func(field_object, term)
        for term_transform in lookup_info.term_transforms:
            term = term_transform(term)
        if isinstance(value, TimestampComparand):
            term = DateAsTimestamp(term, value.zone_name)
        criterion = op(term, value)
        # `value` is the converted value the criterion embeds - a plan binds a later value only
        # where the criterion holds the encoder's own output (identity, not equality), see
        # _get_regular_kwarg().
        return criterion, None, field_object, encoder_field, value_encoder, op, value

    def _get_relation_lookup_criterion(
        self,
        expression_context: ExpressionContext,
        lookup_info: LookupInfo,
        value: Any,
        table: Table,
        join_table: Table | None,
    ) -> tuple[Criterion, TableCriterionTuple, Callable[..., Any], Any]:
        """Builds the criterion of a lookup on a many-to-many or backward relation itself - the
        related rows' key compared on the through table or the related table, joined by the key
        columns (a row of them for a composite key).

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The filter key's description.
            value: The filter value.
            table: The table the filtered model is read through.
            join_table: The through or related table, when it is joined already.

        Returns:
            The criterion, the join, the operator and the converted value.
        """
        model = expression_context.model
        relation = lookup_info.relations[-1]
        field_lookup = cast("FieldLookup", lookup_info.field_lookup)
        compared_term: Term
        if isinstance(relation, ManyToManyFieldInstance):
            related_table = (
                join_table if join_table is not None else Table(relation.through, schema=relation.through_schema)
            )
            left_columns = KeyColumns.get_source_columns(model._meta)
            right_columns = relation.backward_keys
            compared_term = (
                Tuple(*[related_table[column] for column in relation.forward_keys])
                if relation.related_model._meta.pk is None
                else related_table[relation.forward_key]
            )
        else:
            backward_relation = cast("BackwardFKRelation[Model]", relation)
            owner_meta = backward_relation.related_model._meta
            related_table = (
                join_table
                if join_table is not None
                else self._get_filter_join_table(
                    model, backward_relation.model_field_name, table, Table(owner_meta.db_table)
                )
            )
            left_columns = tuple(
                target_field.source_field or target_field.model_field_name
                for target_field in backward_relation.to_field_instances
            )
            right_columns = backward_relation.relation_source_fields
            # A LEFT JOIN that found no row nulls every joined column, so a model without a
            # primary key is tested by its key column to this row - never NULL in a joined row.
            compared_term = related_table[
                KeyColumns.get_source_columns(owner_meta)[0]
                if owner_meta.has_primary_key
                else backward_relation.relation_source_fields[0]
            ]
        join = (
            related_table,
            KeyColumns.row_equality(
                [table[column] for column in left_columns], [related_table[column] for column in right_columns]
            ),
        )
        if field_lookup.value_encoder is not None and not isinstance(value, Term):
            # A value that is a SQL term (OuterRef/Subquery/F()) is compared as it is.
            value = field_lookup.value_encoder(value, model, relation, expression_context.dialect)
        op = expression_context.dialect.filter_operators.get_operator(field_lookup)
        return op(compared_term, value), join, op, value

    def _get_regular_kwarg(
        self,
        expression_context: ExpressionContext,
        lookup_info: LookupInfo,
        value: Any,
        table: Table,
        *,
        bindable: bool,
    ) -> tuple[
        QueryModifier,
        ScalarValueRef
        | ListValueRef
        | ListParameterValueRef
        | RangeValueRef
        | RelatedValueRef
        | LikeValueRef
        | ArrayValueRef
        | EncodedValueRef
        | None,
    ]:
        key = lookup_info.key
        m2m_isnull_modifier = self._get_m2m_isnull_kwarg(expression_context, key, value, table)
        if m2m_isnull_modifier is not None:
            return m2m_isnull_modifier, None
        if self._crosses_relation(lookup_info):
            modifier, nested_ref = self._get_nested_filter(expression_context, key, value, table)
            return modifier, nested_ref if bindable else None

        criterion, join, field_object, cache_ref_field, value_encoder, op, encoded_value = self._process_filter_kwarg(
            expression_context, lookup_info, value, table
        )
        joins = [join] if join else []
        if joins:
            # A lookup on a relation itself (.filter(tags=obj), tags__in=..., ...) joins its
            # through or related table here, scoped like any other JOIN to that model.
            relation = cast("RelationalField[Model]", lookup_info.relations[-1])
            relation_name = relation.model_field_name
            tracker = AggregatedMultiValuedPaths.get_from(expression_context)
            if relation.is_multi_valued and tracker is not None:
                prefix = expression_context.select_related_path_prefix
                tracker.record_path(f"{prefix}__{relation_name}" if prefix else relation_name)
            if isinstance(relation, BackwardFKRelation):
                # The join targets the related model's own table, so its soft-delete/tenant scope
                # belongs in the ON clause, as for a nested books__field=... lookup.
                LookupPaths._fold_ambient_scope_into_join(
                    joins,
                    relation.related_model,
                    visibility=expression_context.visibility,
                    dialect=expression_context.dialect,
                    connection=expression_context.connection,
                )
                join = joins[0]
            else:
                LookupPaths._fold_through_model_ambient_scope_into_join(
                    joins,
                    relation,
                    visibility=expression_context.visibility,
                    dialect=expression_context.dialect,
                    connection=expression_context.connection,
                )
                join = joins[0]
                if isinstance(relation, ManyToManyFieldInstance):
                    visible_target_criterion = self._join_visible_m2m_target(
                        expression_context, relation, relation_name, joins, table
                    )
                    if visible_target_criterion is not None:
                        criterion &= visible_target_criterion
        # field_object is None for a relation's lookup (its JOIN routes a negation through
        # _negate_across_joins()) and for a value that is a SQL term (F()/Subquery/OuterRef/... -
        # treated as possibly NULL).
        if field_object is None and criterion.contains_aggregate:
            # A plain field compared with an aggregate (budget__lt=F("aggregate_annotation")) is a
            # HAVING condition, exactly like the mirrored aggregate_annotation__gt=F("budget").
            return QueryModifier(having_criterion=criterion, joins=joins, has_nullable_column=True), None
        modifier = QueryModifier(
            where_criterion=criterion, joins=joins, has_nullable_column=field_object is None or field_object.null
        )
        if not bindable or join is not None or field_object is None or cache_ref_field is None:
            return modifier, None
        return modifier, self._get_value_ref(
            expression_context, criterion, op, value, encoded_value, cache_ref_field, value_encoder
        )

    @staticmethod
    def _get_value_ref(
        expression_context: ExpressionContext,
        criterion: Criterion,
        op: Callable[..., Any],
        value: Any,
        encoded_value: Any,
        cache_ref_field: Field[Any],
        value_encoder: Callable[..., Any] | None,
    ) -> (
        ScalarValueRef
        | ListValueRef
        | ListParameterValueRef
        | RangeValueRef
        | LikeValueRef
        | ArrayValueRef
        | EncodedValueRef
        | None
    ):
        """The reference a later query binds a filter's value through - found by where the
        criterion holds the converted value.

        Args:
            expression_context: The context the filter is resolved in.
            criterion: The filter's criterion.
            op: The operator that built it.
            value: The value given.
            encoded_value: The converted value the criterion embeds.
            cache_ref_field: The field the value was converted by.
            value_encoder: The lookup's value encoder, None for the field's own conversion.

        Returns:
            The reference, None when the value isn't where a reference can rebind it.
        """
        # Identity, not shape: a custom lookup may compare with a value derived from the encoded one
        # - a plan would then bind the wrong value.
        if value_encoder is None:
            if (
                isinstance(criterion, BasicCriterion)
                and isinstance(criterion.right, ValueWrapper)
                and criterion.right.value is encoded_value
            ):
                return ScalarValueRef(criterion.right, cache_ref_field)
            # The converted value as an argument of a function in the criterion (a JSON
            # containment test): one parameter holds it.
            value_wrappers = [node for node in criterion.find_(ValueWrapper) if node.value is encoded_value]
            if not isinstance(value, (list, tuple, set)) and len(value_wrappers) == 1:
                return ScalarValueRef(value_wrappers[0], cache_ref_field)
            return None
        # A BetweenCriterion means both bounds were given. The bounds must be the encoded values
        # themselves, as above.
        if isinstance(criterion, BetweenCriterion):
            if (
                isinstance(criterion.start, ValueWrapper)
                and isinstance(criterion.end, ValueWrapper)
                and isinstance(encoded_value, (list, tuple))
                and len(encoded_value) == 2
                and criterion.start.value is encoded_value[0]
                and criterion.end.value is encoded_value[1]
            ):
                range_encoder = None if value_encoder is ValueEncoders.encode_list else value_encoder
                return RangeValueRef(criterion.start, criterion.end, cache_ref_field, range_encoder)
            return None
        # A LIKE lookup: the prefix/suffix flags are read off the partial the criterion was built
        # with. The case-insensitive pattern sits inside UPPER(...) - the innermost ValueWrapper is
        # referenced either way.
        if isinstance(criterion, Like) and isinstance(op, partial):
            case_insensitive = op.keywords.get("case_insensitive")
            pattern_wrapper: ValueWrapper | None = None
            if case_insensitive is False and isinstance(criterion.right, ValueWrapper):
                pattern_wrapper = criterion.right
            elif (
                case_insensitive is True
                and isinstance(criterion.right, Function)
                and len(criterion.right.args) == 1
                and isinstance(criterion.right.args[0], ValueWrapper)
            ):
                pattern_wrapper = criterion.right.args[0]
            if pattern_wrapper is not None:
                return LikeValueRef(
                    pattern_wrapper, cache_ref_field, value_encoder, op.keywords["prefix"], op.keywords["suffix"]
                )
        # An encoder returning a whole array literal: the encoder's result must be the right operand
        # itself - the Array node is then replaced as a whole.
        if (
            isinstance(criterion, BasicCriterion)
            and criterion.right is encoded_value
            and isinstance(criterion.right, Function)
            and len(criterion.right.args) == 1
            and isinstance(criterion.right.args[0], Array)
        ):
            return ArrayValueRef(criterion.right.args[0], cache_ref_field, value_encoder)
        # Another encoded lookup ending as a plain comparison: the ValueWrapper must hold the
        # encoder's result itself, not a piece derived from it.
        elif (
            isinstance(criterion, BasicCriterion)
            and not isinstance(criterion, Like)
            and isinstance(criterion.right, ValueWrapper)
            and criterion.right.value is encoded_value
        ):
            return EncodedValueRef(criterion.right, cache_ref_field, value_encoder)
        # The encoded value as an argument of a function in the criterion (``UPPER(?)`` of
        # ``__iexact``, the text query of ``__search``, a JSON containment test): one parameter
        # holds the encoder's result itself.
        if not isinstance(value, (list, tuple, set)):
            value_wrappers = [node for node in criterion.find_(ValueWrapper) if node.value is encoded_value]
            if len(value_wrappers) == 1:
                return EncodedValueRef(value_wrappers[0], cache_ref_field, value_encoder)
        if not isinstance(value, (list, tuple, set)):
            return None
        # A list the dialect binds as one parameter - any length binds through it.
        list_parameters = [node for node in criterion.find_(Term) if isinstance(node, ListParameter)]
        if len(list_parameters) == 1:
            return ListParameterValueRef(list_parameters[0], cache_ref_field, value_encoder)
        # __in/__not_in: one ContainsCriterion in the tree, alone or ORed with a NULL check. A None
        # in the list isn't bound - the plan key holds whether there is one. The container must be a
        # literal Tuple of values, not a subquery. Other encoded lookups keep no plan.
        contains_nodes = criterion.find_(ContainsCriterion)
        if len(contains_nodes) != 1:
            return None
        contains_node = contains_nodes[0]
        if not isinstance(contains_node.container, Tuple):
            return None
        container_values = contains_node.container.values
        if not all(isinstance(v, ParameterizedValueWrapper) for v in container_values):
            return None
        parameterized_values = cast("list[ParameterizedValueWrapper]", container_values)
        # Identity again: the Tuple must wrap the encoded list's own elements.
        if not isinstance(encoded_value, (list, tuple, set)):
            return None
        bound_encoded_values = [item for item in encoded_value if item is not None]
        if len(bound_encoded_values) != len(parameterized_values):
            return None
        min_length = expression_context.dialect.single_parameter_in_list_min_length
        if min_length is not None and len(parameterized_values) >= min_length:
            # A long list the dialect couldn't bind as one parameter - its plan key holds no length.
            return None
        if not all(v.value is enc for v, enc in zip(parameterized_values, bound_encoded_values, strict=True)):
            return None
        return ListValueRef(contains_node.container, cache_ref_field, value_encoder)

    @staticmethod
    def _get_forward_relation_lookup_value(key: str, field_object: RelationalField[Model], value: Any) -> Any:
        """Converts related-model instances in a relation lookup's value into their key values.

        Args:
            key: The filter kwarg name.
            field_object: The forward FK/O2O field.
            value: The lookup value - an instance, a key value, a list of either, or a queryset.

        Returns:
            The value with every instance replaced by its ``to_field`` value, and a queryset of the
            related model narrowed to that one column.

        Raises:
            QueryError: An instance isn't of the related model, or is unsaved.
        """
        from hare.models import Model
        from hare.query.queryset import QuerySet
        from hare.query.statements.select.combined_query import CombinedQuery

        to_field_name = field_object.to_field_instance.model_field_name
        related_model = field_object.related_model

        def get_key_value(element: Any) -> Any:
            if not isinstance(element, Model):
                return element
            if not isinstance(element, related_model):
                raise QueryError(
                    f"'{key}' expects {related_model.__name__} instances or key values, got a "
                    f"{type(element).__name__} instance"
                )
            (key_value,) = element._get_relation_key_values((to_field_name,), f"Filter '{key}'")
            if key_value is None:
                raise QueryError(f"'{key}' got an unsaved {related_model.__name__} instance")
            return key_value

        if isinstance(value, QuerySet) and not value._selects_values() and value.model is related_model:
            if value._combination is not None:
                return cast("CombinedQuery", value._get_compiler())._get_field_values_query(to_field_name)
            return value._get_field_values_query(to_field_name)
        if isinstance(value, (list, tuple, set)):
            return [get_key_value(element) for element in value]
        return get_key_value(value)

    def _get_relation_filter_params(
        self, expression_context: ExpressionContext, lookup_info: LookupInfo, value: Any
    ) -> tuple[LookupInfo, Any]:
        """The lookup a filter kwarg resolves to and the value it compares - a lookup on a forward
        FK/O2O itself compares its key column (``author__in`` is ``author_id__in``) with the
        related instances' keys, one on a to-many relation the related instances' primary keys.

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The filter key's description.
            value: The filter value.

        Returns:
            The lookup's description and the value.
        """
        key = lookup_info.key
        compared_lookup_info = lookup_info
        filter_value: Any = value
        meta = expression_context.model._meta
        if lookup_info.target == LookupTarget.RELATION and len(lookup_info.relations) == 1:
            relation = cast("RelationalField[Model]", lookup_info.relations[0])
            if relation.model_field_name in meta.fk_fields or relation.model_field_name in meta.o2o_fields:
                from hare.models import Model

                source_field_name = cast("str", relation.source_field)
                compared_lookup_info = meta._get_lookup_info(
                    f"{source_field_name}__{lookup_info.lookup}" if lookup_info.lookup else source_field_name
                )
                if lookup_info.lookup:
                    filter_value = self._get_forward_relation_lookup_value(key, relation, value)
                elif isinstance(value, Model):
                    (filter_value,) = value._get_relation_key_values(
                        (relation.to_field_instance.model_field_name,), f"Filter '{key}'"
                    )
            else:
                # A to-many relation compares the related rows' primary key - an instance names its own.
                filter_value = getattr(value, "pk", value)
        return compared_lookup_info, filter_value

    def _get_filter_value(
        self, expression_context: ExpressionContext, key: str, value: Any
    ) -> tuple[Any, list[TableCriterionTuple], Field[Any] | None]:
        """The value a filter kwarg compares - a query value becomes a subquery, an expression
        value its term.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.

        Returns:
            The value, the joins an expression value adds, and the field an expression value reads.

        Raises:
            QueryError: An expression is an element of an ``__in``/``__not_in`` list.
        """
        value_field: Field[Any] | None = None
        filter_value = self._get_subquery_filter_value(value, expression_context.value_wrapper_refs)
        filter_value_joins: list[TableCriterionTuple] = []
        if isinstance(filter_value, Subquery):
            filter_value.raise_if_selects_more_columns(
                key, self._get_filter_column_count(expression_context.model, key)
            )
        if isinstance(filter_value, Expression):
            expression_result = filter_value.get_result(expression_context)
            EncryptedFieldMixin.raise_if_compared_to_expression(
                expression_context.model,
                key,
                expression_result.output_field,  # type: ignore[call-overload]
            )
            filter_value = self._get_date_timestamp_comparand(
                expression_context,
                key,
                expression_result.term,
                expression_result.output_field,  # type: ignore[call-overload]
            )
            filter_value_joins = expression_result.joins
            value_field = expression_result.output_field  # type: ignore[call-overload]
        else:
            if isinstance(filter_value, (list, tuple, set)) and any(
                isinstance(element, Expression) for element in filter_value
            ):
                # An expression inside a literal list can't be resolved by the value encoder - a
                # field would stringify it or fail with a TypeError.
                raise QueryError(
                    f"'{key}' - an Expression (F()/Case()/...) can't be used as an element "
                    "of a __in/__not_in list. Combine per-value Q(...) objects with '|' instead, "
                    "e.g. Q(field=F(...)) | Q(field=other_value)."
                )

        return filter_value, filter_value_joins, value_field

    @staticmethod
    def _get_subquery_filter_value(filter_value: Any, value_wrapper_refs: RecordedValueRefs | None = None) -> Any:
        """A query passed as a filter value, as the subquery the filter compares with
        (``field__in=Other.objects.filter(...)``). A bare queryset is narrowed to its model's
        primary key; a set operation to its combined rows' primary key.

        Args:
            filter_value: The filter value.
            value_wrapper_refs: The references being recorded, None when no plan is.

        Returns:
            A ``Subquery``, the built SELECT of a set operation's primary key, or ``filter_value``
            unchanged.
        """
        # Deferred import: hare.query.queryset itself imports from this module at module level
        # (Q/Expression/...), so a direct import here would be circular.
        from hare.query.queryset import QuerySet
        from hare.query.statements import AwaitableQuery

        if not isinstance(filter_value, (QuerySet, AwaitableQuery)):
            return filter_value
        # Imported only for a query value - an ordinary filter value loads no set-operation module.
        from hare.query.statements.select.combined_query import CombinedQuery

        if isinstance(filter_value, QuerySet):
            # The values it selects, else its primary key - or the rows it combines.
            filter_value = (
                filter_value._get_filter_value_query()
                if filter_value._combination is None
                else filter_value._get_compiler()
            )
        if isinstance(filter_value, CombinedQuery) and not filter_value._combines_values:
            return filter_value._get_field_values_query("pk", value_wrapper_refs)
        if isinstance(filter_value, AwaitableQuery):
            return Subquery(filter_value._get_filter_value_query())
        return filter_value

    @classmethod
    def _get_date_timestamp_comparand(
        cls, expression_context: ExpressionContext, key: str, term: Any, value_field: Field[Any] | None
    ) -> Any:
        """An expression value comparing a date with a timestamp - the date as the first moment of
        its day, as a date literal is (in the configured zone, a naive timestamp's wall clock).

        Args:
            expression_context: The context the kwarg is resolved against.
            key: The filter kwarg.
            term: The value's resolved term.
            value_field: The value's field.

        Returns:
            The term - a date promoted, a timestamp marked for the date column to be promoted.
        """
        field_name, __, lookup = key.partition("__")
        field_object = expression_context.model._meta.fields_map.get(field_name)
        if lookup not in DATE_TIMESTAMP_COMPARISON_LOOKUPS or field_object is None or value_field is None:
            return term
        filtered_field = cls._get_effective_field(field_object)
        compared_field = cls._get_effective_field(value_field)
        zone_name = Timezone.get_aware_zone_name()
        if isinstance(filtered_field, DatetimeField) and isinstance(compared_field, DateField):
            return DateAsTimestamp(term, zone_name)
        if isinstance(filtered_field, DateField) and isinstance(compared_field, DatetimeField):
            return TimestampComparand(term, zone_name)
        return term

    @staticmethod
    def _get_filter_column_count(model: type[Model], key: str) -> int:
        """The number of columns a filter kwarg compares - more than one for a composite primary
        key or a composite relation.

        Args:
            model: The model the kwarg is resolved against.
            key: The filter kwarg.

        Returns:
            The column count.
        """
        base_field_name = key.partition("__")[0]
        meta = model._meta
        if base_field_name == "pk":
            return len(meta.pk_attr_names)
        field_object = meta.fields_map.get(base_field_name)
        if base_field_name in meta.fk_fields or base_field_name in meta.o2o_fields:
            return max(len(getattr(field_object, "source_fields", ()) or ()), 1)
        if (
            base_field_name in meta.m2m_fields
            or base_field_name in meta.backward_fk_fields
            or base_field_name in meta.backward_o2o_fields
        ):
            related_model = cast("RelationalField[Model]", field_object).related_model
            return len(related_model._meta.pk_attr_names)
        return 1

    def _combine_modifier(self, modifier: QueryModifier, other: QueryModifier) -> QueryModifier:
        return modifier & other if self.connector == Connector.AND else modifier | other

    def _finalize_modifier(
        self, modifier: QueryModifier, expression_context: ExpressionContext, recorded_paths_mark: int = 0
    ) -> QueryModifier:
        """Applies this node's negation to the combined modifier of its kwargs or children.

        Args:
            modifier: The combined modifier.
            expression_context: The context this node is resolved in.
            recorded_paths_mark: The tracked to-many path position before this node, to roll back to
                when its JOINs go into a NOT EXISTS subquery.

        Returns:
            The modifier with the negation applied.
        """
        if modifier.joins and (self._is_negated or self._needs_double_negation_rewrite):
            tracker = AggregatedMultiValuedPaths.get_from(expression_context)
            if tracker is not None:
                tracker.discard_since(recorded_paths_mark)
        if not self._is_negated:
            if self._needs_double_negation_rewrite and modifier.joins:
                # An even number of negations over a relation: NOT (NOT EXISTS(...)) - the rows of
                # `q`, with the JOINs of `~q`.
                return ~self._negate_across_joins(modifier, expression_context)
            return modifier
        if not modifier.joins:
            # No relation was crossed: QueryModifier.__invert__() chooses between a plain NOT and
            # the NULL-safe IS NOT TRUE - a row whose column is NULL belongs to exclude()'s result.
            return ~modifier
        return self._negate_across_joins(modifier, expression_context)

    def _negate_across_joins(self, modifier: QueryModifier, expression_context: ExpressionContext) -> QueryModifier:
        """Negates a filter that crossed a relation as a correlated ``NOT EXISTS`` subquery: ``NOT
        (criterion)`` over a LEFT JOIN is UNKNOWN for a row with no related row and would drop it.
        The subquery reuses the joins and criterion already built, retargeted onto an aliased copy
        of the base table correlated by primary key (by every column for a model without one).
        """
        outer_table = expression_context.table
        negation_depth = modifier.negation_depth + 1
        depth_suffix = "" if negation_depth == 1 else str(negation_depth)
        inner_table = outer_table.as_(
            Identifiers.get_within_limit(f"{outer_table.get_table_name()}__negated{depth_suffix}")
        )
        inner_where = modifier.where_criterion.replace_table(outer_table, inner_table)
        correlation = KeyColumns.get_row_correlation(expression_context.model._meta, inner_table, outer_table)
        inner_query = QueryBuilder().from_(inner_table).select(Star())
        # Children crossing the same relation path each contribute the same join - it is joined
        # once, by join table.
        seen_join_tables: set[Table] = set()
        for join_table, join_criterion in modifier.joins:
            if join_table in seen_join_tables:
                continue
            seen_join_tables.add(join_table)
            inner_query = inner_query.join(join_table, how=JoinType.LEFT_OUTER).on(
                join_criterion.replace_table(outer_table, inner_table)
            )
        inner_query = inner_query.where(correlation & inner_where)
        return QueryModifier(where_criterion=Not(ExistsTerm(inner_query)), negation_depth=negation_depth)

    def _get_composite_relation_kwarg(
        self, expression_context: ExpressionContext, key: str, value: Any
    ) -> QueryModifier | None:
        """Resolves ``key=value`` naming a forward relation to a composite primary key as an AND of
        equalities on the relation's own key columns - no JOIN.

        Returns:
            The modifier, None for any other key.
        """
        from hare.query.queryset.queryset import QuerySet

        model = expression_context.model
        relation_name, _, lookup = key.partition("__")
        if lookup not in TO_MANY_RELATION_SHORTCUT_LOOKUPS:
            return None
        if relation_name not in model._meta.fk_fields and relation_name not in model._meta.o2o_fields:
            return None
        field_object = cast("RelationalField[Model]", model._meta.fields_map[relation_name])
        source_fields = tuple(field_object.source_fields)
        if len(source_fields) <= 1:
            return None
        if lookup in ("", "not") and value is None:
            lookup, value = Lookup.ISNULL, lookup == Lookup.EXACT
        if lookup in (Lookup.ISNULL, Lookup.NOT_ISNULL):
            # A composite key is null exactly when every key column is.
            is_null = bool(value) if lookup == Lookup.ISNULL else not bool(value)
            null_kwargs: list[dict[str, Any]] = [
                {f"{source_field}__isnull": is_null} for source_field in source_fields
            ]
            null_groups = [Q(**null_kwarg) for null_kwarg in null_kwargs]
            null_connector = Connector.AND if is_null else Connector.OR
            return Q.with_connector(null_connector, *null_groups).get_result(expression_context)
        if lookup in (Lookup.IN, Lookup.NOT_IN) and QuerySet._is_subquery_filter_value(value):  # noqa: SLF001
            source_columns: list[Term] = [
                expression_context.table[model._meta.fields_db_projection[source_field]]
                for source_field in source_fields
            ]
            modifier = Q._get_composite_key_subquery_in_modifier(expression_context, value, source_columns, key)
            return ~modifier if lookup == Lookup.NOT_IN else modifier
        to_field_names = [to_field_instance.model_field_name for to_field_instance in field_object.to_field_instances]
        key_q = KeyColumns.get_comparison_q(
            key,
            source_fields,
            to_field_names,
            lookup,
            value,
            instance_key_names=to_field_names,
            reads_outer_refs=True,
        )
        return key_q._with_filter_call_generation(self._filter_call_generation).get_result(expression_context)

    def _get_composite_pk_kwarg(
        self, expression_context: ExpressionContext, key: str, value: Any
    ) -> tuple[QueryModifier, RowListValueRef | None] | None:
        """``"pk"``/``"pk__in"`` on a composite-pk model - several column conditions at once,
        built by ``KeyColumns.get_primary_key_q()``, as the same ``.filter()`` kwarg is.

        Returns:
            The modifier and the reference of a ``pk__in`` list of rows, or None for any other key.
        """
        composite_pk_q = KeyColumns.get_primary_key_q(expression_context.model, key, value)
        if composite_pk_q is None:
            return None
        if key == "pk__in" and composite_pk_q.filters.get(key) is not None:
            return self._get_composite_pk_in_modifier(expression_context, composite_pk_q.filters[key])
        return composite_pk_q.get_result(expression_context), None

    @staticmethod
    def _get_composite_pk_in_modifier(
        expression_context: ExpressionContext, value_rows: list[tuple[Any, ...]]
    ) -> tuple[QueryModifier, RowListValueRef | None]:
        """``pk__in=`` on a composite-pk model as one row-value membership check -
        ``(a, b) IN ((1, 2), ...)``, bound as few parameters as the backend allows.

        Args:
            expression_context: The context the filter is resolved in.
            value_rows: The primary key tuples, already checked to match the key's shape.

        Returns:
            The modifier, and the reference a later query binds its rows through - None for rows
            not bound one parameter per value.
        """
        from hare.query.queryset import QuerySet

        model = expression_context.model
        meta = model._meta
        pk_attr = cast("tuple[str, ...]", meta.pk_attr)
        if QuerySet._is_subquery_filter_value(value_rows):  # noqa: SLF001
            subquery_modifier = Q._get_composite_key_subquery_in_modifier(
                expression_context, value_rows, Q._get_composite_pk_columns(expression_context), "pk__in"
            )
            return subquery_modifier, None
        if any(isinstance(component, (Expression, Term)) for row in value_rows for component in row):
            # An expression component has no bound value to put in a row list - one equality
            # group per row instead.
            groups = [Q(**dict(zip(pk_attr, row, strict=True))) for row in value_rows]
            return Q.with_connector(Connector.OR, *groups).get_result(expression_context), None
        columns = Q._get_composite_pk_columns(expression_context)
        encoded_rows = [
            tuple(
                None
                if component is None
                else expression_context.dialect.types.get_lookup_value(pk_field, component, model)
                for pk_field, component in zip(meta.pk_fields, row, strict=True)
            )
            for row in value_rows
        ]
        criterion = expression_context.dialect.filter_operators.get_row_membership_criterion(
            columns, encoded_rows, list(meta.pk_fields)
        )
        has_none = any(component is None for row in encoded_rows for component in row)
        modifier = QueryModifier(where_criterion=criterion, has_nullable_column=has_none)
        return modifier, None if has_none else Q._get_row_list_value_ref(criterion, encoded_rows, meta.pk_fields)

    @staticmethod
    def _get_row_list_value_ref(
        criterion: Criterion, encoded_rows: list[tuple[Any, ...]], fields: Iterable[Field[Any]]
    ) -> RowListValueRef | None:
        """The reference of key rows compared one parameter per value - ``(a, b) IN ((?, ?), ...)``
        holding the encoded values themselves.

        Args:
            criterion: The row membership criterion.
            encoded_rows: The rows' encoded values.
            fields: The key's fields.

        Returns:
            The reference, None for another form (a container binding the rows at once).
        """
        if not isinstance(criterion, ContainsCriterion) or not isinstance(criterion.container, Tuple):
            return None
        row_terms = criterion.container.values
        if len(row_terms) != len(encoded_rows):
            return None
        for row_term, encoded_row in zip(row_terms, encoded_rows, strict=True):
            if not isinstance(row_term, Tuple) or len(row_term.values) != len(encoded_row):
                return None
            for component_term, component in zip(row_term.values, encoded_row, strict=True):
                if not isinstance(component_term, ValueWrapper) or component_term.value is not component:
                    return None
        return RowListValueRef(criterion.container, tuple(fields))

    @staticmethod
    def _get_composite_pk_columns(expression_context: ExpressionContext) -> list[Term]:
        """The composite primary key columns of the resolved model, in key order.

        Args:
            expression_context: The context the filter is resolved in.

        Returns:
            The columns, each wrapped in its field's comparison cast where the dialect needs one.
        """
        meta = expression_context.model._meta
        dialect = expression_context.dialect
        columns: list[Term] = []
        for pk_field, column_name in zip(meta.pk_fields, KeyColumns.get_source_columns(meta), strict=True):
            column: Term = expression_context.table[column_name]
            if (function_cast := pk_field.get_function_cast(dialect)) is not None:
                column = function_cast(pk_field, column)
            columns.append(column)
        return columns

    @staticmethod
    def _get_composite_key_subquery_in_modifier(
        expression_context: ExpressionContext, value: Any, columns: list[Term], key: str
    ) -> QueryModifier:
        """A composite key compared against a query - ``(a, b) IN (SELECT a, b ...)``.

        Args:
            expression_context: The context the filter is resolved in.
            value: A queryset (its rows' primary key), a union of such querysets, a
                ``values()``/``values_list()`` query or a ``Subquery`` selecting the key columns.
            columns: The compared key columns, in key order.
            key: The filter kwarg name, for the error message.

        Returns:
            The modifier.

        Raises:
            QueryError: The query doesn't select as many columns as the key has.
        """
        from hare.query.queryset import QuerySet
        from hare.query.statements.select.combined_query import CombinedQuery

        pk_column_count = len(columns)
        if isinstance(value, QuerySet):
            value = (
                value._get_compiler()
                if value._selection is not None or value._combination is not None
                else value._get_primary_key_values_query()
            )
        joins: list[TableCriterionTuple] = []
        if isinstance(value, CombinedQuery) and not value._combines_values:
            subquery_term: Term = value._get_fields_values_query(value.model._meta.pk_attr_names)
        else:
            subquery = value if isinstance(value, Subquery) else Subquery(value)
            selected_names = subquery.get_selected_names()
            if selected_names is not None and len(selected_names) != pk_column_count:
                raise QueryError(
                    f"'{key}' compares the {pk_column_count} columns of a composite key, but "
                    f"the subquery selects {len(selected_names)} ({', '.join(selected_names)})"
                )
            subquery_result = subquery.get_result(expression_context)
            subquery_term = subquery_result.term
            joins = subquery_result.joins
        criterion = Tuple(*columns).isin(subquery_term)
        return QueryModifier(where_criterion=criterion, joins=joins)

    def _get_to_many_relation_shortcut_kwarg(
        self, expression_context: ExpressionContext, key: str, value: Any
    ) -> tuple[QueryModifier, RelatedKeyValueRef | None] | None:
        """Resolves a lookup on a to-many relation's own name (``tags=obj``, ``tags__in=[...]``,
        ``emps__isnull=True``) as the same lookup on the related primary key, joined like the other
        lookups through the relation. ``relation=None`` is ``relation__isnull=True``; a reverse
        relation to a composite primary key compares every key column.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.
            value: The filter value.

        Returns:
            The modifier and the reference a later query binds its value through (None when it
            can't); None if ``key`` isn't such a lookup, or compares composite primary key values of
            a many-to-many relation.

        Raises:
            QueryError: A composite primary key value isn't a model instance or a tuple of the key's
                length.
        """
        from hare.models import Model

        relation_name, _, lookup = key.partition("__")
        if lookup not in TO_MANY_RELATION_SHORTCUT_LOOKUPS:
            return None
        relation_field = expression_context.model._meta.fields_map.get(relation_name)
        if not isinstance(relation_field, (BackwardFKRelation, ManyToManyFieldInstance)):
            return None
        if lookup in ("", "not") and value is None:
            # Like Django, `relation=None` keeps the rows with no related row at all.
            isnull_kwarg: dict[str, Any] = {f"{relation_name}__isnull": lookup == Lookup.EXACT}
            isnull_q = Q(**isnull_kwarg)
            isnull_q._filter_call_generation = self._filter_call_generation
            return isnull_q.get_result(dataclasses.replace(expression_context, value_wrapper_refs=None)), None
        is_many_to_many = isinstance(relation_field, ManyToManyFieldInstance)
        target_pk_attr_names = relation_field.related_model._meta.pk_attr_names
        if not is_many_to_many and len(target_pk_attr_names) > 1 and lookup not in (Lookup.ISNULL, Lookup.NOT_ISNULL):
            composite_q = KeyColumns.get_comparison_q(
                key,
                tuple(f"{relation_name}__{pk_attr_name}" for pk_attr_name in target_pk_attr_names),
                target_pk_attr_names,
                lookup,
                value,
            )._with_filter_call_generation(self._filter_call_generation)
            return composite_q.get_result(dataclasses.replace(expression_context, value_wrapper_refs=None)), None
        if not relation_field.is_multi_valued:
            return None
        if is_many_to_many and lookup in MANY_TO_MANY_EXISTS_LOOKUPS:
            return None
        given_value = value
        converts_lists = lookup in (Lookup.IN, Lookup.NOT_IN)
        if isinstance(value, Model):
            value = value.pk
        elif converts_lists and isinstance(value, (list, tuple, set)):
            value = [element.pk if isinstance(element, Model) else element for element in value]
        # A plain value (not a query or an expression) binds in a plan, through its key.
        is_plain_value = not isinstance(given_value, (Plannable, Term))
        relation_path = self._get_relation_path(expression_context, relation_name)
        if is_many_to_many and relation_path not in (expression_context.select_related_extra_conditions or {}):
            # The through table's link column is the related primary key - and its JOIN already
            # holds only links to rows the related model's default scope shows - so, like Django,
            # the related table itself isn't joined.
            lookup_info = self._get_lookup_info(expression_context, key)
            filter_value, value_joins, __ = self._get_filter_value(expression_context, key, value)
            _, _, relation_joins = self._get_relation_joins(
                expression_context, relation_name, expression_context.table
            )
            through_join = relation_joins[0]
            criterion, _, _, _, _, op, encoded_value = self._process_filter_kwarg(
                expression_context,
                lookup_info,
                filter_value,
                expression_context.table,
                join_table=through_join[0],
            )
            modifier = QueryModifier(joins=value_joins) & QueryModifier(
                where_criterion=criterion, joins=[through_join]
            )
            through_ref = None
            if is_plain_value and filter_value is value:
                value_ref = self._get_value_ref(
                    expression_context,
                    criterion,
                    op,
                    filter_value,
                    encoded_value,
                    relation_field,
                    cast("FieldLookup", lookup_info.field_lookup).value_encoder,
                )
                if value_ref is not None:
                    through_ref = RelatedKeyValueRef(value_ref, Model, converts_lists)
            return modifier, through_ref
        if len(target_pk_attr_names) > 1 and lookup not in (Lookup.ISNULL, Lookup.NOT_ISNULL):
            return None
        # For IS [NOT] NULL: the LEFT JOIN found no row exactly when any one of its primary key
        # columns is NULL - or, for a related model without a primary key, its key column to
        # this row.
        target_meta = relation_field.related_model._meta
        target_column_name = (
            target_pk_attr_names[0]
            if target_meta.has_primary_key
            else target_meta.fields_db_projection_reverse[
                cast("BackwardFKRelation[Any]", relation_field).relation_source_fields[0]
            ]
        )
        nested_key = f"{relation_name}__{target_column_name}"
        if lookup:
            nested_key = f"{nested_key}__{lookup}"
        nested_q = Q(**{nested_key: value})
        nested_q._filter_call_generation = self._filter_call_generation
        nested_refs: RecordedValueRefs | None = (
            [] if is_plain_value and expression_context.value_wrapper_refs is not None else None
        )
        modifier = nested_q.get_result(dataclasses.replace(expression_context, value_wrapper_refs=nested_refs))
        nested_ref = None
        if nested_refs is not None and len(nested_refs) == 1 and nested_refs[0][1] is not None:
            nested_ref = RelatedKeyValueRef(nested_refs[0][1], Model, converts_lists)
        return modifier, nested_ref

    @staticmethod
    def _get_recorded_paths_mark(expression_context: ExpressionContext) -> int:
        tracker = AggregatedMultiValuedPaths.get_from(expression_context)
        return tracker.get_mark() if tracker is not None else 0

    def _get_own_expression_context(self, expression_context: ExpressionContext) -> ExpressionContext:
        """The context this node's kwargs and children resolve in. A negated node's JOINs go into its
        ``NOT EXISTS`` subquery, so they don't take a to-many relation's JOIN of the enclosing
        query.

        Args:
            expression_context: The context the node is resolved in.

        Returns:
            The context.
        """
        if (self._is_negated or self._needs_double_negation_rewrite) and (
            expression_context.multi_valued_join_generations is not None
        ):
            return dataclasses.replace(expression_context, multi_valued_join_generations=None)
        return expression_context

    def _get_kwargs(self, expression_context: ExpressionContext) -> QueryModifier:
        expression_context = self._get_own_expression_context(expression_context)
        modifier = QueryModifier()
        recorded_paths_mark = self._get_recorded_paths_mark(expression_context)
        own_expression_context = expression_context
        for filter_key, filter_value in self.filters.items():
            self._raise_if_lookup_unsupported(own_expression_context, filter_key)
            # A constant filter's value is part of the plan key - nothing is recorded for it.
            recording = (
                None
                if self.is_constant_filter(filter_key, filter_value)
                else own_expression_context.value_wrapper_refs
            )
            raw_key, raw_value = self._get_json_path_mirrored_kwarg(own_expression_context, filter_key, filter_value)
            expression_context, annotation_name = self._get_json_path_expression_context(
                own_expression_context, raw_key
            )
            if annotation_name is not None:
                value, value_joins, value_field = self._get_filter_value(expression_context, raw_key, raw_value)
                filter_modifier, custom_ref = self._get_custom_kwarg(
                    expression_context,
                    annotation_name,
                    raw_key,
                    value,
                    expression_context.table,
                    compared_value_field=value_field,
                    records_value=recording is not None and value is raw_value,
                )
                if recording is not None and not isinstance(raw_value, Expression):
                    # An expression value recorded its own values while it was resolved
                    # (_get_filter_value()).
                    recording.append((raw_key, custom_ref))
            else:
                lookup_info = self._get_lookup_info(expression_context, raw_key)
                expanded = self._get_expanded_kwarg(expression_context, lookup_info, raw_value)
                if expanded is not None:
                    expanded_modifier, expanded_ref = expanded
                    modifier = self._combine_modifier(modifier, expanded_modifier)
                    if recording is not None:
                        recording.append((raw_key, expanded_ref))
                    continue
                compared_lookup_info, value = self._get_relation_filter_params(
                    expression_context, lookup_info, raw_value
                )
                value, value_joins, __ = self._get_filter_value(expression_context, raw_key, value)
                # A lookup on a relation itself compares the key column or the related keys, and a
                # query or expression value is resolved - a later value converts differently, so
                # only the value given as it is binds in a plan. A forward relation compared through
                # its key column binds a plain key value as its column does: the plan key holds the
                # value's type, so a later value is a key value too - not so a list, which may hold
                # instances whatever its length.
                bindable = value is raw_value and (
                    compared_lookup_info is lookup_info or not isinstance(value, (list, tuple, set))
                )
                filter_modifier, ref = self._get_regular_kwarg(
                    expression_context, compared_lookup_info, value, expression_context.table, bindable=bindable
                )
                if recording is None or isinstance(value, Subquery) or isinstance(raw_value, Expression):
                    # A subquery or an expression value recorded its own values while it was
                    # resolved (_get_filter_value(), Subquery.get_result()).
                    pass
                elif self._is_constant_list_condition(raw_key, raw_value):
                    # An empty __in/__not_in list, or one of none but None - a constant condition
                    # with no value to bind.
                    pass
                elif self._is_union_query(raw_value):
                    # A union recorded its own values while it was built into the filter
                    # (_get_subquery_filter_value()).
                    pass
                elif isinstance(raw_value, RawSQL) and bindable:
                    # The SQL text is part of the plan key; its parameters are the values.
                    recording.extend((raw_key, LiteralValueRef(param)) for param in raw_value.params)
                else:
                    recording.append((raw_key, ref))
            if value_joins:
                # An expression value crossing a relation of its own (F("relation__field")) - its
                # joins come with this kwarg's modifier, as its term does.
                filter_modifier = QueryModifier(joins=value_joins) & filter_modifier

            modifier = self._combine_modifier(modifier, filter_modifier)
        return self._finalize_modifier(modifier, own_expression_context, recorded_paths_mark)

    @staticmethod
    def _get_lookup_info(expression_context: ExpressionContext, key: str) -> LookupInfo:
        """The description of a filter key of the model the context resolves.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field or relation, or a lookup its field doesn't have.
        """
        path_prefix = expression_context.select_related_path_prefix
        label = f"Unknown filter param '{path_prefix}__{key}'" if path_prefix else None
        return expression_context.model._meta._get_lookup_info(key, label)

    @staticmethod
    def _crosses_relation(lookup_info: LookupInfo) -> bool:
        """Whether a key goes on through its first relation, rather than comparing the field or
        relation it names.

        Args:
            lookup_info: The key's description.

        Returns:
            True for a key resolved on the related model.
        """
        compared_relations = 1 if lookup_info.target == LookupTarget.RELATION else 0
        return len(lookup_info.relations) > compared_relations

    def _get_expanded_kwarg(
        self, expression_context: ExpressionContext, lookup_info: LookupInfo, value: Any
    ) -> tuple[QueryModifier, RowListValueRef | RelatedKeyValueRef | None] | None:
        """Resolves a key that becomes more than one comparison - a composite primary key, a
        relation to a composite key, a forward relation whose target a default scope may hide,
        or a lookup on a to-many relation itself.

        Args:
            expression_context: The context the filter is resolved in.
            lookup_info: The key's description.
            value: The filter value.

        Returns:
            The modifier and the reference a later query binds its value through (None when it
            can't), or None when the key is a single comparison.
        """
        key = lookup_info.key
        if lookup_info.target == LookupTarget.PRIMARY_KEY and not lookup_info.relations:
            return self._get_composite_pk_kwarg(expression_context, key, value)
        if lookup_info.target != LookupTarget.RELATION or len(lookup_info.relations) != 1:
            return None
        relation = cast("RelationalField[Model]", lookup_info.relations[0])
        meta = expression_context.model._meta
        if relation.model_field_name in meta.fk_fields or relation.model_field_name in meta.o2o_fields:
            modifier = self._get_forward_relation_isnull_kwarg(expression_context, key, value)
            if modifier is None and len(relation.source_fields) > 1:
                modifier = self._get_composite_relation_kwarg(expression_context, key, value)
            return None if modifier is None else (modifier, None)
        return self._get_to_many_relation_shortcut_kwarg(expression_context, key, value)

    @staticmethod
    def _get_annotation_name(expression_context: ExpressionContext, key: str) -> str | None:
        """The annotation a filter key starts with.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Returns:
            The annotation's name - the longest one when several match - or None.
        """
        annotations = expression_context.annotations
        if not annotations:
            return None
        segments = key.split("__")
        for end in range(len(segments), 0, -1):
            name = "__".join(segments[:end])
            if name in annotations:
                return name
        return None

    @staticmethod
    def _get_unknown_key_error(expression_context: ExpressionContext, key: str) -> FieldError:
        """The error of a filter key naming nothing the model or the query has.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Returns:
            The error.
        """
        meta = expression_context.model._meta
        allowed = sorted(meta.fields | meta.fetch_fields | set(expression_context.annotations))
        path_prefix = expression_context.select_related_path_prefix
        full_key = f"{path_prefix}__{key}" if path_prefix else key
        return FieldError(
            f"Unknown filter param '{full_key}': {expression_context.model.__name__} has no field or lookup "
            f"'{key}'. Allowed base values are {allowed}"
        )

    @staticmethod
    def _raise_if_lookup_unsupported(expression_context: ExpressionContext, key: str) -> None:
        """Rejects a lookup the query's dialect or connection doesn't run, before any SQL is built.
        Keys on an annotation are left to the annotation; so is everything while no connection is
        chosen.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Raises:
            FieldError: The key names no field or relation, or a lookup its field doesn't have.
            UnSupportedError: The dialect or the connection doesn't run the lookup.
        """
        if expression_context.connection is None or Q._get_annotation_name(expression_context, key) is not None:
            return
        lookup_info = expression_context.model._meta.get_lookup_info(key)
        dialect = expression_context.dialect
        lookup_name = f"__{lookup_info.lookup}" if lookup_info.lookup else "equality"
        if dialect.supports_lookup(lookup_info):
            required_feature = LOOKUP_REQUIRED_FEATURES.get(lookup_info.lookup)
            if required_feature is None or getattr(expression_context.connection.features, required_feature):
                return
            raise UnSupportedError(
                f"{expression_context.model.__name__}.objects.filter({key}=...) can't run on the "
                f"{expression_context.connection.connection_name!r} connection: the {lookup_name} lookup needs "
                f"features.{required_feature}, which the connection doesn't have"
            )
        lookup_name = f"__{lookup_info.lookup}" if lookup_info.lookup else "equality"
        if lookup_info.dialects is not None and dialect.name not in lookup_info.dialects:
            reason = f"its field only exists on {', '.join(sorted(lookup_info.dialects))}"
        elif lookup_info.requires_extension is not None and not dialect.supports_extensions:
            reason = f"it needs the {lookup_info.requires_extension} extension"
        else:
            reason = f"the {dialect.name} dialect doesn't implement the {lookup_name} lookup"
        raise UnSupportedError(
            f"{expression_context.model.__name__}.objects.filter({key}=...) can't run on {dialect.name}: {reason}"
        )

    @staticmethod
    def _get_json_comparand(term: Term, value_field: Field[Any] | None, dialect: Dialect) -> Term:
        """An expression value compared with the value at a JSON path, as a JSON value.

        Args:
            term: The value's resolved term.
            value_field: The value's field.
            dialect: The dialect the comparison renders for.

        Returns:
            The term as a JSON value, or itself when it holds one already or its type is unknown.
        """
        # Local import: hare.query.functions.json imports the expressions package this module is in.
        from hare.query.functions.json import JSONObject

        effective_field = Q._get_effective_field(value_field) if value_field is not None else None
        if effective_field is None or isinstance(effective_field, JSONField):
            return term
        json_term, value_type = JSONObject.get_json_value(term, effective_field, dialect)
        return JsonComparand(json_term, value_type, is_aware=Timezone.get_use_tz())

    @staticmethod
    def _get_json_path_mirrored_kwarg(expression_context: ExpressionContext, key: str, value: Any) -> tuple[str, Any]:
        """A comparison of a field with the value at a JSON path (``number__gt=F("data__rank")``)
        as the same comparison of the path with the field (``data__rank__lt=F("number")``), which
        compares JSON values.

        Args:
            expression_context: The node's context.
            key: The filter key.
            value: The filter value.

        Returns:
            The key and value to resolve.
        """
        if not isinstance(value, F):
            return key, value
        meta = expression_context.model._meta
        field_name, __, lookup = key.partition("__")
        field = meta.fields_map.get(field_name)
        if (
            field is None
            or field_name in meta.fetch_fields
            or isinstance(Q._get_effective_field(field), JSONField)
            or lookup not in MIRRORED_COMPARISON_LOOKUPS
            or value.name.partition("__")[0] in meta.fetch_fields
            or not isinstance(value.get_result(expression_context).output_field, JSONPathField)  # type: ignore[call-overload]
        ):
            return key, value
        mirrored_lookup = MIRRORED_COMPARISON_LOOKUPS[lookup]
        return (f"{value.name}__{mirrored_lookup}" if mirrored_lookup else value.name), F(field_name)

    @staticmethod
    def _get_json_base_field(expression_context: ExpressionContext, name: str) -> JSONField[Any] | None:
        """The JSON field of a model field or annotation a filter key starts with.

        Args:
            expression_context: The node's context.
            name: The key's first segment.

        Returns:
            The field, or None when the name isn't a JSON field or JSON-valued annotation.
        """
        annotation = expression_context.annotations.get(name)
        if annotation is not None:
            if not isinstance(annotation, Expression):
                return None
            # Resolved only to read its field - its values are recorded where it is built.
            field = annotation.get_result(  # type: ignore[call-overload]
                dataclasses.replace(expression_context, value_wrapper_refs=None)
            ).output_field
        else:
            field = expression_context.model._meta.fields_map.get(name)
        effective_field = Q._get_effective_field(field) if field is not None else None
        return effective_field if isinstance(effective_field, JSONField) else None

    @classmethod
    def _get_json_path_expression_context(
        cls, expression_context: ExpressionContext, key: str
    ) -> tuple[ExpressionContext, str | None]:
        """The context a filter key resolves in, and the annotation it starts with. A key reading a
        path into a JSON field or JSON-valued annotation (``data__owner__name``,
        ``data__score__gt``) resolves as a filter on ``F()`` of that path.

        Args:
            expression_context: The node's context.
            key: The filter key.

        Returns:
            The context - with the path as an annotation for such a key - and the annotation the key
            starts with, None for a key of the model.
        """
        annotation_name = cls._get_annotation_name(expression_context, key)
        field_name, __, path = key.partition("__")
        if not path or (json_field := cls._get_json_base_field(expression_context, field_name)) is None:
            return expression_context, annotation_name
        if path in FieldLookups.get(json_field):
            # A lookup of the whole value.
            return expression_context, annotation_name
        path_segments = path.split("__")
        path_lookup_names = JsonPathLookups.get_lookup_names()
        if len(path_segments) == 1 and path_segments[0] in path_lookup_names:
            # A lookup name right after the field is that lookup of the whole value, never a key.
            return expression_context, annotation_name
        path_name = key
        if len(path_segments) > 1 and path_segments[-1] in path_lookup_names:
            path_name = key.rpartition("__")[0]
        return (
            dataclasses.replace(
                expression_context, annotations={**expression_context.annotations, path_name: F(path_name)}
            ),
            path_name,
        )

    def _get_children(self, expression_context: ExpressionContext) -> QueryModifier:
        expression_context = self._get_own_expression_context(expression_context)
        modifier = QueryModifier()
        recorded_paths_mark = self._get_recorded_paths_mark(expression_context)
        for node in self.children:
            node_modifier = node.get_result(expression_context)
            modifier = self._combine_modifier(modifier, node_modifier)
        return self._finalize_modifier(modifier, expression_context, recorded_paths_mark)

    def get_result(
        self,
        expression_context: ExpressionContext,
    ) -> QueryModifier:
        """
        Resolves the logical Q chain into the parts of a SQL statement.

        Args:
            expression_context: Carries the model and virtual SQL table (to allow self referential
                joins) this Q Expression should be resolved on.
        """
        if self.expression is not None:
            return self._get_expression(expression_context)
        if self.filters:
            return self._get_kwargs(expression_context)
        return self._get_children(expression_context)

    def _get_expression(self, expression_context: ExpressionContext) -> QueryModifier:
        """Resolves a node matching on a boolean expression (``Exists(...)``).

        Args:
            expression_context: The context this node is resolved against.

        Returns:
            The expression as the criterion - in HAVING when it reads an aggregate.
        """
        result = cast("Exists", self.expression).get_result(expression_context)
        criterion = cast("Criterion", result.term)
        if self._is_negated:
            criterion = Not(criterion)
        if criterion.contains_aggregate:
            return QueryModifier(having_criterion=criterion, joins=result.joins)
        return QueryModifier(where_criterion=criterion, joins=result.joins)
