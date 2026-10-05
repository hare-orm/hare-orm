from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanPartType
from hare.query.plans.plan_origins import PlanOrigins


class PlanDescriberCompiler:
    """Generates a plannable class's ``get_plan_description()`` from the parts it declares
    (``plan_parts``) - once, when the class is made - so a class only declares how each attribute
    meets the plan. The generated function runs what a description written by hand would: the key
    is the class and its ``KEY`` parts, then each other part's structure; the values are bound in
    the order of the parts, each with its origin while a query records its plan."""

    #: The attributes of a plannable object its plan never reads - the origin a copy keeps.
    ORIGIN_SLOTS = ("_plan_origin", "_value_origins")
    #: The parts whose value the statement binds as it is.
    VALUE_PART_TYPES = frozenset({PlanPartType.LITERAL, PlanPartType.PARAMETERS, PlanPartType.VALUE_METHOD})

    @staticmethod
    def compile(describer_class: type) -> Callable[[Any, Any], PlanDescription | None]:
        """Generates the class's ``get_plan_description()``.

        Args:
            describer_class: The class - its ``plan_parts`` declared.

        Returns:
            The function.

        Raises:
            TypeError: A part of an unknown type.
        """
        head = ["type(self)"]
        body: list[str] = []
        namespace: dict[str, Any] = {
            "PlanDescription": PlanDescription,
            "PlanOrigins": PlanOrigins,
            "PlanParts": PlanParts,
        }
        for index, (attribute, part_type) in enumerate(describer_class.plan_parts):  # type: ignore[attr-defined]
            # Each part's structure in a local of its own - the key is made of them at once.
            structure = f"structure_{index}"
            if part_type is PlanPartType.NONE:
                continue
            if part_type is PlanPartType.KEEPS_PLAN_METHOD:
                body += [f"    if not self.{attribute}():", "        return None"]
                continue
            if part_type is PlanPartType.KEY:
                head.append(f"self.{attribute}")
                continue
            if part_type is PlanPartType.KEY_METHOD:
                head.append(f"self.{attribute}()")
                continue
            if part_type is PlanPartType.PLAN_KEY:
                head.append(f"(None if self.{attribute} is None else self.{attribute}.get_plan_key())")
                continue
            if part_type is PlanPartType.KEYS:
                head.append(f"tuple(self.{attribute})")
                continue
            if part_type in PlanDescriberCompiler.VALUE_PART_TYPES:
                body += PlanDescriberCompiler.get_value_part_source(attribute, part_type, structure, namespace)
            else:
                part_source = PlanDescriberCompiler.get_nested_part_source(attribute, part_type, structure, namespace)
                if part_source is None:
                    raise TypeError(
                        f"{describer_class.__qualname__}.plan_parts: unknown part type {part_type!r} of {attribute!r}"
                    )
                body += part_source
            head.append(structure)
        source = "\n".join(
            [
                "def get_plan_description(self, context):",
                "    values = []",
                "    origins = [] if PlanOrigins.records else None",
                *body,
                f"    return PlanDescription(({', '.join(head)},), values, origins)",
            ]
        )
        exec(source, namespace)  # nosec B102 - class-derived source, no external input
        return cast("Callable[[Any, Any], PlanDescription | None]", namespace["get_plan_description"])

    @staticmethod
    def get_value_part_source(
        attribute: str, part_type: PlanPartType, structure: str, namespace: dict[str, Any]
    ) -> list[str]:
        """The lines binding a part whose value the statement binds as it is - a literal, the
        parameters of raw SQL, or a method's value.

        Args:
            attribute: The attribute of the part.
            part_type: How the part meets the plan.
            structure: The local the part's structure is kept in.
            namespace: The names the generated function reads - added to.

        Returns:
            The lines.
        """
        if part_type is PlanPartType.LITERAL:
            # Imported here: the constants import the fields, which import the plans package.
            from hare.query.expressions.constants import LITERAL_CAST_SQL_TYPE

            namespace["LITERAL_CAST_SQL_TYPE"] = LITERAL_CAST_SQL_TYPE
            return [
                f"    value = self.{attribute}",
                f"    {structure} = (",
                "        self.get_cast_sql_type(value, LITERAL_CAST_SQL_TYPE),",
                "        self.get_literal_structure(value),",
                "    )",
                "    values.append(value)",
                "    if origins is not None:",
                f"        origins.append(PlanOrigins.get_value_origin(self, {attribute!r}))",
            ]
        if part_type is PlanPartType.PARAMETERS:
            return [
                f"    parameters = self.{attribute}",
                f"    {structure} = len(parameters)",
                "    for parameter_index, parameter in enumerate(parameters):",
                "        values.append(parameter.value)",
                "        if origins is not None:",
                f"            origins.append(PlanOrigins.get_value_origin(self, {attribute!r}, parameter_index))",
            ]
        if part_type is PlanPartType.VALUE_METHOD:
            namespace["get_literal_structure"] = PlanParts.get_literal_structure
            return [
                f"    value = self.{attribute}()",
                f"    {structure} = get_literal_structure(value)",
                "    values.append(value)",
                "    if origins is not None:",
                f"        origins.append(PlanOrigins.get_value_origin(self, {attribute!r}))",
            ]
        return []

    @staticmethod
    def get_nested_part_source(
        attribute: str, part_type: PlanPartType, structure: str, namespace: dict[str, Any]
    ) -> list[str] | None:
        """The lines describing a part made of what describes itself - arguments, fields,
        expressions, queries or a condition's filters.

        Args:
            attribute: The attribute of the part.
            part_type: How the part meets the plan.
            structure: The local the part's structure is kept in.
            namespace: The names the generated function reads - added to.

        Returns:
            The lines, None for an unknown part type.
        """
        if part_type in {
            PlanPartType.ARGUMENT,
            PlanPartType.ARGUMENT_METHOD,
            PlanPartType.ENCODED_ARGUMENT,
            PlanPartType.FIELD,
        }:
            method = "describe_field" if part_type is PlanPartType.FIELD else "describe_argument"
            value_source = f"self.{attribute}()" if part_type is PlanPartType.ARGUMENT_METHOD else f"self.{attribute}"
            # An encoded argument binds a list or tuple literal whole.
            more_arguments = ", None, True" if part_type is PlanPartType.ENCODED_ARGUMENT else ""
            call_arguments = f"self, {attribute!r}, {value_source}, context{more_arguments}"
            return [
                f"    description = PlanParts.{method}({call_arguments})",
                *PlanDescriberCompiler.get_part_source(f"{structure} = description.structure", "    "),
            ]
        if part_type in {PlanPartType.EXPRESSION, PlanPartType.EXPRESSION_METHOD, PlanPartType.JOIN_CONDITION_METHOD}:
            call = "" if part_type is PlanPartType.EXPRESSION else "()"
            part_source = PlanDescriberCompiler.get_part_source(f"{structure} = description.structure", "        ")
            if part_type is PlanPartType.JOIN_CONDITION_METHOD:
                part_source[-1] = part_source[-1].replace("get_origins", "get_optional_origins")
            return [
                f"    part = self.{attribute}{call}",
                "    if part is None:",
                f"        {structure} = None",
                "    else:",
                "        description = part.get_plan_description(context)",
                *part_source,
            ]
        if part_type in {PlanPartType.ARGUMENTS, PlanPartType.FIELDS, PlanPartType.EXPRESSIONS}:
            if part_type is PlanPartType.ARGUMENTS:
                loop = "        for item_index, item in enumerate(items):"
                item_call = f"PlanParts.describe_argument(self, {attribute!r}, item, context, item_index)"
            elif part_type is PlanPartType.FIELDS:
                loop = "        for item in items:"
                item_call = f"PlanParts.describe_field(self, {attribute!r}, item, context)"
            else:
                loop = "        for item in items:"
                item_call = "item.get_plan_description(context)"
            return [
                f"    items = self.{attribute}",
                "    if items:",
                "        item_structures = []",
                loop,
                f"            description = {item_call}",
                *PlanDescriberCompiler.get_part_source(
                    "item_structures.append(description.structure)", "            "
                ),
                f"        {structure} = tuple(item_structures)",
                "    else:",
                f"        {structure} = ()",
            ]
        if part_type in {PlanPartType.QUERY, PlanPartType.QUERY_METHOD}:
            namespace.update(PlanDescriberCompiler.get_query_namespace())
            call = "()" if part_type is PlanPartType.QUERY_METHOD else ""
            return [
                f"    query = self.{attribute}{call}",
                "    description = query.get_plan_description(context)",
                *PlanDescriberCompiler.get_part_source(
                    f"{structure} = (QueryConnection.get_pinned_connection_name(query), description.structure)",
                    "    ",
                ),
            ]
        if part_type is PlanPartType.FILTERS:
            namespace.update(PlanDescriberCompiler.get_filters_namespace())
            return PlanDescriberCompiler.get_filters_source(attribute, structure)
        return None

    @staticmethod
    def get_part_source(keep_structure: str, indent: str) -> list[str]:
        """The lines taking a part's description in: no plan when it keeps none, its structure, its
        values and their origins.

        Args:
            keep_structure: The statement keeping the part's structure.
            indent: The indentation of the lines.

        Returns:
            The lines.
        """
        return [
            f"{indent}if description is None:",
            f"{indent}    return None",
            f"{indent}{keep_structure}",
            f"{indent}values += description.values",
            f"{indent}if origins is not None:",
            f"{indent}    origins = PlanParts.get_origins(description, origins)",
        ]

    @staticmethod
    def get_query_namespace() -> dict[str, Any]:
        """What the lines of a query built into the statement read.

        Returns:
            The names.
        """
        # Imported here: the connection module imports the plans package.
        from hare.query.query_connection import QueryConnection

        return {"QueryConnection": QueryConnection}

    @staticmethod
    def get_filters_namespace() -> dict[str, Any]:
        """What the lines of a condition's filters read.

        Returns:
            The names.
        """
        # Imported here: the filter descriptions and the field classes import the plans package.
        from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
        from hare.query.expressions.constants import PLAIN_VALUE_TYPES
        from hare.query.plans.description.filter_plan_descriptions import FilterPlanDescriptions
        from hare.query.plans.description.plannable import Plannable

        return {
            "FilterPlanDescriptions": FilterPlanDescriptions,
            "GenericForeignKeyFieldInstance": GenericForeignKeyFieldInstance,
            "PLAIN_VALUE_TYPES": PLAIN_VALUE_TYPES,
            "Plannable": Plannable,
        }

    @staticmethod
    def get_filters_source(attribute: str, structure: str) -> list[str]:
        """The lines describing a condition's filters: each key with its value's structure - a plain
        value by its type, bound, at once - and, for a node of no filters, children or expression,
        no plan.

        Args:
            attribute: The attribute holding the filters.
            structure: The local the filters' structure is kept in.

        Returns:
            The lines.
        """
        return [
            f"    filters = self.{attribute}",
            "    if not filters:",
            "        if not self.children and self.expression is None:",
            "            return None",
            f"        {structure} = ()",
            "    else:",
            "        declared_names = GenericForeignKeyFieldInstance.declared_names",
            "        if declared_names and self.names_generic_field(declared_names, context.model):",
            "            # A generic foreign key's condition depends on the model of each value and the type",
            "            # named - no plan describes it.",
            "            return None",
            "        filter_structures = []",
            "        for key, value in filters.items():",
            "            value_type = type(value)",
            "            if value_type in PLAIN_VALUE_TYPES:",
            "                # Bound as it is - the lookup builds its criterion from a value of its type.",
            "                filter_structures.append((key, value_type))",
            "                values.append(value)",
            "                if origins is not None:",
            "                    origins.append(PlanOrigins.get_value_origin(self, key))",
            "            elif isinstance(value, Plannable):",
            "                description = FilterPlanDescriptions.get_filter_value_plan_description(value, context)",
            "                if description is None:",
            "                    return None",
            "                filter_structures.append((key, description.structure))",
            "                values += description.values",
            "                if origins is not None:",
            "                    origins = PlanParts.get_origins(description, origins)",
            "            else:",
            "                if origins is not None:",
            "                    value_count = len(values)",
            "                filter_structures.append(",
            "                    (",
            "                        key,",
            "                        FilterPlanDescriptions.describe_value_filter(",
            "                            key,",
            "                            value,",
            "                            values,",
            "                            context.single_parameter_in_list_min_length,",
            "                            context.describes_json_containment_by_shape,",
            "                        ),",
            "                    )",
            "                )",
            "                if origins is not None and len(values) > value_count:",
            "                    origins.append(PlanOrigins.get_value_origin(self, key))",
            f"        {structure} = tuple(filter_structures)",
        ]

    @staticmethod
    def raise_if_slots_unclassified(describer_class: type) -> None:
        """Rejects a class whose own ``__slots__`` name an attribute none of its ``plan_parts``
        classifies - an attribute added without saying how it meets the plan would leave it out of
        the key.

        Args:
            describer_class: The class.

        Raises:
            TypeError: An attribute is unclassified.
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
