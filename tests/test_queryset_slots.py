"""Regression test for a __slots__ redeclaration bug: query classes used to redeclare slot names
already declared on a base class (e.g. "_db"). Redeclaring an inherited slot creates a second,
shadowing member-descriptor on the subclass instead of reusing the parent's - every query class
must inherit its bases' descriptors instead.
"""

import hare.query.statements.select.combined_query
import hare.query.statements.select.raw_sql_query
import hare.query.statements.select.values_query
import hare.query.statements.summary.aggregate_query
import hare.query.statements.summary.contains_query
import hare.query.statements.summary.count_query
import hare.query.statements.summary.exists_query
import hare.query.statements.write.bulk_create_query
import hare.query.statements.write.bulk_update_query
import hare.query.statements.write.delete_query
import hare.query.statements.write.update_query  # noqa: F401
from hare.query.queryset import QuerySet
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements import AwaitableQuery, BulkCreateQuery, DeleteQuery, RawSQLQuery


def get_all_subclasses(cls: type) -> list[type]:
    subclasses = []
    for subclass in cls.__subclasses__():
        subclasses.append(subclass)
        subclasses.extend(get_all_subclasses(subclass))
    return subclasses


def test_no_query_class_redeclares_an_inherited_slot():
    redeclared = []
    for query_class in {QuerySpec, *get_all_subclasses(QuerySpec)}:
        own_slots = query_class.__dict__.get("__slots__", ())
        if isinstance(own_slots, str):
            own_slots = (own_slots,)
        for base in query_class.__mro__[1:]:
            base_slots = base.__dict__.get("__slots__", ())
            if isinstance(base_slots, str):
                base_slots = (base_slots,)
            redeclared.extend(
                f"{query_class.__qualname__}.{name} (declared on {base.__qualname__})"
                for name in set(own_slots) & set(base_slots)
            )
    assert redeclared == []


def test_queryset_reuses_parent_db_descriptor():
    assert "_db" not in QuerySet.__dict__
    assert getattr(QuerySet, "_db") is QuerySpec.__dict__["_db"]  # noqa: B009


def test_raw_sql_query_reuses_parent_db_descriptor():
    assert "_db" not in RawSQLQuery.__dict__
    assert getattr(RawSQLQuery, "_db") is QuerySpec.__dict__["_db"]  # noqa: B009


def test_bulk_create_query_reuses_parent_db_descriptor():
    assert "_db" not in BulkCreateQuery.__dict__
    assert getattr(BulkCreateQuery, "_db") is QuerySpec.__dict__["_db"]  # noqa: B009


def test_delete_query_reuses_parent_descriptors():
    assert "_db" not in DeleteQuery.__dict__
    assert "_db" not in AwaitableQuery.__dict__
