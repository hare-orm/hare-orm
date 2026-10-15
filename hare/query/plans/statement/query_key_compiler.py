from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, cast

from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins


class QueryKeyCompiler:
    """Generates the description of a query class's statement from the slots it declares - once,
    when the class is made - so a class only declares how each of its settings meets the plan key.
    The generated function runs what a description written by hand would: the key is the query's
    class, the declaration's name and each slot's part; the values are bound in the order of the
    slots, each with its origin while a query records its plan."""

    #: Each declaration a query class may make, with the method its description is generated as:
    #: the statement itself, the short one of the most frequent query, the statement over a rows
    #: query, the statement picking its rows by a subquery.
    DECLARATIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("plan_slots", "_describe_statement"),
        ("plain_plan_slots", "_describe_plain_statement"),
        ("rows_plan_slots", "_describe_over_rows"),
        ("subquery_plan_slots", "_describe_through_subquery"),
    )

    @classmethod
    def compile_declared(cls, query_class: type) -> None:
        """Generates the description of each declaration the class makes itself.

        Args:
            query_class: The query class.
        """
        for declaration_name, method_name in cls.DECLARATIONS:
            if declaration_name in query_class.__dict__:
                setattr(query_class, method_name, cls.compile(query_class, declaration_name))

    @staticmethod
    def compile(query_class: type, declaration_name: str) -> Callable[[Any, bool], PlanDescription | None]:
        """Generates a description ``(query, built_into_another) -> PlanDescription | None``.

        Args:
            query_class: The query class.
            declaration_name: The class attribute holding the slots.

        Returns:
            The function.

        Raises:
            TypeError: A slot of an unknown form.
        """
        head = ["type(self)", repr(declaration_name)]
        body: list[str] = []
        namespace: dict[str, Any] = {
            "PlanContext": PlanContext,
            "PlanDescription": PlanDescription,
            "PlanOrigins": PlanOrigins,
            "PlanParts": PlanParts,
        }
        for index, (slot, form) in enumerate(getattr(query_class, declaration_name)):
            key = f"key_{index}"
            if callable(slot):
                # A function every type of query describes the same part with.
                namespace[f"slot_{index}"] = slot
                call = f"slot_{index}(self)"
                attribute = None
            else:
                call = f"self.{slot}()"
                attribute = slot
            if form is PlanKeyForm.VALUE:
                head.append(f"self.{attribute}")
            elif form is PlanKeyForm.TUPLE:
                head.append(f"tuple(self.{attribute})")
            elif form is PlanKeyForm.SORTED_TUPLE:
                head.append(f"tuple(sorted(self.{attribute}))")
            elif form is PlanKeyForm.PRESENCE:
                head.append(f"self.{attribute} is not None")
            elif form is PlanKeyForm.METHOD:
                head.append(call)
            elif form is PlanKeyForm.CONNECTION:
                body += [
                    "    connection = self._connection",
                    "    connection_alias = connection.connection_alias if connection is not None else None",
                ]
                head.append("*((connection_alias,) if built_into_another else (self.dialect, connection_alias))")
            elif form is PlanKeyForm.DESCRIBED:
                body += [
                    f"    description = {call}",
                    "    if description is None:",
                    "        return None",
                    f"    {key} = description.structure",
                    "    values += description.values",
                    "    if origins is not None:",
                    "        origins = PlanParts.get_origins(description, origins)",
                ]
                head.append(key)
            elif form is PlanKeyForm.CONDITIONS:
                namespace.update(QueryKeyCompiler.get_conditions_namespace())
                body += [
                    "    # The model tells a generic foreign key from a field of its name on another model.",
                    "    context = PlanContext(model=self.model) if GenericForeignKeyFieldInstance.declared_names "
                    "else PlanContext.EMPTY",
                    "    structures = []",
                    f"    for condition in self.{attribute}:",
                    "        description = condition.get_plan_description(context)",
                    "        if description is None:",
                    "            return None",
                    "        structures.append(description.structure)",
                    "        values += description.values",
                    "        if origins is not None:",
                    "            origins = PlanParts.get_origins(description, origins)",
                    f"    {key} = tuple(structures)",
                ]
                head.append(key)
            elif form in {PlanKeyForm.BOUND, PlanKeyForm.BOUND_WHEN_TRUE, PlanKeyForm.BOUND_INTO_ANOTHER}:
                test = {
                    PlanKeyForm.BOUND: "value is not None",
                    PlanKeyForm.BOUND_WHEN_TRUE: "bool(value)",
                    PlanKeyForm.BOUND_INTO_ANOTHER: "value is not None",
                }[form]
                bound_test = f"{key} and built_into_another" if form is PlanKeyForm.BOUND_INTO_ANOTHER else key
                body += [
                    f"    value = self.{attribute}",
                    f"    {key} = {test}",
                    f"    if {bound_test}:",
                    "        values.append(value)",
                    "        if origins is not None:",
                    f"            origins.append(PlanOrigins.get_value_origin(self, {attribute!r}))",
                ]
                head.append(key)
            elif form is PlanKeyForm.BOUND_VALUES_METHOD:
                body += [
                    f"    bound_values = {call}",
                    f"    {key} = len(bound_values)",
                    "    values += bound_values",
                    "    if origins is not None:",
                    f"        origins += [PlanOrigins.get_value_origin(self, {attribute!r}, value_index) "
                    "for value_index in range(len(bound_values))]",
                ]
                head.append(key)
            else:
                raise TypeError(f"{query_class.__qualname__}.{declaration_name}: unknown form {form!r} of {slot!r}")
        source = "\n".join(
            [
                "def describe(self, built_into_another):",
                "    values = []",
                "    origins = [] if PlanOrigins.records else None",
                *body,
                f"    return PlanDescription(({', '.join(head)},), values, origins)",
            ]
        )
        exec(source, namespace)  # nosec B102 - class-derived source, no external input
        return cast("Callable[[Any, bool], PlanDescription | None]", namespace["describe"])

    @staticmethod
    def get_conditions_namespace() -> dict[str, Any]:
        """What the lines of a sequence of conditions read.

        Returns:
            The names.
        """
        # Imported here: the field classes import the plans package.
        from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance

        return {"GenericForeignKeyFieldInstance": GenericForeignKeyFieldInstance}
