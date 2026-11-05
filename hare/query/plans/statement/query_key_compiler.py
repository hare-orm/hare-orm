from __future__ import annotations

from collections.abc import Callable
from operator import attrgetter
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.query.plans.constants import KEY_ONLY_PLAN_KEY_FORMS
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class QueryKeyCompiler:
    """Bind a query's slot declarations to one statement-description interpreter.

    The descriptor contains structure only. Values, origins and connection identity are read
    from the query on every call, with no generated Python or retained query instance.
    """

    DECLARATIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("plan_slots", "_describe_statement"),
        ("plain_plan_slots", "_describe_plain_statement"),
        ("rows_plan_slots", "_describe_over_rows"),
    )

    @classmethod
    def compile_declared(cls, query_class: type) -> None:
        """Install descriptors for declarations introduced by this class.

        Args:
            query_class: The statement class.
        """
        for declaration_name, method_name in cls.DECLARATIONS:
            if declaration_name in query_class.__dict__:
                setattr(query_class, method_name, cls.compile(query_class, declaration_name))

    @staticmethod
    def compile(query_class: type, declaration_name: str) -> Callable[[Any, bool], PlanDescription | None]:
        """Bind a snapshot of declared slots to a description function.

        Args:
            query_class: The statement class.
            declaration_name: The attribute containing its slot declarations.

        Returns:
            A function describing a query and whether it is embedded in another query.

        Raises:
            TypeError: A slot has an unknown form.
        """
        slots = tuple(getattr(query_class, declaration_name))
        for slot, form in slots:
            if all(form is not candidate for candidate in PlanKeyForm):
                raise TypeError(f"{query_class.__qualname__}.{declaration_name}: unknown form {form!r} of {slot!r}")

        def describe(query: Any, built_into_another: bool) -> PlanDescription | None:
            return QueryKeyCompiler.describe(query, declaration_name, slots, built_into_another)

        return describe

    @staticmethod
    def get_connection_structure(connection: DatabaseClient | None) -> tuple[Any, ...]:
        """Capture the ordered connection settings shared by both SQL-plan indexes.

        Read the settings on every description so configuration changes select another
        plan. An unbound query contributes the same five absent components. Dialect,
        query structure, visibility and timezone remain the caller's responsibility.
        """
        if connection is None:
            return (None,) * 5
        return (
            connection.connection_alias,
            connection.features.plan_key,
            connection.native_python_types,
            connection.parameter_binding_key,
            connection.sql_context_key,
        )

    @staticmethod
    def describe(
        query: Any, declaration_name: str, slots: DeclaredPlanSlots, built_into_another: bool
    ) -> PlanDescription | None:
        """Read the declaration in parameter order, then its key-only attributes.

        Args:
            query: The current execution's query.
            declaration_name: The declaration identifying this statement shape.
            slots: Its structural declarations.
            built_into_another: Whether slice values belong to the enclosing query's parameters.

        Returns:
            The description, or None when a nested part cannot be planned.
        """
        structure: list[Any] = [type(query), declaration_name]
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        key_positions: list[tuple[int, Any, PlanKeyForm]] = []
        for slot, form in slots:
            attribute = cast("str", slot)
            if form in KEY_ONLY_PLAN_KEY_FORMS:
                key_positions.append((len(structure), slot, form))
                structure.append(None)
            elif form is PlanKeyForm.CONNECTION:
                connection = query._connection
                if not built_into_another:
                    key_positions.append((len(structure), slot, form))
                    structure.append(None)
                structure.extend(QueryKeyCompiler.get_connection_structure(connection))
            elif form is PlanKeyForm.DESCRIBED:
                description = slot(query) if callable(slot) else attrgetter(slot)(query)()
                if description is None:
                    return None
                structure.append(description.structure)
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            elif form is PlanKeyForm.CONDITIONS:
                from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions

                context = PlanContext(model=query.model)
                description = StatementPlanDescriptions.get_conditions_plan_description(
                    attrgetter(cast("str", slot))(query), context
                )
                if description is None:
                    return None
                structure.append(description.structure)
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            elif form in {PlanKeyForm.BOUND, PlanKeyForm.BOUND_WHEN_TRUE, PlanKeyForm.BOUND_INTO_ANOTHER}:
                value = attrgetter(cast("str", slot))(query)
                present = bool(value) if form is PlanKeyForm.BOUND_WHEN_TRUE else value is not None
                structure.append(present)
                if present and (form is not PlanKeyForm.BOUND_INTO_ANOTHER or built_into_another):
                    values.append(value)
                    if origins is not None:
                        origins.append(PlanOrigins.get_value_origin(query, attribute))
            elif form is PlanKeyForm.BOUND_VALUES_METHOD:
                bound_values = slot(query) if callable(slot) else attrgetter(slot)(query)()
                structure.append(len(bound_values))
                values.extend(bound_values)
                if origins is not None:
                    origin_attribute = None if callable(slot) else slot
                    origins.extend(
                        PlanOrigins.get_value_origin(query, origin_attribute, index)
                        for index in range(len(bound_values))
                    )
        for position, slot, form in key_positions:
            if form is PlanKeyForm.CONNECTION:
                value = query.dialect
            elif form is PlanKeyForm.METHOD:
                value = slot(query) if callable(slot) else attrgetter(cast("str", slot))(query)()
            else:
                value = attrgetter(cast("str", slot))(query)
                if form is PlanKeyForm.TUPLE:
                    value = tuple(value)
                elif form is PlanKeyForm.SORTED_TUPLE:
                    value = tuple(sorted(value))
                elif form is PlanKeyForm.PRESENCE:
                    value = value is not None
            structure[position] = value
        return PlanDescription(tuple(structure), values, origins)
