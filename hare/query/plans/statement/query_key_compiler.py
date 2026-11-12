from __future__ import annotations

from collections.abc import Callable
from operator import attrgetter, methodcaller
from typing import TYPE_CHECKING, Any, ClassVar

from hare.query.plans.constants import KEY_ONLY_PLAN_KEY_FORMS
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declared_plan_slots import CompiledKeyPosition, CompiledPlanSlot
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class QueryKeyCompiler:
    """Bind a query's slot declarations to one statement-description interpreter.

    The descriptor contains structure only. Values, origins and connection identity are read
    from the query on every call, with no generated Python or retained query instance.
    """

    #: Each slot declaration a query class may hold, with the method its description is installed as.
    DECLARATIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("plan_slots", "_describe_statement"),
        ("plain_plan_slots", "_describe_plain_statement"),
        ("rows_plan_slots", "_describe_over_rows"),
    )
    #: The connection settings of an unbound query's key (``get_connection_structure()``).
    UNBOUND_CONNECTION_STRUCTURE: ClassVar[tuple[None, ...]] = (None, None, None, None, None, None)
    #: The forms whose slot names a method of the query, called for the value.
    METHOD_PLAN_KEY_FORMS: ClassVar[frozenset[PlanKeyForm]] = frozenset(
        {PlanKeyForm.DESCRIBED, PlanKeyForm.BOUND_VALUES_METHOD, PlanKeyForm.METHOD}
    )
    #: The forms whose value the plan binds as a parameter.
    BOUND_VALUE_PLAN_KEY_FORMS: ClassVar[frozenset[PlanKeyForm]] = frozenset(
        {PlanKeyForm.BOUND, PlanKeyForm.BOUND_WHEN_TRUE, PlanKeyForm.BOUND_INTO_ANOTHER}
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
        compiled_slots: list[CompiledPlanSlot] = []
        for slot, form in slots:
            if all(form is not candidate for candidate in PlanKeyForm):
                raise TypeError(f"{query_class.__qualname__}.{declaration_name}: unknown form {form!r} of {slot!r}")
            if callable(slot):
                reader: Callable[[Any], Any] = slot
                origin_attribute: str | None = None
            else:
                attribute_name: str = slot
                reader = (
                    methodcaller(attribute_name)
                    if form in QueryKeyCompiler.METHOD_PLAN_KEY_FORMS
                    else attrgetter(attribute_name)
                )
                origin_attribute = attribute_name
            compiled_slots.append((form, reader, origin_attribute))
        bound_slots = tuple(compiled_slots)
        outer_positions = QueryKeyCompiler.get_key_positions(bound_slots, built_into_another=False)
        nested_positions = QueryKeyCompiler.get_key_positions(bound_slots, built_into_another=True)

        def describe(query: Any, built_into_another: bool) -> PlanDescription | None:
            return QueryKeyCompiler.describe_compiled(
                query,
                declaration_name,
                bound_slots,
                nested_positions if built_into_another else outer_positions,
                built_into_another,
            )

        return describe

    @staticmethod
    def get_key_positions(
        slots: tuple[CompiledPlanSlot, ...], *, built_into_another: bool
    ) -> tuple[CompiledKeyPosition, ...]:
        """Prepare fixed key-only positions after the query type and declaration name.

        A connection contributes its five settings, plus the dialect outside a nested query.
        Its flag records whether the first pass needs a deferred dialect read: readers may
        change the query, so the second pass must not check the connection again.

        Args:
            slots: The declaration's bound readers, in parameter-reading order.
            built_into_another: Whether the enclosing query supplies the dialect.

        Returns:
            Positions and connection flags containing no query values or connection instances.
        """
        positions: list[CompiledKeyPosition] = []
        position = 2
        connection_bit = 1
        for form, reader, _origin_attribute in slots:
            if form is PlanKeyForm.CONNECTION:
                if not built_into_another:
                    positions.append((position, reader, form, connection_bit))
                position += len(QueryKeyCompiler.UNBOUND_CONNECTION_STRUCTURE) + (not built_into_another)
                connection_bit <<= 1
            else:
                if form in KEY_ONLY_PLAN_KEY_FORMS:
                    positions.append((position, reader, form, 0))
                position += 1
        return tuple(positions)

    @staticmethod
    def get_connection_structure(connection: DatabaseClient | None) -> tuple[Any, ...]:
        """The settings of a connection a plan key holds - read on every description, so a changed
        setting selects another plan; absent ones for an unbound query. Whether the client has
        installed transaction tenants is structural too: a plan using related tables cannot
        skip their client checks by crossing from that transaction to an ordinary client.

        Args:
            connection: The connection, None for an unbound query.

        Returns:
            The settings, in order.
        """
        if connection is None:
            return QueryKeyCompiler.UNBOUND_CONNECTION_STRUCTURE
        return (
            connection.connection_alias,
            connection.features.plan_key,
            connection.native_python_types,
            connection.parameter_binding_key,
            connection.sql_context_key,
            connection.sets_tenants,
        )

    @staticmethod
    def describe_compiled(
        query: Any,
        declaration_name: str,
        slots: tuple[CompiledPlanSlot, ...],
        key_positions: tuple[CompiledKeyPosition, ...],
        built_into_another: bool,
    ) -> PlanDescription | None:
        """Read a prebound slot declaration in parameter order, then its key-only attributes."""
        structure: list[Any] = [type(query), declaration_name]
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        deferred_connections = 0
        connection_bit = 1
        for form, reader, origin_attribute in slots:
            if form in KEY_ONLY_PLAN_KEY_FORMS:
                structure.append(None)
            elif form is PlanKeyForm.CONNECTION:
                connection = query._connection
                if connection is None:
                    if not built_into_another:
                        deferred_connections |= connection_bit
                        structure.append(None)
                    structure.extend(QueryKeyCompiler.UNBOUND_CONNECTION_STRUCTURE)
                else:
                    # Built into another query, the dialect is that query's.
                    if not built_into_another:
                        structure.append(connection.dialect)
                    structure.extend(QueryKeyCompiler.get_connection_structure(connection))
                connection_bit <<= 1
            elif form is PlanKeyForm.DESCRIBED:
                description = reader(query)
                if description is None:
                    return None
                structure.append(description.structure)
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            elif form is PlanKeyForm.CONDITIONS:
                context = PlanContext(model=query.model)
                description = StatementPlanDescriptions.get_conditions_plan_description(reader(query), context)
                if description is None:
                    return None
                structure.append(description.structure)
                values.extend(description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(description, origins)
            elif form in QueryKeyCompiler.BOUND_VALUE_PLAN_KEY_FORMS:
                value = reader(query)
                present = bool(value) if form is PlanKeyForm.BOUND_WHEN_TRUE else value is not None
                structure.append(present)
                if present and (form is not PlanKeyForm.BOUND_INTO_ANOTHER or built_into_another):
                    values.append(value)
                    if origins is not None:
                        origins.append(PlanOrigins.get_value_origin(query, origin_attribute))
            elif form is PlanKeyForm.BOUND_VALUES_METHOD:
                bound_values = reader(query)
                structure.append(len(bound_values))
                values.extend(bound_values)
                if origins is not None:
                    origins.extend(
                        PlanOrigins.get_value_origin(query, origin_attribute, index)
                        for index in range(len(bound_values))
                    )
        for position, reader, form, connection_flag in key_positions:
            if form is PlanKeyForm.CONNECTION:
                if not (deferred_connections & connection_flag):
                    continue
                value = query.dialect
            elif form is PlanKeyForm.VALUE or form is PlanKeyForm.METHOD:
                value = reader(query)
            else:
                value = reader(query)
                if form is PlanKeyForm.TUPLE:
                    value = tuple(value)
                elif form is PlanKeyForm.SORTED_TUPLE:
                    value = tuple(sorted(value))
                elif form is PlanKeyForm.PRESENCE:
                    value = value is not None
            structure[position] = value
        return PlanDescription(tuple(structure), values, origins)
