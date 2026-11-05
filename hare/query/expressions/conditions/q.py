from __future__ import annotations

import dataclasses
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.ddl.conditions.field_condition import FieldCondition
from hare.exceptions import QueryError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.generated_field import GeneratedField
from hare.query.enums import Connector
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.constants import (
    LIST_LOOKUP_SUFFIXES,
)
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.structure.declarations import ExpressionChild
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.subqueries.exists import Exists
from hare.query.expressions.value import Value
from hare.query.filters.resolution.filter_kwargs import FilterKwargs
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.filters.resolution.to_many_filters import ToManyFilters
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.hashable_values import HashableValues
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.enums import PlanPartType
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.not_criterion import Not
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.models import Model


class Q(Plannable, FieldCondition):
    """A condition to filter by, composed from smaller ones. ``Q(...)`` joins its conditions with AND;
    ``Q.with_connector(Connector.OR, ...)`` joins them with OR. The constructor takes nothing but
    conditions, so a filter on any field name fits in it.

    Args:
        conditions: Inner ``Q`` expressions or ``Exists(...)`` conditions.
        filters: The filters of this node.
    """

    __slots__ = (
        "_children",
        "_extended_from",
        "_extension",
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
        # Read through ``children``.
        ("_children", PlanPartType.NONE),
        ("_extended_from", PlanPartType.NONE),
        ("_extension", PlanPartType.NONE),
        ("expression", PlanPartType.EXPRESSION),
        ("filters", PlanPartType.FILTERS),
    )

    def __init__(self, *conditions: Q | Exists, **filters: Any) -> None:
        self._set_up(Connector.AND, conditions, filters)

    @classmethod
    def from_expression(cls, expression: Expression) -> Q:
        """Match a declared boolean expression without changing positional filter arguments.

        The expression is bound in the consuming query's context. Its SQL must be a
        criterion or its declared output field must be boolean; other scalar results
        raise QueryError when the condition is prepared. Joins and aggregate placement
        remain those of the expression.
        """
        condition = cls()
        condition.expression = expression
        return condition

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
        #: A boolean expression leaf, without kwargs or children.
        self.expression: Expression | None = None
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
                value = FilterValues.get_detached_value(value)
                if type(value) is Value:
                    value = FilterValues.get_literal_value(value)
                if key.endswith(LIST_LOOKUP_SUFFIXES):
                    value = FilterValues.get_list_lookup_value(key, value)
                filters[key] = value
            kwargs = filters
        #: The conditions this Q joins by its connector (``children``); None for a ``get_extended()`` Q
        #: until they are first read.
        self._children: tuple[Q, ...] | None = tuple(children)
        #: The Q a ``get_extended()`` Q extends and the condition it adds - None once its children are read.
        self._extended_from: Q | None = None
        self._extension: Q | None = None
        #: The filters of this Q, as passed to Q(...)/`.filter(...)`: {"id": 5}, {"name__icontains": "x"}.
        self.filters: dict[str, Any] = kwargs
        #: Whether this Q joins its children by AND or by OR.
        self.connector = connector
        self._is_negated = False
        #: The number of the ``.filter()``/``.exclude()`` call the condition came from - a lookup over a
        #: to-many relation from another call gets a JOIN of its own; 0 for none.
        self._filter_call_generation = 0
        #: Whether the Q is a negation of a negated one: over a to-many relation ``~q`` is a correlated
        #: ``NOT EXISTS``, so ``~~q`` can't fall back to the plain JOIN - decided once the model is known.
        #: Once set, it stays set.
        self._needs_double_negation_rewrite = False

    def __and__(self, other: Q | Exists) -> Q:
        """The conditions of both joined by AND - ``q1 & q2``.

        Raises:
            QueryError: ``other`` is neither a ``Q`` nor an ``Exists``.
        """
        if not isinstance(other, (Q, Exists)):
            raise QueryError("AND operation requires a Q node")
        if self.extends_with(Connector.AND):
            return self.get_extended(Connector.AND, other)
        return Q(self, other)

    def __or__(self, other: Q | Exists) -> Q:
        """The conditions of both joined by OR - ``q1 | q2``.

        Raises:
            QueryError: ``other`` is neither a ``Q`` nor an ``Exists``.
        """
        if not isinstance(other, (Q, Exists)):
            raise QueryError("OR operation requires a Q node")
        if self.extends_with(Connector.OR):
            return self.get_extended(Connector.OR, other)
        return Q.with_connector(Connector.OR, self, other)

    def get_extended(self, connector: Connector, other: Q | Exists) -> Q:
        """A ``Q`` of this one's children and one more condition, keeping only the two - its children
        are gathered when first read: a chain ``q |= ...`` of thousands of conditions is built in
        linear time, not by copying every child at every step.

        Args:
            connector: The connector of this ``Q``.
            other: The condition joined.

        Returns:
            The ``Q``.
        """
        extended = Q.with_connector(connector)
        extended._children = None
        extended._extended_from = self
        extended._extension = Q(other) if isinstance(other, Exists) else other
        return extended

    @property
    def children(self) -> tuple[Q, ...]:
        """The conditions this Q joins by its connector - a ``get_extended()`` Q's gathered along its
        chain the first time they are read."""
        children = self._children
        if children is None:
            extensions: list[Q] = []
            node: Q = self
            while node._children is None:
                extensions.append(cast("Q", node._extension))
                node = cast("Q", node._extended_from)
            children = self._children = (*node._children, *reversed(extensions))
            self._extended_from = self._extension = None
        return children

    @children.setter
    def children(self, children: tuple[Q, ...]) -> None:
        self._children = children
        self._extended_from = self._extension = None

    def extends_with(self, connector: Connector) -> bool:
        """Whether a condition joined to this ``Q`` by ``connector`` joins its children instead - a
        chain ``q1 | q2 | q3 ...`` stays one ``Q`` of every condition, never a ``Q`` nested as deep as
        the chain is long.

        Args:
            connector: The connector of the join.

        Returns:
            True when this ``Q`` only joins its children by ``connector``.
        """
        return (
            self.connector == connector
            # An extended Q has children - not gathered here, which would copy them at every step.
            and (self._children is None or bool(self._children))
            and not self.filters
            and self.expression is None
            and not self._is_negated
            and not self._filter_call_generation
            and not self._needs_double_negation_rewrite
        )

    def __rand__(self, other: Exists) -> Q:
        return Q(other, self)

    def __ror__(self, other: Exists) -> Q:
        return Q.with_connector(Connector.OR, other, self)

    def __invert__(self) -> Q:
        """The negated condition - ``~q``; ``~~q`` is the original again (``_needs_double_negation_rewrite``
        marks it)."""
        inverted = self.with_connector(self.connector, *self.children, **self.filters)
        inverted.expression = self.expression
        # A fresh Q() always starts _is_negated=False - toggling THIS instance's own state
        # (not q's just-initialized one) is what makes ~~q restore the original rather than
        # staying negated forever.
        inverted._is_negated = not self._is_negated
        # The generation .filter()/.exclude() stamped survives the negation.
        inverted._filter_call_generation = self._filter_call_generation
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
        if self.expression is not None and not isinstance(self.expression, Exists):
            return f"{self.get_negation_prefix()}Q.from_expression({self.expression!r})"
        arguments = [repr(child) for child in self.children]
        if self.expression is not None:
            arguments.append(repr(self.expression))
        arguments += [f"{key}={value!r}" for key, value in self.filters.items()]
        if self.connector == Connector.OR:
            return f"{self.get_negation_prefix()}Q.with_connector({', '.join(['Connector.OR', *arguments])})"
        return f"{self.get_negation_prefix()}Q({', '.join(arguments)})"

    def names_generic_field(self, model: type[Model]) -> bool:
        """Whether a filter key of this node reaches a generic foreign key of the model.

        Args:
            model: The model the keys start from.

        Returns:
            True when a key names a generic foreign key, directly or through relations.
        """
        from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths

        return any(GenericForeignKeyPaths.get_generic_path(model, key) is not None for key in self.filters)

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
        from hare.query.expressions.structure.expression_traversal import ExpressionTraversal

        return {
            reference.path.partition("__")[0]
            for reference in ExpressionTraversal.get_field_references(self)
            if not reference.outer_scopes
        }

    def get_expression_children(self) -> tuple[ExpressionChild, ...]:
        return (
            ExpressionChild("children", self.children),
            ExpressionChild("expression", self.expression),
            ExpressionChild("filters", self.filters, ArgumentRole.FILTERS),
        )

    def rebuild_expression(self, children: tuple[ExpressionChild, ...]) -> Q:
        result = copy(self)
        for child in children:
            setattr(result, cast("str", child.argument), child.value)
        return result

    def with_renamed_field(self, old_name: str, new_name: str) -> Q:
        """A copy reading the field ``new_name`` wherever this condition reads ``old_name``.

        Args:
            old_name: The field's former name.
            new_name: The field's new name.

        Returns:
            The renamed copy - this tree itself is left unchanged.
        """

        from hare.ddl.expressions.expression_field_renaming import ExpressionFieldRenaming

        return ExpressionFieldRenaming.get_renamed(self, old_name, new_name)

    def __bool__(self) -> bool:
        if self.filters or self.expression is not None:
            return True
        return any(self.children)

    def negate(self) -> None:
        """Negates the Q in place - as ``~q`` does to a copy, ``_needs_double_negation_rewrite`` included."""
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
        condition = copy(self)
        condition.children = tuple(child._with_filter_call_generation(generation) for child in self.children)
        condition.filters = dict(self.filters)
        condition._filter_call_generation = generation
        # Its values are this condition's (PlanOrigins).
        condition._plan_origin = self
        return condition

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

    def _get_node_expression_context(self, expression_context: ExpressionContext) -> ExpressionContext:
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
        expression_context = self._get_node_expression_context(expression_context)
        recorded_paths_mark = self._get_recorded_paths_mark(expression_context)
        modifier = QueryModifier.get_paired(
            [node.get_result(expression_context) for node in self.children], self._combine_modifier
        )
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
            if self.names_generic_field(expression_context.model):
                # A condition built outside filter() - in When(), an aggregate's filter= - may
                # name a generic foreign key.
                from hare.query.generic_foreign_keys.generic_foreign_key_filters import GenericForeignKeyFilters

                rewritten = GenericForeignKeyFilters.rewrite_q(expression_context.model, self)
                if rewritten is not self:
                    return rewritten.get_result(expression_context)
            return FilterKwargs.get_kwargs(self, expression_context)
        return self._get_children(expression_context)

    def _get_expression(self, expression_context: ExpressionContext) -> QueryModifier:
        """Resolves a declared boolean expression into a composable SQL condition.

        Args:
            expression_context: The context this node is resolved against.

        Returns:
            The expression as the criterion - in HAVING when it reads an aggregate.
        """
        expression = cast("Expression", self.expression)
        result = expression.get_result(expression_context)
        field = expression.get_value_field(result)
        criterion: Criterion
        if field is not None and not isinstance(GeneratedField.get_effective_field(field), BooleanField):
            raise QueryError("A condition expression must return a boolean value")
        if isinstance(result.term, Criterion):
            criterion = result.term
        elif field is not None:
            criterion = BasicCriterion(Equality.EQ, result.term, ValueWrapper(True, allow_parametrize=False))
        else:
            raise QueryError("A condition expression must return a boolean value")
        if self._is_negated:
            criterion = Not(criterion)
        if criterion.contains_aggregate:
            return QueryModifier(having_criterion=criterion, joins=result.joins)
        return QueryModifier(where_criterion=criterion, joins=result.joins)
