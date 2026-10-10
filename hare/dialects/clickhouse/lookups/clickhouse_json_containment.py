from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_JSON_CONTAINMENT_FUNCTION_NAME,
    CLICKHOUSE_JSON_SAME_SCALAR_SQL,
)
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.criteria.criterion import Criterion
    from hare.sql.terms.term import Term


class ClickhouseJsonContainment(Function):
    """PostgreSQL's jsonb containment over JSON text - ``@>`` (``contains``) and ``<@``
    (``contained_by``) - written from the compared value: an object by its keys, an array by its
    elements (order and repeats ignored), a scalar by its normalized JSON text; at the top an array
    also contains a scalar element. The value's scalars and keys are bound - the SQL text depends on
    its shape alone (the plan key holds it).

    Args:
        document: The JSON text of the column - a JSON column read by ``toString()``.
        value: The compared value, decoded.
        contains: Whether the document contains the value - else the value contains the document.
    """

    def __init__(self, document: Term, value: Any, *, contains: bool) -> None:
        self.value = value
        self.contains = contains
        self.leaves: list[Any] = []
        self.collect_leaves(value)
        leaf_terms = (ValueWrapper(leaf) for leaf in self.leaves)
        super().__init__(CLICKHOUSE_JSON_CONTAINMENT_FUNCTION_NAME, document, *leaf_terms)

    def collect_leaves(self, value: Any) -> None:
        """Collects the bound parts of a value in the order the SQL reads them - a key, then its value;
        an element; a scalar's JSON text.

        Args:
            value: The value.
        """
        if isinstance(value, dict):
            for key, item in value.items():
                self.leaves.append(key)
                self.collect_leaves(item)
        elif isinstance(value, list):
            for item in value:
                self.collect_leaves(item)
        else:
            self.leaves.append(json.dumps(value, separators=(",", ":")))

    @classmethod
    def get_criterion(cls, document: Term, value_text: str, *, contains: bool) -> Criterion:
        """The containment test as a condition.

        Args:
            document: The JSON text of the column.
            value_text: The compared value's JSON text.
            contains: Whether the document contains the value.

        Returns:
            The condition.
        """
        test = cls(document, json.loads(value_text), contains=contains)
        return BasicCriterion(Equality.EQ, test, ValueWrapper(1, allow_parametrize=False))

    def get_function_sql(self, sql_context: SqlContext) -> str:
        document_sql, *leaf_sqls = (self.get_arg_sql(argument, sql_context) for argument in self.args)
        leaves = iter(leaf_sqls)
        # The JSON text - of a JSON column, or of a String one holding it; '' for NULL, a document of no
        # type the whole test is NULL for.
        document_sql = f"ifNull(toString({document_sql}), '')"
        if self.contains:
            return f"toUInt8(ifNull({self.get_contains_sql(document_sql, self.value, leaves, 0, top_level=True)}, 0))"
        return f"toUInt8(ifNull({self.get_contained_sql(document_sql, self.value, leaves, 0, top_level=True)}, 0))"

    @staticmethod
    def get_same_scalar_sql(document_sql: str, scalar_sql: str) -> str:
        """Whether a JSON text is the same scalar as a bound one - by their normalized texts.

        Args:
            document_sql: The JSON text.
            scalar_sql: The bound scalar's JSON text.

        Returns:
            The test.
        """
        return CLICKHOUSE_JSON_SAME_SCALAR_SQL.format(document=document_sql, scalar=scalar_sql)

    def get_contains_sql(self, document_sql: str, value: Any, leaves: Any, depth: int, *, top_level: bool) -> str:
        """Whether a JSON text contains a value.

        Args:
            document_sql: The JSON text.
            value: The value.
            leaves: The bound parts' SQL, read in order.
            depth: The nesting depth - a lambda's parameter name per depth.
            top_level: Whether the text is the whole document.

        Returns:
            The test.
        """
        element_sql = f"hare_json_element_{depth}"
        if isinstance(value, dict):
            tests = [f"JSONType({document_sql}) = 'Object'"]
            for item in value.values():
                key_sql = next(leaves)
                item_sql = f"JSONExtractRaw({document_sql}, {key_sql})"
                tests.append(f"JSONHas({document_sql}, {key_sql})")
                tests.append(self.get_contains_sql(item_sql, item, leaves, depth + 1, top_level=False))
            return "(" + " AND ".join(tests) + ")"
        if isinstance(value, list):
            tests = [f"JSONType({document_sql}) = 'Array'"]
            for item in value:
                item_test = self.get_contains_sql(element_sql, item, leaves, depth + 1, top_level=False)
                tests.append(f"arrayExists({element_sql} -> {item_test}, JSONExtractArrayRaw({document_sql}))")
            return "(" + " AND ".join(tests) + ")"
        scalar_sql = next(leaves)
        same_sql = self.get_same_scalar_sql(document_sql, scalar_sql)
        if not top_level:
            return same_sql
        # At the top an array contains a scalar element too.
        element_test = self.get_same_scalar_sql(element_sql, scalar_sql)
        return (
            f"({same_sql} OR (JSONType({document_sql}) = 'Array' AND "
            f"arrayExists({element_sql} -> {element_test}, JSONExtractArrayRaw({document_sql}))))"
        )

    def get_contained_sql(self, document_sql: str, value: Any, leaves: Any, depth: int, *, top_level: bool) -> str:
        """Whether a value contains a JSON text.

        Args:
            document_sql: The JSON text.
            value: The value.
            leaves: The bound parts' SQL, read in order.
            depth: The nesting depth - a lambda's parameter name per depth.
            top_level: Whether the text is the whole document.

        Returns:
            The test.
        """
        element_sql = f"hare_json_element_{depth}"
        if isinstance(value, dict):
            key_sql = f"hare_json_key_{depth}"
            branches = []
            for item in value.values():
                bound_key_sql = next(leaves)
                item_test = self.get_contained_sql(
                    f"JSONExtractRaw({document_sql}, {key_sql})", item, leaves, depth + 1, top_level=False
                )
                branches.append(f"{key_sql} = {bound_key_sql}, {item_test}")
            key_test = f"multiIf({', '.join(branches)}, 0)" if branches else "0"
            return (
                f"(JSONType({document_sql}) = 'Object' AND "
                f"arrayAll({key_sql} -> {key_test}, JSONExtractKeys({document_sql})))"
            )
        if isinstance(value, list):
            element_tests = []
            scalar_leaf_sqls = []
            for item in value:
                if isinstance(item, (dict, list)):
                    element_tests.append(self.get_contained_sql(element_sql, item, leaves, depth + 1, top_level=False))
                else:
                    leaf_sql = next(leaves)
                    scalar_leaf_sqls.append(leaf_sql)
                    element_tests.append(self.get_same_scalar_sql(element_sql, leaf_sql))
            any_item_sql = " OR ".join(element_tests) if element_tests else "0"
            array_sql = (
                f"(JSONType({document_sql}) = 'Array' AND "
                f"arrayAll({element_sql} -> ({any_item_sql}), JSONExtractArrayRaw({document_sql})))"
            )
            if not top_level or not scalar_leaf_sqls:
                return array_sql
            # At the top a scalar is contained by an array holding it.
            scalar_sql = " OR ".join(self.get_same_scalar_sql(document_sql, leaf_sql) for leaf_sql in scalar_leaf_sqls)
            return f"({array_sql} OR (JSONType({document_sql}) NOT IN ('Array', 'Object') AND ({scalar_sql})))"
        return self.get_same_scalar_sql(document_sql, next(leaves))
