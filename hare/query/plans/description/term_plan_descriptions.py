from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import HareError
from hare.query.expressions.constants import UNBINDABLE_VALUE_ORIGIN
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.plan_origins import PlanOrigins
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT
from hare.sql.terms.parameters.recording_parameterizer import RecordingParameterizer
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences


class TermPlanDescriptions:
    """How a ``hare.sql`` term built by hand describes itself for a plan, as a ``RawSQL`` fragment
    does: its SQL text with each value a parameter and the classes of its nodes are the structure,
    the values it binds as parameters are the values - in the order its tree holds them, each
    coming from the term holding it."""

    @staticmethod
    def get_bound_wrappers(term: Term) -> tuple[str, list[ValueWrapper]] | None:
        """The term's SQL text, rendered with every value it binds a parameter, and the terms of the
        values it binds - a value written into the text itself (a date part's keyword) is part of
        the text.

        Args:
            term: The term.

        Returns:
            The text and the terms, None for a term rendering only on a database's own dialect or
            binding a parameter no value term holds.
        """
        parameterizer = RecordingParameterizer()
        try:
            sql = term.get_sql(DEFAULT_SQL_CONTEXT.copy(parameterizer=parameterizer))
        except (HareError, NotImplementedError, TypeError, ValueError):
            return None
        # In the order the text binds them, each once - a value bound twice is one value.
        wrappers_by_id: dict[int, ValueWrapper] = {}
        for source in parameterizer.sources:
            if not isinstance(source, ValueWrapper):
                return None
            wrappers_by_id.setdefault(id(source), source)
        return sql, list(wrappers_by_id.values())

    @staticmethod
    def describe(term: Term) -> PlanDescription | None:
        """Describes a term.

        Args:
            term: The term.

        Returns:
            The description, None for a term rendering only on a database's own dialect.
        """
        bound = TermPlanDescriptions.get_bound_wrappers(term)
        if bound is None:
            return None
        sql, wrappers = bound
        values = [wrapper.value for wrapper in wrappers]
        nodes: list[Term] = list(term.nodes_())
        node_types = tuple([type(node) for node in nodes])
        return PlanDescription(
            (Term, sql, node_types, tuple([type(value) for value in values])),
            values,
            [PlanOrigins.get_value_origin(wrapper, "value") for wrapper in wrappers] if PlanOrigins.records else None,
        )

    @staticmethod
    def record(term: Term, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Records the references of the values a term binds.

        Args:
            term: The term.
            value_wrapper_references: The recording - None when no plan is recorded.
        """
        if value_wrapper_references is None:
            return
        bound = TermPlanDescriptions.get_bound_wrappers(term)
        if bound is None:
            # The description is None as well - the query keeps no plan.
            value_wrapper_references.append((UNBINDABLE_VALUE_ORIGIN, None))
            return
        # Imported here: the expressions package imports the plans package.
        from hare.query.expressions.value_references.expression_arguments import ExpressionArguments

        for wrapper in bound[1]:
            ExpressionArguments.record_literal(value_wrapper_references, wrapper, "value", wrapper)
