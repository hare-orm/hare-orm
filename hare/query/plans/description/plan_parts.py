from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.query.plans.constants import OPTIONAL_VALUE_ORIGIN
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.plan_origins import PlanOrigins

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.plans.plan_origins import ValueOrigin


class PlanParts:
    """What the generated descriptions of plannable classes (``PlanDescriberCompiler``) share: an
    argument and a field read, each described with the origins of its values while a query records
    its plan."""

    @staticmethod
    def describe_argument(
        owner: Any,
        attribute: str,
        value: Any,
        context: PlanContext,
        index: int | None = None,
        binds_sequence: bool = False,
    ) -> PlanDescription | None:
        """Describes a value passed as an argument: an expression by its own description, a ``None``
        by its structure alone, a SQL term by its text and the values it binds, any other literal by
        its type, bound itself.

        Args:
            owner: The object the argument was passed to.
            attribute: The attribute holding it.
            value: The argument.
            context: The context the argument is resolved in.
            index: Its position in a sequence held under ``attribute``.
            binds_sequence: Whether a list or tuple literal is bound whole - encoded into one value.

        Returns:
            The description, None for a list or tuple literal not bound whole.
        """
        # Imported here: the plans package is imported by the expressions it describes.
        from hare.query.expressions.constants import NULL_ARGUMENT_STRUCTURE
        from hare.query.expressions.expression import Expression
        from hare.sql.terms.term import Term

        if isinstance(value, Expression):
            return value.get_plan_description(context)
        if value is None:
            return PlanDescription(NULL_ARGUMENT_STRUCTURE, [], [])
        if isinstance(value, Term):
            # Imported here: the term descriptions import the expressions package.
            from hare.query.plans.description.term_plan_descriptions import TermPlanDescriptions

            return TermPlanDescriptions.describe(value)
        if isinstance(value, (list, tuple)) and not binds_sequence:
            return None
        return PlanDescription(
            PlanParts.get_literal_structure(value),
            [value],
            [PlanOrigins.get_value_origin(owner, attribute, index)] if PlanOrigins.records else None,
        )

    @staticmethod
    def get_literal_structure(value: Any) -> tuple[Any, ...]:
        """The structure of a literal bound as a parameter - its type, and what else of it changes
        the SQL built (``Value.get_literal_structure()``).

        Args:
            value: The literal.

        Returns:
            The structure.
        """
        # Imported here: the plans package is imported by the expressions it describes.
        from hare.query.expressions.value import Value

        return ("literal", Value.get_literal_structure(value))

    @staticmethod
    def describe_field(owner: Any, attribute: str, field: Any, context: PlanContext) -> PlanDescription | None:
        """Describes what a function reads: an expression by its own description, a field or an
        annotation by its name - the annotation's values are bound where it is described - and a SQL
        term by its text and the values it binds.

        Args:
            owner: The function.
            attribute: The attribute holding what it reads.
            field: What it reads.
            context: The context the function is resolved in.

        Returns:
            The description, None for anything else.
        """
        # Imported here: the plans package is imported by the expressions it describes.
        from hare.query.expressions.expression import Expression
        from hare.sql.terms.term import Term

        if isinstance(field, Expression):
            return field.get_plan_description(context)
        if isinstance(field, str):
            return PlanDescription(field, [], [])
        if isinstance(field, Term):
            # Imported here: the term descriptions import the expressions package.
            from hare.query.plans.description.term_plan_descriptions import TermPlanDescriptions

            return TermPlanDescriptions.describe(field)
        return None

    @staticmethod
    def get_optional_origins(
        description: PlanDescription, origins: list[ValueOrigin] | None
    ) -> list[ValueOrigin] | None:
        """``origins`` with the origins of a condition folded into JOINs after them, each marked
        optional - a value of the condition binds nothing when the build folds it into no JOIN.

        Args:
            description: The condition's description.
            origins: The origins so far, None when they aren't listed.

        Returns:
            The origins, None when the condition listed none.
        """
        if origins is None:
            return None
        part_origins = description.origins
        if part_origins is None:
            return None if description.values else origins
        origins.extend([(OPTIONAL_VALUE_ORIGIN, origin) for origin in part_origins])
        return origins

    @staticmethod
    def get_origins(description: PlanDescription, origins: list[ValueOrigin] | None) -> list[ValueOrigin] | None:
        """``origins`` with a part's origins after them - None when the part listed none.

        Args:
            description: The part's description.
            origins: The origins so far, None when they aren't listed.

        Returns:
            The origins.
        """
        if origins is None:
            return None
        part_origins = description.origins
        if part_origins is None:
            return None if description.values else origins
        origins.extend(part_origins)
        return origins
