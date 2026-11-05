from __future__ import annotations

import json
from typing import Any

from hare.classes.class_path import ClassPath
from hare.classes.comparable_values import ComparableValues
from hare.ddl.indexes.ordered_index_key import OrderedIndexKey
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.query.expressions import Ordering, Q
from hare.sql.constants import NEUTRAL_SQL_CONTEXT
from hare.sql.terms.term import Term


class DeclarationValues(ComparableValues):
    """Comparable declarations without generating Python source or allocating import aliases.

    Constructor-backed objects use the common deconstruction protocol. Conditions
    and orderings retain their semantic inputs explicitly; raw SQL remains opaque
    and compares by its exact text.
    """

    @classmethod
    def get_object_value(cls, value: Any) -> object:
        """Describe expression containers that do not expose a constructor deconstruction."""
        if isinstance(value, RawSQLTerm):
            return [ClassPath.get(type(value)), value.sql]
        if isinstance(value, OrderedIndexKey):
            return [ClassPath.get(type(value)), cls.get(value.term), value.order.value]
        if isinstance(value, Term):
            # An already constructed SQL term is serialized as opaque RawSQLTerm text.
            return [ClassPath.get(RawSQLTerm), value.get_sql(NEUTRAL_SQL_CONTEXT)]
        if isinstance(value, Ordering):
            return [ClassPath.get(type(value)), cls.get(value.target), value.order.value]
        if isinstance(value, Q):
            return [
                ClassPath.get(type(value)),
                value.connector.value,
                value.get_negation_prefix(),
                cls.get(value.children),
                cls.get(value.filters),
                cls.get(value.expression),
            ]
        return super().get_object_value(value)

    @classmethod
    def get_key(cls, value: Any) -> str:
        """A deterministic, hashable encoding of the declaration's comparable structure."""
        return json.dumps(cls.get(value), sort_keys=True)
