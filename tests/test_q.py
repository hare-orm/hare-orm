import inspect
from typing import Any

import pytest

from hare.exceptions import (
    QueryError,
)
from hare.query.enums import Connector
from hare.query.expressions import ExpressionContext, F, Q
from hare.sql.context import DEFAULT_SQL_CONTEXT
from tests.testmodels import CharFields, IntFields

# =============================================================================
# Tests for Q object basic operations (no database needed)
# =============================================================================


def test_q_basic():
    q = Q(moo="cow")
    assert q.children == ()
    assert q.filters == {"moo": "cow"}
    assert q.connector == "AND"


def test_q_to_bool():
    q = Q(row="data")
    q_negative = ~q
    q_empty = Q()
    q_children = Q(Q(row="data"), Q(row="data"))
    q_children_empty = Q(Q(), Q())
    q_children_empty_or = Q.with_connector(Connector.OR, Q(), Q())
    assert bool(q) is True
    assert bool(q_negative) is True
    assert bool(q_negative) is True
    assert bool(q_empty) is False
    assert bool(q_children) is True
    assert bool(q_children_empty) is False
    assert bool(q_children_empty_or) is False


def test_q_compound():
    q1 = Q(moo="cow")
    q2 = Q(moo="bull")
    q = Q.with_connector(Connector.OR, q1, q2)

    assert q1.children == ()
    assert q1.filters == {"moo": "cow"}
    assert q1.connector == "AND"

    assert q2.children == ()
    assert q2.filters == {"moo": "bull"}
    assert q2.connector == "AND"

    assert q.children == (q1, q2)
    assert q.filters == {}
    assert q.connector == "OR"


def test_q_compound_or():
    q1 = Q(moo="cow")
    q2 = Q(moo="bull")
    q = q1 | q2

    assert q1.children == ()
    assert q1.filters == {"moo": "cow"}
    assert q1.connector == "AND"

    assert q2.children == ()
    assert q2.filters == {"moo": "bull"}
    assert q2.connector == "AND"

    assert q.children == (q1, q2)
    assert q.filters == {}
    assert q.connector == "OR"


def test_q_compound_and():
    q1 = Q(moo="cow")
    q2 = Q(moo="bull")
    q = q1 & q2

    assert q1.children == ()
    assert q1.filters == {"moo": "cow"}
    assert q1.connector == "AND"

    assert q2.children == ()
    assert q2.filters == {"moo": "bull"}
    assert q2.connector == "AND"

    assert q.children == (q1, q2)
    assert q.filters == {}
    assert q.connector == "AND"


def test_q_compound_or_notq():
    with pytest.raises(QueryError, match="OR operation requires a Q node"):
        Q() | 2  # pylint: disable=W0106


def test_q_compound_and_notq():
    with pytest.raises(QueryError, match="AND operation requires a Q node"):
        Q() & 2  # pylint: disable=W0106


def test_q_notq():
    with pytest.raises(QueryError, match="All ordered arguments must be Q nodes"):
        Q(Q(), 1)


def test_q_bad_connector():
    with pytest.raises(QueryError, match="connector must be Connector.AND or Connector.OR"):
        Q.with_connector(3)


def test_q_partial_or():
    q = Q.with_connector(Connector.OR, moo="cow")
    assert q.children == ()
    assert q.filters == {"moo": "cow"}
    assert q.connector == "OR"


def test_q_equality():
    # basic query
    basic_q1 = Q(moo="cow")
    basic_q2 = Q(moo="cow")
    assert basic_q1 == basic_q2

    # and query
    and_q1 = Q(firstname="John") & Q(lastname="Doe")
    and_q2 = Q(firstname="John") & Q(lastname="Doe")
    assert and_q1 == and_q2

    # or query
    or_q1 = Q(firstname="John") | Q(lastname="Doe")
    or_q2 = Q(firstname="John") | Q(lastname="Doe")
    assert or_q1 == or_q2

    # complex query
    complex_q1 = (Q(firstname="John") & Q(lastname="Doe")) | Q(mother_name="Jane")
    complex_q2 = (Q(firstname="John") & Q(lastname="Doe")) | Q(mother_name="Jane")
    assert complex_q1 == complex_q2


def test_q_inequality():
    assert Q(moo="cow") != Q(moo="bull")
    assert Q(moo="cow") != Q.with_connector(Connector.OR, moo="cow")
    assert Q(moo="cow") != "not a q"


def test_q_hash_follows_equality():
    assert hash(Q(moo="cow", id__in=[1, 2])) == hash(Q(id__in=[1, 2], moo="cow"))
    assert hash(~Q(moo="cow") | Q(a=1)) == hash(~Q(moo="cow") | Q(a=1))
    assert len({Q(moo="cow"), Q(moo="cow"), ~Q(moo="cow")}) == 2


# =============================================================================
# Tests for Q object resolution (requires database for model resolution)
# =============================================================================


@pytest.fixture
def int_fields_context(db):
    """Context for IntFields model resolution."""
    return ExpressionContext(
        model=IntFields,
        table=IntFields._meta.basequery,
        annotations={},
        dialect=IntFields._meta.db.dialect,
        connection=IntFields._meta.db,
    )


@pytest.fixture
def char_fields_context(db):
    """Context for CharFields model resolution."""
    return ExpressionContext(
        model=CharFields,
        table=CharFields._meta.basequery,
        annotations={},
        dialect=CharFields._meta.db.dialect,
        connection=CharFields._meta.db,
    )


def test_q_call_basic(int_fields_context):
    q = Q(id=8)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id"=8'


def test_q_call_basic_or(int_fields_context):
    q = Q.with_connector(Connector.OR, id=8)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id"=8'


def test_q_call_multiple_and(int_fields_context):
    q = Q(id__gt=8, id__lt=10)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">8 AND "id"<10'


def test_q_call_multiple_or(int_fields_context):
    q = Q.with_connector(Connector.OR, id__gt=8, id__lt=10)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">8 OR "id"<10'


def test_q_call_multiple_and2(int_fields_context):
    q = Q(id=8, intnum=80)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id"=8 AND "intnum"=80'


def test_q_call_multiple_or2(int_fields_context):
    q = Q.with_connector(Connector.OR, id=8, intnum=80)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id"=8 OR "intnum"=80'


def test_q_call_complex_int(int_fields_context):
    q = Q(Q(intnum=80), Q.with_connector(Connector.OR, id__lt=5, id__gt=50))
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"intnum"=80 AND ("id"<5 OR "id">50)'


def test_q_call_complex_int2(int_fields_context):
    q = Q(Q(intnum="80"), Q.with_connector(Connector.OR, Q(id__lt="5"), Q(id__gt="50")))
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"intnum"=80 AND ("id"<5 OR "id">50)'


def test_q_call_complex_int3(int_fields_context):
    q = Q(Q.with_connector(Connector.OR, id__lt=5, id__gt=50), intnum=80)
    r = q.get_result(int_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"intnum"=80 AND ("id"<5 OR "id">50)'


def test_q_call_complex_char(char_fields_context):
    q = Q(Q(char_null=80), ~Q.with_connector(Connector.OR, char__lt=5, char__gt=50))
    r = q.get_result(char_fields_context)
    assert (
        r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == "\"char_null\"='80' AND NOT (\"char\"<'5' OR \"char\">'50')"
    )


def test_q_call_complex_char2(char_fields_context):
    q = Q(
        Q(char_null="80"),
        ~Q.with_connector(Connector.OR, Q(char__lt="5"), Q(char__gt="50")),
    )
    r = q.get_result(char_fields_context)
    assert (
        r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == "\"char_null\"='80' AND NOT (\"char\"<'5' OR \"char\">'50')"
    )


def test_q_call_complex_char3(char_fields_context):
    q = Q(~Q.with_connector(Connector.OR, char__lt=5, char__gt=50), char_null=80)
    r = q.get_result(char_fields_context)
    assert (
        r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == "\"char_null\"='80' AND NOT (\"char\"<'5' OR \"char\">'50')"
    )


def test_q_call_with_blank_and(char_fields_context):
    q = Q(Q(id__gt=5), Q())
    r = q.get_result(char_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5'


def test_q_call_with_blank_or(char_fields_context):
    q = Q.with_connector(Connector.OR, Q(id__gt=5), Q())
    r = q.get_result(char_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5'


def test_q_call_with_blank_and2(char_fields_context):
    q = Q(id__gt=5) & Q()
    r = q.get_result(char_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5'


def test_q_call_with_blank_or2(char_fields_context):
    q = Q(id__gt=5) | Q()
    r = q.get_result(char_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5'


def test_q_call_with_blank_and3(char_fields_context):
    q = Q() & Q(id__gt=5)
    r = q.get_result(char_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5'


def test_q_call_with_blank_or3(char_fields_context):
    q = Q() | Q(id__gt=5)
    r = q.get_result(char_fields_context)
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5'


def test_q_call_annotations_resolved(db):
    q = Q(id__gt=5) | Q(annotated__lt=5)
    r = q.get_result(
        ExpressionContext(
            model=IntFields,
            table=IntFields._meta.basequery,
            annotations={"annotated": F("intnum")},
            dialect=IntFields._meta.db.dialect,
            connection=IntFields._meta.db,
        )
    )
    assert r.where_criterion.get_sql(DEFAULT_SQL_CONTEXT) == '"id">5 OR "intnum"<5'


def test_q_takes_a_filter_on_any_field_name():
    q = Q(join_type="inner", connector="usb")
    assert q.filters == {"join_type": "inner", "connector": "usb"}
    assert q.connector == Connector.AND
    either = Q.with_connector(Connector.OR, join_type="inner", connector="usb")
    assert either.filters == {"join_type": "inner", "connector": "usb"}
    assert either.connector == Connector.OR


def test_q_constructor_takes_only_conditions():
    """Nothing but conditions: ``Q(**filters)`` accepts a filter on every field name, and a
    ``dict[str, str]`` of filters type-checks as its ``**filters``."""
    spec = inspect.getfullargspec(Q.__init__)
    assert (spec.args, spec.varargs, spec.varkw, spec.kwonlyargs) == (["self"], "conditions", "filters", [])
    assert spec.annotations["filters"] in (Any, "Any")
    assert str(inspect.signature(Q.with_connector)).startswith("(connector: 'Connector', /,")


def test_q_repr_rebuilds_the_connector():
    assert repr(Q.with_connector(Connector.OR, a=1, b=2)) == "Q.with_connector(Connector.OR, a=1, b=2)"
    assert repr(~Q.with_connector(Connector.OR, a=1)) == "~Q.with_connector(Connector.OR, a=1)"
    assert repr(Q(a=1, b=2)) == "Q(a=1, b=2)"
