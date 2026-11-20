"""Regression test for a __slots__ redeclaration bug: query classes used to redeclare slot names
already declared on a base class (e.g. "_db"). Redeclaring an inherited slot creates a second,
shadowing member-descriptor on the subclass instead of reusing the parent's - every query class
must inherit its bases' descriptors instead.
"""

from types import SimpleNamespace

import pytest

import hare.query.statements.select.combined_query
import hare.query.statements.select.raw_sql_query
import hare.query.statements.select.values_query
import hare.query.statements.summary.aggregate_query
import hare.query.statements.summary.contains_query
import hare.query.statements.summary.count_query
import hare.query.statements.summary.exists_query
import hare.query.statements.write.bulk.bulk_update_query
import hare.query.statements.write.bulk.create.bulk_create_query
import hare.query.statements.write.delete_query
import hare.query.statements.write.update_query  # noqa: F401
from hare.query.enums import SpecificationSlotPlanRole
from hare.query.queryset import QuerySet
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements import AwaitableQuery, BulkCreateQuery, DeleteQuery, RawSQLQuery

pytestmark = pytest.mark.database_independent


def get_all_subclasses(cls: type) -> list[type]:
    subclasses = []
    for subclass in cls.__subclasses__():
        subclasses.append(subclass)
        subclasses.extend(get_all_subclasses(subclass))
    return subclasses


def test_no_query_class_redeclares_an_inherited_slot():
    redeclared = []
    for query_class in {QuerySpecification, *get_all_subclasses(QuerySpecification)}:
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


@pytest.mark.parametrize("query_class", [QuerySet, RawSQLQuery, BulkCreateQuery])
def test_a_query_class_reuses_parent_db_descriptor(query_class):
    assert "_connection" not in query_class.__dict__
    assert getattr(query_class, "_connection") is QuerySpecification.__dict__["_connection"]  # noqa: B009


def test_delete_query_reuses_parent_descriptors():
    assert "_connection" not in DeleteQuery.__dict__
    assert "_connection" not in AwaitableQuery.__dict__


@pytest.mark.parametrize("connection", [None, SimpleNamespace(connection_handler=None)])
def test_sharing_connection_copies_every_connection_slot_and_keeps_other_settings(monkeypatch, connection):
    # Opaque markers exercise slot transfer without choosing a route or initializing a model.
    source = object.__new__(QuerySet)
    target = object.__new__(QuerySet)
    source_values = {name: object() for name in QuerySpecification.SPECIFICATION_SLOTS}
    target_values = {name: object() for name in QuerySpecification.SPECIFICATION_SLOTS}
    source_values["_connection"] = connection
    for name in source_values:
        setattr(source, name, source_values[name])
        setattr(target, name, target_values[name])

    applied_connections = []
    apply_connection = QuerySet._apply_connection

    def record_connection(query, connection):
        applied_connections.append((query, connection))
        apply_connection(query, connection)

    monkeypatch.setattr(QuerySet, "_apply_connection", record_connection)

    assert source._share_connection(target) is target
    assert applied_connections == [(target, source_values["_connection"])]
    for name, role in QuerySpecification.SLOT_PLAN_ROLES.items():
        expected = (
            source_values[name]
            if role is SpecificationSlotPlanRole.CONNECTION or name == "_binding"
            else target_values[name]
        )
        assert getattr(target, name) is expected, name
        assert getattr(source, name) is source_values[name], name
