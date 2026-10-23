from __future__ import annotations

from enum import StrEnum


class PlanPartType(StrEnum):
    """How an attribute of a plannable class meets the plan of the query it is in - declared per
    attribute in the class's ``plan_parts``; the class's description is generated from them."""

    #: Written into the SQL text as it is - the key holds the value; a name of a field or an
    #: annotation among them: the values of an annotation are bound where it is described.
    KEY = "key"
    #: A sequence written into the SQL text - field names, orderings; the key holds it as a tuple.
    KEYS = "keys"
    #: A method giving what is written into the SQL text - the key holds what it returns.
    KEY_METHOD = "key_method"
    #: A literal bound as a parameter - the key holds its cast and literal type, the value is bound.
    LITERAL = "literal"
    #: An argument: an expression by its own description, a ``None`` as ``NULL``, a SQL term by its
    #: text and values, any other literal by its type, bound - a list or tuple keeps no plan.
    ARGUMENT = "argument"
    #: A sequence of arguments.
    ARGUMENTS = "arguments"
    #: A method giving an argument, described as an ``ARGUMENT``.
    ARGUMENT_METHOD = "argument_method"
    #: An argument whose literal the build encodes into one bound value - a point as its text, a
    #: vector: described as an ``ARGUMENT``, a list or tuple literal bound whole.
    ENCODED_ARGUMENT = "encoded_argument"
    #: A sequence of parameters made with the object (``ValueWrapper``s) - the key holds their
    #: count, each one's value is bound.
    PARAMETERS = "parameters"
    #: A method giving a value bound as a parameter - the key holds its literal type.
    VALUE_METHOD = "value_method"
    #: What a function reads: an expression by its own description, a field or annotation by its
    #: name, a SQL term by its text and values - anything else keeps no plan.
    FIELD = "field"
    #: A sequence of what a function reads, each described as a ``FIELD``.
    FIELDS = "fields"
    #: An expression or condition by its own description - None as absent.
    EXPRESSION = "expression"
    #: A sequence of expressions or conditions.
    EXPRESSIONS = "expressions"
    #: A method giving an expression or condition, described by its own description - None as absent.
    EXPRESSION_METHOD = "expression_method"
    #: A method giving a condition the build folds into JOINs - described by its own description,
    #: its values bound into each JOIN it is folded into, into none when it is folded into none.
    JOIN_CONDITION_METHOD = "join_condition_method"
    #: A condition's filters - each key with its value's structure (``FilterPlanDescriptions``).
    FILTERS = "filters"
    #: A query built into the statement - the connection it is pinned to and its own description.
    QUERY = "query"
    #: A method giving a query built into the statement, described as a ``QUERY``.
    QUERY_METHOD = "query_method"
    #: An object giving its own plan key (``get_plan_key()``) - None as absent.
    PLAN_KEY = "plan_key"
    #: A method telling whether the object keeps a plan at all - the description is None when it
    #: returns False; no part of the key.
    KEEPS_PLAN_METHOD = "keeps_plan_method"
    #: No part of its own - described through another part (a method reading it), or no part of
    #: the SQL text at all: a cache or bookkeeping of the object.
    NONE = "none"


class PlanKeyForm(StrEnum):
    """How a setting of a query meets the key of its plan - declared per setting in the query
    class's ``plan_slots``; the description of the statement is generated from them
    (``QueryKeyCompiler``). A slot names an attribute or a method of the query - or is a function
    taking the query, for a part every type of query describes the same way."""

    #: Written into the SQL text as it is - the key holds the value.
    VALUE = "value"
    #: A sequence written into the SQL text - the key holds it as a tuple.
    TUPLE = "tuple"
    #: An unordered collection written into the SQL text - the key holds it sorted, as a tuple.
    SORTED_TUPLE = "sorted_tuple"
    #: Written into the SQL text when set - the key holds whether it is set (not None).
    PRESENCE = "presence"
    #: A method or function giving what is written into the SQL text - the key holds what it returns.
    METHOD = "method"
    #: The connection - the key holds its dialect and alias; built into another query, the alias it
    #: is pinned to alone (the dialect is that query's).
    CONNECTION = "connection"
    #: A part described by a method or function (``PlanDescription``) - the key holds its structure,
    #: its values are bound; None keeps no plan.
    DESCRIBED = "described"
    #: The conditions in a sequence, each by its own description, as the most frequent query's
    #: filters are.
    CONDITIONS = "conditions"
    #: A value bound as a parameter when set - the key holds whether it is set (not None).
    BOUND = "bound"
    #: A value bound as a parameter when true - the key holds whether it is.
    BOUND_WHEN_TRUE = "bound_when_true"
    #: A slice bound as a parameter of a query built into another one - the key holds whether it
    #: is set; a query of its own binds its slice apart (``plan_binds_slice``).
    BOUND_INTO_ANOTHER = "bound_into_another"
    #: A method giving values bound as parameters - the key holds how many.
    BOUND_VALUES_METHOD = "bound_values_method"
