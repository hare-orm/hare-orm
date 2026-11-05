from __future__ import annotations

from collections.abc import Callable
from operator import attrgetter
from typing import Any

from hare.query.plans.constants import KEY_ONLY_PLAN_PART_TYPES
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanPartType
from hare.query.plans.plan_origins import PlanOrigins


class PlanDescriberCompiler:
    """Bind declared parts to a shared interpreter without generating Python source.

    Values and their origins follow declaration order. Key-only attributes are read after
    nested descriptions, so preparation performed by those descriptions is reflected in the key.
    """

    ORIGIN_SLOTS = ("_plan_origin", "_value_origins")

    @staticmethod
    def compile(describer_class: type) -> Callable[[Any, PlanContext], PlanDescription | None]:
        """Bind a snapshot of plan_parts to the class's description method.

        Args:
            describer_class: The class declaring the parts.

        Returns:
            A description method reading each object's current values.

        Raises:
            TypeError: A declaration uses an unknown part type.
        """
        parts = tuple(describer_class.plan_parts)  # type: ignore[attr-defined]
        for attribute, part_type in parts:
            if all(part_type is not candidate for candidate in PlanPartType):
                raise TypeError(
                    f"{describer_class.__qualname__}.plan_parts: unknown part type {part_type!r} of {attribute!r}"
                )

        def describe(obj: Any, context: PlanContext) -> PlanDescription | None:
            return PlanDescriberCompiler.describe(obj, context, parts)

        return describe

    @staticmethod
    def describe(obj: Any, context: PlanContext, parts: DeclaredPlanParts) -> PlanDescription | None:
        """Combine the declared parts, retaining no execution values on the class.

        Args:
            obj: The expression or condition.
            context: The enclosing model and binding context.
            parts: The declared parts in parameter order.

        Returns:
            The structure, values and origins, or None for an unplannable part.
        """
        structures: list[Any] = [type(obj)]
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        key_positions: list[tuple[int, str, PlanPartType]] = []
        for attribute, part_type in parts:
            if part_type is PlanPartType.NONE:
                continue
            if part_type is PlanPartType.KEEPS_PLAN_METHOD:
                if not attrgetter(attribute)(obj)():
                    return None
                continue
            if part_type in KEY_ONLY_PLAN_PART_TYPES:
                key_positions.append((len(structures), attribute, part_type))
                structures.append(None)
                continue
            description = PlanDescriberCompiler.describe_part(obj, attribute, part_type, context)
            if description is None:
                return None
            structures.append(description.structure)
            values.extend(description.values)
            if origins is not None:
                origins = (
                    PlanParts.get_optional_origins(description, origins)
                    if part_type is PlanPartType.JOIN_CONDITION_METHOD
                    else PlanParts.get_origins(description, origins)
                )
        for position, attribute, part_type in key_positions:
            value = attrgetter(attribute)(obj)
            if part_type is PlanPartType.KEY_METHOD:
                value = value()
            elif part_type is PlanPartType.PLAN_KEY:
                value = None if value is None else value.get_plan_key()
            elif part_type is PlanPartType.KEYS:
                value = tuple(value)
            structures[position] = value
        return PlanDescription(tuple(structures), values, origins)

    @staticmethod
    def describe_part(
        obj: Any, attribute: str, part_type: PlanPartType, context: PlanContext
    ) -> PlanDescription | None:
        """Read a value-bearing part through its declared protocol.

        Args:
            obj: The declaring object.
            attribute: Its attribute or method name.
            part_type: How the value contributes to the statement.
            context: The enclosing binding context.

        Returns:
            The part's description, or None for an unplannable value.
        """
        value = attrgetter(attribute)(obj)
        if part_type in {
            PlanPartType.VALUE_METHOD,
            PlanPartType.ARGUMENT_METHOD,
            PlanPartType.EXPRESSION_METHOD,
            PlanPartType.JOIN_CONDITION_METHOD,
            PlanPartType.QUERY_METHOD,
        }:
            value = value()
        if part_type in {PlanPartType.LITERAL, PlanPartType.VALUE_METHOD}:
            if part_type is PlanPartType.LITERAL:
                from hare.query.expressions.constants import LITERAL_CAST_SQL_TYPE

                structure = (obj.get_cast_sql_type(value, LITERAL_CAST_SQL_TYPE), obj.get_literal_structure(value))
            else:
                structure = PlanParts.get_literal_structure(value)
            return PlanDescription(
                structure,
                [value],
                [PlanOrigins.get_value_origin(obj, attribute)] if PlanOrigins.records else None,
            )
        if part_type is PlanPartType.PARAMETERS:
            return PlanDescription(
                len(value),
                [parameter.value for parameter in value],
                [PlanOrigins.get_value_origin(obj, attribute, index) for index in range(len(value))]
                if PlanOrigins.records
                else None,
            )
        if part_type in {PlanPartType.ARGUMENT, PlanPartType.ARGUMENT_METHOD, PlanPartType.ENCODED_ARGUMENT}:
            return PlanParts.describe_argument(
                obj, attribute, value, context, binds_sequence=part_type is PlanPartType.ENCODED_ARGUMENT
            )
        if part_type is PlanPartType.FIELD:
            return PlanParts.describe_field(value, context)
        if part_type in {PlanPartType.EXPRESSION, PlanPartType.EXPRESSION_METHOD, PlanPartType.JOIN_CONDITION_METHOD}:
            return PlanDescription.ABSENT if value is None else value.get_plan_description(context)
        if part_type in {PlanPartType.ARGUMENTS, PlanPartType.FIELDS, PlanPartType.EXPRESSIONS}:
            descriptions = []
            for index, item in enumerate(value or ()):
                if part_type is PlanPartType.ARGUMENTS:
                    description = PlanParts.describe_argument(obj, attribute, item, context, index)
                elif part_type is PlanPartType.FIELDS:
                    description = PlanParts.describe_field(item, context)
                else:
                    description = item.get_plan_description(context)
                if description is None:
                    return None
                descriptions.append(description)
            combined = PlanDescription.combine(None, descriptions)
            # Unplannable children returned above; combining the remaining descriptions cannot fail.
            assert combined is not None  # nosec B101
            combined.structure = combined.structure[1:]
            return combined
        if part_type in {PlanPartType.QUERY, PlanPartType.QUERY_METHOD}:
            from hare.query.query_connection import QueryConnection

            description = value.get_plan_description(context)
            if description is None:
                return None
            return PlanDescription(
                (QueryConnection.get_pinned_connection_name(value), description.structure),
                description.values,
                description.origins,
            )
        if part_type is PlanPartType.FILTERS:
            return PlanDescriberCompiler.describe_filters(obj, value, context)
        raise TypeError(f"Unknown plan part {part_type!r}")

    @staticmethod
    def describe_filters(obj: Any, filters: dict[str, Any], context: PlanContext) -> PlanDescription | None:
        """Describe lookup arguments in order, including their nested value origins.

        Args:
            obj: The condition.
            filters: Its lookup names and arguments.
            context: The model and collection-binding context.

        Returns:
            The description, or None for an empty or unsupported condition.
        """
        from hare.query.expressions.constants import PLAIN_VALUE_TYPES
        from hare.query.plans.description.filter_plan_descriptions import FilterPlanDescriptions
        from hare.query.plans.description.plannable import Plannable

        if not filters:
            return None if not obj.children and obj.expression is None else PlanDescription.EMPTY
        if context.model is None or obj.names_generic_field(context.model):
            return None
        structures = []
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for key, value in filters.items():
            if type(value) in PLAIN_VALUE_TYPES:
                structures.append((key, type(value)))
                values.append(value)
                if origins is not None:
                    origins.append(PlanOrigins.get_value_origin(obj, key))
            elif isinstance(value, Plannable):
                description = FilterPlanDescriptions.get_filter_value_plan_description(value, context)
                if description is None:
                    return None
                structures.append((key, description.structure))
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            else:
                value_count = len(values)
                structure = FilterPlanDescriptions.describe_value_filter(
                    key, value, values, context.single_parameter_in_list_min_length
                )
                structures.append((key, structure))
                if origins is not None and len(values) > value_count:
                    origins.append(PlanOrigins.get_value_origin(obj, key))
        return PlanDescription(tuple(structures), values, origins)

    @staticmethod
    def raise_if_slots_unclassified(describer_class: type) -> None:
        """Reject state omitted from the declared description.

        Args:
            describer_class: The class being installed.

        Raises:
            TypeError: A slot has no declared role in the plan.
        """
        slots = describer_class.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        classified = {attribute for attribute, _part_type in describer_class.plan_parts}  # type: ignore[attr-defined]
        unclassified = sorted(set(slots) - classified - set(PlanDescriberCompiler.ORIGIN_SLOTS))
        if unclassified:
            raise TypeError(
                f"{describer_class.__qualname__}.plan_parts classifies none of its slots {unclassified} - "
                "declare each with its PlanPartType (NONE for one no SQL text depends on)"
            )
