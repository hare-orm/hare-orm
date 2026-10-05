from __future__ import annotations

import dataclasses
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import QueryError
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.enums import Connector
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.constants import (
    LIST_LOOKUP_SUFFIXES,
)
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.f import F
from hare.query.expressions.subqueries.exists import Exists
from hare.query.expressions.value import Value
from hare.query.filters.resolution.filter_kwargs import FilterKwargs
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.filters.resolution.to_many_filters import ToManyFilters
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.hashable_values import HashableValues
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.not_criterion import Not

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


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
        "_plan_origin",
        "_value_origins",
    )

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("connector", PlanPartType.KEY),
        ("_is_negated", PlanPartType.KEY),
        ("_needs_double_negation_rewrite", PlanPartType.KEY),
        ("_filter_call_generation", PlanPartType.KEY),
        ("children", PlanPartType.EXPRESSIONS),
        ("expression", PlanPartType.EXPRESSION),
        ("filters", PlanPartType.FILTERS),
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
                    value = FilterValues.get_literal_value(value)
                if key.endswith(LIST_LOOKUP_SUFFIXES):
                    value = FilterValues.get_list_lookup_value(key, value)
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
        inverted = Q.with_connector(self.connector, *self.children, **self.filters)
        inverted.expression = self.expression
        # A fresh Q() always starts _is_negated=False - toggling THIS instance's own state
        # (not q's just-initialized one) is what makes ~~q restore the original rather than
        # staying negated forever.
        inverted._is_negated = not self._is_negated
        # The generation .filter()/.exclude() stamped survives the negation.
        inverted._filter_call_generation = self._filter_call_generation
        # Negating an already negated Q. For a condition over a to-many relation `~q` is a
        # correlated NOT EXISTS, so `~~q` can't fall back to the plain JOIN - marked here, decided
        # in _finalize_modifier(), where the model is known. Once set, it stays set.
        inverted._needs_double_negation_rewrite = self._needs_double_negation_rewrite or self._is_negated
        # Its values are this condition's (PlanOrigins).
        inverted._plan_origin = self
        return inverted

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

    def names_generic_field(self, declared_names: set[str], model: type[Model] | None = None) -> bool:
        """Whether a filter key of this node may name a generic foreign key - one of its parts is
        the name of one, and with ``model`` known, the part is a generic foreign key of the model
        the key reaches there.

        Args:
            declared_names: The names of the generic foreign keys of every model.
            model: The model the keys start from, None where unknown.

        Returns:
            True when a key has such a part.
        """
        for key in self.filters:
            segments = key.split("__")
            if declared_names.isdisjoint(segments):
                continue
            if model is None or self.path_names_generic_field(model, segments):
                return True
        return False

    @staticmethod
    def path_names_generic_field(model: type[Model], segments: list[str]) -> bool:
        """Whether a key's path, walked from ``model`` through its relations, reaches a generic
        foreign key.

        Args:
            model: The model the path starts from.
            segments: The path's parts.

        Returns:
            True when a part is a generic foreign key of the model reached there.
        """
        current_model = model
        for segment in segments:
            if segment in current_model._meta.generic_foreign_key_fields:
                return True
            relation = current_model._meta.fields_map.get(segment)
            if not isinstance(relation, RelationalField):
                return False
            current_model = relation.related_model
        return False

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
            tuple(
                sorted(
                    ((key, HashableValues.get_hashable_value(value)) for key, value in self.filters.items()), key=repr
                )
            ),
            id(self.expression) if self.expression is not None else None,
        )

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
        # Copied node by node - the filters are this node's, set up already; a node holds filters or
        # children, never both.
        copy = Q.__new__(Q)
        copy.children = tuple([child._with_filter_call_generation(generation) for child in self.children])
        copy.filters = dict(self.filters)
        copy.connector = self.connector
        copy.expression = self.expression
        copy._is_negated = self._is_negated
        copy._filter_call_generation = generation
        # See __invert__()'s own docstring - must survive stamping the same way _is_negated does,
        # or a `~Q(rel__x)` argument re-negated by exclude()'s own `~stamped` (QuerySet.
        # _append_filters(), stamping happens BEFORE that second negation) would lose the mark.
        copy._needs_double_negation_rewrite = self._needs_double_negation_rewrite
        # Its values are this condition's (PlanOrigins).
        copy._plan_origin = self
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
        if modifier.joins and modifier.having_criterion and (self._is_negated or self._needs_double_negation_rewrite):
            # An aggregate can't go into a NOT EXISTS subquery: the condition is negated in HAVING, its
            # JOINs staying in the query - right for a forward relation, which repeats no row.
            tracker = AggregatedMultiValuedPaths.get_from(expression_context)
            to_many_paths = tracker.get_paths_since(recorded_paths_mark) if tracker is not None else []
            if to_many_paths:
                raise QueryError(
                    f"A negated condition (exclude()/~Q) combining an aggregate annotation with a lookup across "
                    f"the to-many relation {', '.join(dict.fromkeys(to_many_paths))} can't be built: the "
                    "aggregate's groups would be split by the related rows. Move the relation's lookup into an "
                    "Exists()/Subquery()."
                )
            return ~modifier if self._is_negated else modifier
        if modifier.joins and (self._is_negated or self._needs_double_negation_rewrite):
            tracker = AggregatedMultiValuedPaths.get_from(expression_context)
            if tracker is not None:
                tracker.discard_since(recorded_paths_mark)
        if not self._is_negated:
            if self._needs_double_negation_rewrite and modifier.joins:
                # An even number of negations over a relation: NOT (NOT EXISTS(...)) - the rows of
                # `q`, with the JOINs of `~q`.
                return ~ToManyFilters.negate_across_joins(modifier, expression_context)
            return modifier
        if not modifier.joins:
            # No relation was crossed: QueryModifier.__invert__() chooses between a plain NOT and
            # the NULL-safe IS NOT TRUE - a row whose column is NULL belongs to exclude()'s result.
            return ~modifier
        return ToManyFilters.negate_across_joins(modifier, expression_context)

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
            declared_names = GenericForeignKeyFieldInstance.declared_names
            if declared_names and self.names_generic_field(declared_names, expression_context.model):
                # A condition built outside filter() - in When(), an aggregate's filter= - may
                # name a generic foreign key.
                from hare.query.generic_foreign_keys.generic_foreign_key_filters import GenericForeignKeyFilters

                rewritten = GenericForeignKeyFilters.rewrite_q(expression_context.model, self)
                if rewritten is not self:
                    return rewritten.get_result(expression_context)
            return FilterKwargs.get_kwargs(self, expression_context)
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
