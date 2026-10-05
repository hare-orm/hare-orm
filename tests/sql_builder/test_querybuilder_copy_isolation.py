"""Characterization tests for QueryBuilder's copy-on-mutation contract.

Every @builder-decorated method must return an independent copy: mutating the
returned query must never change the SQL of the query it was derived from, nor
of any sibling branch derived from the same parent. These tests exercise every
mutable container QueryBuilder carries (_from/_with/_selects/_columns/_values/
_groupbys/_orderbys/_joins/_updates/_select_star_tables/_on_conflict_fields/
_on_conflict_do_updates/_returns, plus PostgreSQL's _distinct_on) through public
builder methods only - they must keep passing unchanged regardless of whether
QueryBuilder.__copy__ eagerly copies every container or lazily copies-on-write.
"""

import pytest

from hare.dialects.postgresql.query import PostgresqlQuery
from hare.dialects.sqlite.query import SqliteQuery
from hare.sql import Field, Table
from hare.sql.analytics import Following, Preceding
from hare.sql.exceptions import QueryException

pytestmark = pytest.mark.database_independent

WIDGET = Table("widget")
GADGET = Table("gadget")


OTHER = Table("other")


def make_union():
    return PostgresqlQuery.from_(WIDGET).select(WIDGET.id).union(PostgresqlQuery.from_(GADGET).select(GADGET.id))


@pytest.mark.parametrize(
    ("make_base", "make_branch", "marker"),
    [
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.from_(GADGET),
            "gadget",
            id="from",
        ),
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.with_(PostgresqlQuery.from_(GADGET).select(GADGET.id), "cte_gadget"),
            "cte_gadget",
            id="with",
        ),
        pytest.param(
            lambda: PostgresqlQuery.into(WIDGET).columns("id").insert(1),
            lambda base: base.columns("name"),
            "name",
            id="columns",
        ),
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.groupby(WIDGET.id),
            "GROUP BY",
            id="groupby",
        ),
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.orderby(WIDGET.id),
            "ORDER BY",
            id="orderby",
        ),
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.join(GADGET).on(WIDGET.id == GADGET.id),
            "JOIN",
            id="join",
        ),
        pytest.param(
            lambda: PostgresqlQuery.update(WIDGET).set("id", 1),
            lambda base: base.set("name", "renamed"),
            "name",
            id="updates",
        ),
        pytest.param(
            lambda: PostgresqlQuery.into(WIDGET).columns("id").insert(1),
            lambda base: base.on_conflict("id").do_nothing(),
            "CONFLICT",
            id="on_conflict_fields",
        ),
        pytest.param(
            lambda: PostgresqlQuery.into(WIDGET).columns("id").insert(1),
            lambda base: base.returning("id"),
            "RETURNING",
            id="returning",
        ),
        pytest.param(
            lambda: SqliteQuery.into(WIDGET).columns("id").insert(1),
            lambda base: base.returning("id"),
            "RETURNING",
            id="returning_sqlite",
        ),
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.distinct_on(Field("name")),
            "DISTINCT ON",
            id="distinct_on",
        ),
        pytest.param(
            make_union,
            lambda base: base.union(PostgresqlQuery.from_(THIRD).select(THIRD.id)),
            "third",
            id="set_operation_union",
        ),
        pytest.param(make_union, lambda base: base.orderby(WIDGET.id), "ORDER BY", id="set_operation_orderby"),
    ],
)
def test_a_branch_does_not_leak(make_base, make_branch, marker):
    base = make_base()
    base_sql = str(base)
    branch = make_branch(base)
    assert str(base) == base_sql
    assert marker not in str(base)
    assert marker in str(branch)


@pytest.mark.parametrize(
    ("make_base", "make_first", "first_marker", "make_second", "second_marker"),
    [
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.select(WIDGET.name),
            "name",
            lambda base: base.select(WIDGET.price),
            "price",
            id="select",
        ),
        pytest.param(
            lambda: PostgresqlQuery.into(WIDGET).columns("id"),
            lambda base: base.insert(1),
            "1",
            lambda base: base.insert(2),
            "2",
            id="values",
        ),
        pytest.param(
            lambda: PostgresqlQuery.from_(WIDGET).select(WIDGET.id),
            lambda base: base.join(GADGET).on(WIDGET.id == GADGET.id),
            "gadget",
            lambda base: base.join(OTHER).on(WIDGET.id == OTHER.id),
            "other",
            id="join",
        ),
        # Rendering an UPDATE with joins moves them into its FROM; this must not leak into a sibling
        # built from the same base UPDATE query.
        pytest.param(
            lambda: PostgresqlQuery.update(WIDGET).set("id", 1),
            lambda base: base.join(OTHER).on(WIDGET.id == OTHER.id),
            "other",
            lambda base: base.join(GADGET).on(WIDGET.id == GADGET.id),
            "gadget",
            id="update_from_join",
        ),
        pytest.param(
            make_union,
            lambda base: base.union(PostgresqlQuery.from_(THIRD).select(THIRD.id)),
            "third",
            lambda base: base.union(PostgresqlQuery.from_(FOURTH).select(FOURTH.id)),
            "fourth",
            id="set_operation_union",
        ),
    ],
)
def test_sibling_branches_are_independent(make_base, make_first, first_marker, make_second, second_marker):
    base = make_base()
    first = str(make_first(base))
    second = str(make_second(base))
    assert first_marker in first and second_marker not in first
    assert second_marker in second and first_marker not in second


def test_select_star_tables_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.select(WIDGET.star)
    assert base.get_sql() == base_sql
    assert branch.get_sql() != base_sql


def test_on_conflict_do_updates_branch_does_not_leak():
    # on_conflict() fields alone (no do_nothing/do_update yet) can't render SQL
    # (raises QueryException "No handler defined") - that's the base's valid,
    # incomplete state; what matters is that adding do_update() on a branch
    # doesn't complete the base's state too.
    base = PostgresqlQuery.into(WIDGET).columns("id").insert(1).on_conflict("id")
    branch = base.do_update("name", "updated")
    assert "DO UPDATE" in branch.get_sql()
    with pytest.raises(QueryException, match="No handler defined"):
        base.get_sql()


WIDGET = Table("widget")
GADGET = Table("gadget")
THIRD = Table("third")
FOURTH = Table("fourth")


# --- SetOperationQuery COW isolation ---------------------------------------------------------


# --- self-join --------------------------------------------------------------------------------


def test_self_join_without_alias_raises_instead_of_corrupting_shared_table():
    t = Table("emp")
    with pytest.raises(QueryException, match="Self-join requires an explicit alias"):
        PostgresqlQuery.from_(t).join(t).on(t.mgr_id == t.id)
    # the shared Table must be untouched by the failed attempt - a later, unrelated query
    # built from the same Table object must not see a leaked alias.
    assert t.alias is None
    assert "emp2" not in str(PostgresqlQuery.from_(t).select(t.id))


def test_self_join_with_explicit_alias_works():
    t = Table("emp")
    t2 = t.as_("manager")
    q = PostgresqlQuery.from_(t).join(t2).on(t.mgr_id == t2.id).select(t.id, t2.id)
    sql = str(q)
    assert '"emp" "manager"' in sql or "AS" in sql or "manager" in sql
    assert '"emp"."mgr_id"="manager"."id"' in sql.replace(" ", "")


# --- UPDATE...JOIN get_sql() idempotency ----------------------------------------------------------


def test_update_join_get_sql_is_idempotent():
    """_get_sql_with_self_aliased_update_and_returning() used to mutate self._from directly
    inside get_sql() itself (not a @builder-decorated mutator), permanently appending another
    duplicate self-aliased table reference to the FROM clause on every single call - so merely
    calling get_sql()/str() more than once on the same query object (e.g. to log it before
    executing) corrupted the generated SQL further each time."""
    users = Table("users")
    orders = Table("orders")
    q = (
        PostgresqlQuery.update(users)
        .join(orders)
        .on(orders.user_id == users.id)
        .set(users.name, "x")
        .where(orders.id == 1)
    )
    first = q.get_sql()
    second = q.get_sql()
    third = q.get_sql()
    assert first == second == third
    assert first.count("users_") == 1


# --- Preceding(0) / Following(0) ----------------------------------------------------------------


def test_preceding_zero_renders_as_zero_not_unbounded():
    assert str(Preceding(0)) == "0 PRECEDING"


def test_following_zero_renders_as_zero_not_unbounded():
    assert str(Following(0)) == "0 FOLLOWING"


def test_preceding_unbounded_still_renders_unbounded():
    assert str(Preceding()) == "UNBOUNDED PRECEDING"


def test_preceding_nonzero_unaffected():
    assert str(Preceding(3)) == "3 PRECEDING"
