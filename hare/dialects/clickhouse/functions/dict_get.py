from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.class_path import ClassPath
from hare.classes.value_descriptions import ValueDescriptions
from hare.core.hare_context import HareContext
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_DICTIONARY_DEFAULT_FUNCTION,
    CLICKHOUSE_DICTIONARY_FUNCTION,
    CLICKHOUSE_TUPLE_FUNCTION,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_dictionary import ClickhouseDictionary
from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, F, Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.literal_value import LiteralValue

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model


class DictGet(Expression):
    """``dictGet(dictionary, attribute, key)`` - a value of a ClickHouse dictionary by its key, read
    from the server's memory in place of a JOIN::

        Visit.objects.annotate(
            country=DictGet("country_names", "name", "country_code", output_field=fields.CharField(max_length=50))
        )

    A dictionary a model declares (``Meta.dictionaries``) is checked when the query is built: its
    attribute is named by the field (or the field's column), a key literal is checked by the key's
    field, and the keys are as many as the dictionary's.

    Args:
        dictionary: The dictionary's name.
        attribute: The attribute read - a field the dictionary names, or that field's column.
        key: The key looked up - a field name, an expression or a literal; a list or a tuple of them for a
            dictionary keyed by several fields.
        output_field: The field the value is read as.
        default: The value of a key the dictionary doesn't hold - a literal or an expression
            (``dictGetOrDefault``); without one the server gives the attribute type's own default.

    Raises:
        QueryError: ``dictionary`` or ``attribute`` isn't a non-empty string, ``key`` is empty,
            ``output_field`` isn't a field; a declared dictionary has no such attribute or another
            number of keys.
        ValidationError: A key literal of a declared dictionary isn't a value of its key field.
    """

    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("dictionary", PlanPartType.KEY),
        ("attribute", PlanPartType.KEY),
        ("keys", PlanPartType.EXPRESSIONS),
        ("get_literal_keys", PlanPartType.KEY_METHOD),
        ("default", PlanPartType.EXPRESSION),
        ("get_output_field_type", PlanPartType.KEY_METHOD),
        ("output_field", PlanPartType.NONE),
    )

    def __init__(
        self,
        dictionary: str,
        attribute: str,
        key: Any,
        *,
        output_field: Field[Any],
        default: Any = None,
    ) -> None:
        for argument_name, argument in (("dictionary", dictionary), ("attribute", attribute)):
            if not isinstance(argument, str) or not argument:
                raise QueryError(
                    f"DictGet({argument_name}=...) takes a non-empty string, got "
                    f"{ValueDescriptions.get_shown_value(argument)}"
                )
        if not isinstance(output_field, Field):
            raise QueryError(
                f"DictGet(output_field=...) takes a field, got {ValueDescriptions.get_shown_value(output_field)}"
            )
        key_parts = list(key) if isinstance(key, list | tuple) else [key]
        if not key_parts:
            raise QueryError("DictGet(key=...) takes a field name, an expression, a literal, or a list of them")
        self.dictionary = dictionary
        self.attribute = attribute
        self.keys: tuple[Expression, ...] = tuple(
            F(key_part)
            if isinstance(key_part, str)
            else key_part
            if isinstance(key_part, Expression)
            else Value(key_part)
            for key_part in key_parts
        )
        self.output_field = output_field
        self.default: Expression | None = (
            None if default is None else default if isinstance(default, Expression) else Value(default)
        )

    def __repr__(self) -> str:
        return f"DictGet({self.dictionary!r}, {self.attribute!r}, {list(self.keys)!r})"

    def get_output_field_type(self) -> str:
        """The class of the field the value is read as - two expressions of one SQL text read their
        values differently by it."""
        return ClassPath.get(type(self.output_field))

    def get_literal_keys(self) -> tuple[Any, ...]:
        """The literal keys, in the plan's key - a query of another literal is built again, its key
        checked by a declared dictionary's key field.

        Returns:
            The literals, the text of one that isn't hashable.
        """
        return tuple(
            key.value if isinstance(key.value, Hashable) else repr(key.value)
            for key in self.keys
            if isinstance(key, Value)
        )

    def get_declaration(self) -> tuple[type[Model], ClickhouseDictionary] | None:
        """The model declaring the dictionary and its declaration, among the models of the active
        context.

        Returns:
            The model and the dictionary, None for a dictionary no model declares.
        """
        context = HareContext.get_current()
        apps = context.apps if context is not None else None
        if apps is None:
            return None
        for model in apps.get_models_iterable():
            for dictionary in model._meta.dictionaries:
                if dictionary.name == self.dictionary and isinstance(dictionary, ClickhouseDictionary):
                    return model, dictionary
        return None

    def get_attribute_column(self, types: TypeRegistry) -> str:
        """The dictionary's column of the attribute read - checked against a declared dictionary.

        Args:
            types: The types of the dialect a key literal is written by.

        Returns:
            The column.

        Raises:
            QueryError: A declared dictionary has no such attribute or another number of keys.
            ValidationError: A key literal isn't a value of the dictionary's key field.
        """
        declaration = self.get_declaration()
        if declaration is None:
            return self.attribute
        model, dictionary = declaration
        fields_db_projection = model._meta.fields_db_projection
        columns_by_attribute = {name: fields_db_projection[name] for name in dictionary.attributes}
        column = columns_by_attribute.get(self.attribute)
        if column is None and self.attribute not in columns_by_attribute.values():
            raise QueryError(
                f"DictGet: the dictionary {self.dictionary!r} has no attribute {self.attribute!r} - it gives "
                f"{', '.join(dictionary.attributes)}"
            )
        if len(self.keys) != len(dictionary.key):
            raise QueryError(
                f"DictGet: the dictionary {self.dictionary!r} is keyed by {', '.join(dictionary.key)} - got "
                f"{len(self.keys)} key(s)"
            )
        for key, key_field_name in zip(self.keys, dictionary.key, strict=True):
            if isinstance(key, Value):
                key_field = model._meta.fields_map[key_field_name]
                # Range and type checked as a value written to the field is.
                key_field.validate(types.get_db_value(key_field, key.value, model))
        return column or self.attribute

    def get_qualified_name(self, expression_context: ExpressionContext) -> str:
        """The dictionary's name with its database - the database of the model declaring it, else the
        connection's: the shards a distributed table's query runs on look a bare name up in their default
        database.

        Args:
            expression_context: The context the query is built in.

        Returns:
            The name - as given when it names a database already, or none is known.
        """
        if "." in self.dictionary:
            return self.dictionary
        declaration = self.get_declaration()
        database = declaration[0]._meta.schema if declaration is not None else None
        if database is None and expression_context.connection is not None:
            database = getattr(expression_context.connection, "database", None)
        return self.dictionary if database is None else f"{database}.{self.dictionary}"

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        literals = expression_context.dialect.literals
        attribute_column = self.get_attribute_column(expression_context.dialect.types)
        key_results = [key.get_result(expression_context) for key in self.keys]
        joins = [join for key_result in key_results for join in key_result.joins]
        key_term = (
            key_results[0].term
            if len(key_results) == 1
            else Function(CLICKHOUSE_TUPLE_FUNCTION, *(key_result.term for key_result in key_results))
        )
        arguments = [
            LiteralValue(literals.get_string_literal_sql(self.get_qualified_name(expression_context))),
            LiteralValue(literals.get_string_literal_sql(attribute_column)),
            key_term,
        ]
        function_name = CLICKHOUSE_DICTIONARY_FUNCTION
        if self.default is not None:
            default_result = self.default.get_result(expression_context)
            joins.extend(default_result.joins)
            arguments.append(default_result.term)
            function_name = CLICKHOUSE_DICTIONARY_DEFAULT_FUNCTION
        return ExpressionResult(term=Function(function_name, *arguments), joins=joins, output_field=self.output_field)

    def get_value_field(self, result: ExpressionResult) -> Field[Any] | None:
        return self.output_field
