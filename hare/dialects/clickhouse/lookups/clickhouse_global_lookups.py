from __future__ import annotations

from functools import partial
from typing import Any

from hare.dialects.clickhouse.enums import ClickhouseDialectName, ClickhouseLookup
from hare.dialects.clickhouse.lookups.global_contains_criterion import GlobalContainsCriterion
from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.query.enums import Lookup, LookupValueShape
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple


class ClickhouseGlobalLookups:
    """``__global_in`` and ``__global_not_in`` - ``__in``/``__not_in`` written ``GLOBAL IN``, for a
    ``Distributed`` table whose shards would otherwise each read the list's subquery themselves."""

    @staticmethod
    def get_container(value: Any) -> Term:
        """The list or subquery a ``GLOBAL IN`` reads.

        Args:
            value: The encoded values, or a subquery.

        Returns:
            The container.
        """
        if isinstance(value, list | tuple | set):
            return Tuple(*Lookups.get_in_list_terms(list(value)))
        return value

    @staticmethod
    def global_in(term: Term, value: Any) -> Criterion:
        """``term GLOBAL IN (...)`` - a None in a list matches a NULL, as ``__in`` does.

        Args:
            term: The compared term.
            value: The encoded values, or a subquery.

        Returns:
            The criterion.
        """
        if not isinstance(value, list | tuple | set):
            return GlobalContainsCriterion(term, value)
        non_null_values = [item for item in value if item is not None]
        if not non_null_values:
            return term.isnull() if value else Lookups.is_in(term, [])
        criterion: Criterion = GlobalContainsCriterion(term, ClickhouseGlobalLookups.get_container(non_null_values))
        return criterion | term.isnull() if len(non_null_values) != len(value) else criterion

    @staticmethod
    def global_not_in(term: Term, value: Any) -> Criterion:
        """``term GLOBAL NOT IN (...)`` - NULL kept unless a None is in the list, as ``__not_in`` does.

        Args:
            term: The compared term.
            value: The encoded values, or a subquery.

        Returns:
            The criterion.
        """
        if not isinstance(value, list | tuple | set):
            return GlobalContainsCriterion(term, value).is_not_true()
        non_null_values = [item for item in value if item is not None]
        if not non_null_values:
            return term.notnull() if value else Lookups.not_in(term, [])
        criterion: Criterion = GlobalContainsCriterion(
            term, ClickhouseGlobalLookups.get_container(non_null_values)
        ).negate()
        return criterion if len(non_null_values) != len(value) else criterion | term.isnull()

    @staticmethod
    def get_global_lookup(lookup: Lookup, field: Field[Any] | None) -> FieldLookup:
        """The ``GLOBAL`` lookup of a value - its own ``__in``/``__not_in`` with the ``GLOBAL`` operator.

        Args:
            lookup: ``Lookup.IN`` or ``Lookup.NOT_IN``.
            field: The field, None for a value with no field.

        Returns:
            The lookup.

        A value whose own list lookup isn't a plain ``IN`` - a key of several columns, a range, a JSON
        value - gets a lookup raising ``QueryError`` when it is used.
        """
        operator, plain_operator = (
            (ClickhouseGlobalLookups.global_in, Lookups.is_in)
            if lookup == Lookup.IN
            else (ClickhouseGlobalLookups.global_not_in, Lookups.not_in)
        )
        if field is None:
            return FieldLookup(operator, ValueEncoders.encode_list)
        own_lookup = field.get_lookups().get(lookup)
        if own_lookup is None or own_lookup.operator is not plain_operator:
            return FieldLookup(partial(ClickhouseGlobalLookups.reject, lookup, type(field).__name__))
        return own_lookup.with_changes(operator=operator)

    @staticmethod
    def reject(lookup: Lookup, field_class_name: str, term: Term, value: Any) -> Criterion:
        """Backs a ``GLOBAL`` lookup of a value without a plain list lookup.

        Raises:
            QueryError: Always.
        """
        raise QueryError(
            f"__global_{lookup} compares a value of one column as __{lookup} does - {field_class_name} has no "
            "such list lookup"
        )

    @classmethod
    def register(cls) -> None:
        """Adds ``__global_in`` and ``__global_not_in`` to every value on ClickHouse."""
        for name, lookup in ((ClickhouseLookup.GLOBAL_IN, Lookup.IN), (ClickhouseLookup.GLOBAL_NOT_IN, Lookup.NOT_IN)):
            Field.register_lookup(
                name,
                partial(cls.get_global_lookup, lookup),
                value_shape=LookupValueShape.LIST,
                dialects=(ClickhouseDialectName.CLICKHOUSE,),
            )
