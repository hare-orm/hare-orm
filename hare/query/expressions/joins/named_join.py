from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.exceptions import QueryError
from hare.query.enums import Lookup
from hare.query.expressions.conditions.query_modifier import QueryModifier
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.plans.plan_origins import PlanOrigins

if TYPE_CHECKING:
    from hare.query.expressions.expression_result import ExpressionResult


class NamedJoin(Expression, abstract=True):
    """An annotation that is a JOIN under the name ``alias()``/``annotate()`` gives it - never selected
    itself; ``<name>__<path>`` reads its rows in filters, expressions, ``values()`` and ``order_by()``:
    ``FilteredRelation``, ``AsofJoin``, ``ArrayJoin`` and the named columns (``NamedColumns``).
    """

    #: Its values are in a JOIN - a subclass whose JOIN condition a plan binds declares how.
    plannable: ClassVar[bool] = False

    #: Its JOIN repeats the rows it is joined to - kept even when nothing reads its name, since the
    #: rows are what it declares.
    repeats_rows: ClassVar[bool] = False

    #: The name it is joined under - set by ``with_name()``.
    name: str | None = None

    def with_name(self, name: str) -> Self:
        """A copy joined under ``name``.

        Args:
            name: The alias the queryset gives it.

        Returns:
            The copy.
        """
        named = copy(self)
        named.name = name
        return named

    def check_for_model(self, expression_context: ExpressionContext) -> None:
        """Refuses what it names that the queried model lacks, when ``alias()``/``annotate()`` gives
        it its name - nothing by default.

        Args:
            expression_context: The context of the queried model.
        """

    def get_path_result(self, path: str, expression_context: ExpressionContext) -> ExpressionResult:
        """What ``<name>__<path>`` reads.

        Args:
            path: The path after the name.
            expression_context: The context of the queried model.

        Returns:
            The term, its field and the JOINs.
        """
        raise NotImplementedError

    def get_filter_key(self, path: str, related_model: Any) -> str:
        """A filter key after the name as the related model reads it - a bare lookup
        (``vegetarian__isnull``) compares its primary key.

        Args:
            path: The key after the name.
            related_model: The related model.

        Returns:
            The key.

        Raises:
            QueryError: The bare name compares a composite primary key with anything but ``isnull``.
        """
        first_segment = path.partition("__")[0]
        if not path or (first_segment not in related_model._meta.fields_map and first_segment in set(Lookup)):
            # Whether a row was joined: a column of its primary key is NULL only without one.
            if path != Lookup.ISNULL:
                self.raise_if_composite_primary_key(related_model)
            primary_key_name = related_model._meta.primary_key_attribute_names[0]
            return f"{primary_key_name}__{path}" if path else primary_key_name
        return path

    def get_primary_key_name(self, related_model: Any) -> str:
        """The field the bare name reads - the joined model's primary key.

        Args:
            related_model: The joined model.

        Returns:
            The primary key's field name.

        Raises:
            QueryError: The primary key is composite - no single column to read.
        """
        self.raise_if_composite_primary_key(related_model)
        return related_model._meta.primary_key_attribute_names[0]

    def raise_if_composite_primary_key(self, related_model: Any) -> None:
        """Refuses reading the bare name of a joined model with a composite primary key.

        Args:
            related_model: The joined model.

        Raises:
            QueryError: Its primary key is composite.
        """
        meta = related_model._meta
        if meta.has_composite_primary_key:
            raise QueryError(
                f"{self.name!r} joins {related_model.__name__}, whose primary key is composite - there's no single "
                f"column to read; read its components: "
                + ", ".join(f"{self.name}__{name}" for name in meta.primary_key_attribute_names)
            )

    def get_joined_row_filter(
        self,
        expression_context: ExpressionContext,
        joined_model: Any,
        joined_table: Any,
        joins: list[Any],
        filter_key: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None,
    ) -> QueryModifier:
        """The JOINs, then a filter on the joined row - resolved on the joined model and its table.

        Args:
            expression_context: The context of the query.
            joined_model: The model of the joined row.
            joined_table: Its table in the query.
            joins: The JOINs reaching it.
            filter_key: The filter's key on the joined model.
            value: The filter's value.
            filter_call_generation: The generation of the filter call the condition belongs to.
            value_origin: Where a plan reads the value, None when it isn't recorded.

        Returns:
            The JOINs and the condition.
        """
        # Imported here: the conditions import the named joins.
        from hare.query.expressions.conditions.q import Q

        condition = Q(**{filter_key: value})
        condition._filter_call_generation = filter_call_generation
        if value_origin is not None:
            # The value is the filter's on the join's name.
            PlanOrigins.derive(condition, {filter_key: value_origin})
        modifier = condition.get_result(
            ExpressionContext(
                model=joined_model,
                dialect=expression_context.dialect,
                connection=expression_context.connection,
                table=joined_table,
                annotations=expression_context.annotations,
                visibility=expression_context.visibility,
                value_wrapper_references=expression_context.value_wrapper_references,
            )
        )
        return QueryModifier(joins=joins) & modifier

    def get_path_filter(
        self,
        expression_context: ExpressionContext,
        path: str,
        value: Any,
        filter_call_generation: int,
        value_origin: tuple[Any, ...] | None = None,
    ) -> QueryModifier:
        """Resolves a ``<name>__<path>=value`` filter.

        Args:
            expression_context: The context the filter is resolved in.
            path: The key after the name.
            value: The filter value.
            filter_call_generation: The ``filter()`` call the filter came from.
            value_origin: The origin of the value (``PlanOrigins``) while the query records its
                plan, else None.

        Returns:
            The JOINs and the criterion.
        """
        raise NotImplementedError

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.get_path_result("", expression_context)

    def get_joins_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """What a query reading the name only through its paths resolves it to - for its JOINs. The
        name read alone by default.

        Args:
            expression_context: The context of the queried model.

        Returns:
            The term, its field and the JOINs.
        """
        return self.get_result(expression_context)
