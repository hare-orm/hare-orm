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

WIDGET = Table("widget")
GADGET = Table("gadget")


def test_from_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.from_(GADGET)
    assert base.get_sql() == base_sql
    assert "gadget" not in base.get_sql()
    assert "gadget" in branch.get_sql()


def test_with_branch_does_not_leak():
    cte = PostgresqlQuery.from_(GADGET).select(GADGET.id)
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.with_(cte, "cte_gadget")
    assert base.get_sql() == base_sql
    assert "cte_gadget" not in base.get_sql()
    assert "cte_gadget" in branch.get_sql()


def test_select_branches_are_independent_siblings():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch_a = base.select(WIDGET.name)
    branch_b = base.select(WIDGET.price)
    assert base.get_sql() == base_sql
    assert "name" in branch_a.get_sql() and "price" not in branch_a.get_sql()
    assert "price" in branch_b.get_sql() and "name" not in branch_b.get_sql()


def test_columns_branch_does_not_leak():
    base = PostgresqlQuery.into(WIDGET).columns("id").insert(1)
    base_sql = base.get_sql()
    branch = base.columns("name")
    assert base.get_sql() == base_sql
    assert "name" not in base.get_sql()
    assert "name" in branch.get_sql()


def test_values_branches_are_independent_siblings():
    base = PostgresqlQuery.into(WIDGET).columns("id")
    branch_a = base.insert(1)
    branch_b = base.insert(2)
    assert "1" in branch_a.get_sql() and "2" not in branch_a.get_sql()
    assert "2" in branch_b.get_sql() and "1" not in branch_b.get_sql()


def test_groupby_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.groupby(WIDGET.id)
    assert base.get_sql() == base_sql
    assert "GROUP BY" not in base.get_sql()
    assert "GROUP BY" in branch.get_sql()


def test_orderby_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.orderby(WIDGET.id)
    assert base.get_sql() == base_sql
    assert "ORDER BY" not in base.get_sql()
    assert "ORDER BY" in branch.get_sql()


def test_join_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.join(GADGET).on(WIDGET.id == GADGET.id)
    assert base.get_sql() == base_sql
    assert "JOIN" not in base.get_sql()
    assert "JOIN" in branch.get_sql()


def test_join_sibling_branches_are_independent():
    other = Table("other")
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    branch_a = base.join(GADGET).on(WIDGET.id == GADGET.id)
    branch_b = base.join(other).on(WIDGET.id == other.id)
    assert "gadget" in branch_a.get_sql() and "other" not in branch_a.get_sql()
    assert "other" in branch_b.get_sql() and "gadget" not in branch_b.get_sql()


def test_updates_branch_does_not_leak():
    base = PostgresqlQuery.update(WIDGET).set("id", 1)
    base_sql = base.get_sql()
    branch = base.set("name", "renamed")
    assert base.get_sql() == base_sql
    assert "name" not in base.get_sql()
    assert "name" in branch.get_sql()


def test_select_star_tables_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.select(WIDGET.star)
    assert base.get_sql() == base_sql
    assert branch.get_sql() != base_sql


def test_on_conflict_fields_branch_does_not_leak():
    base = PostgresqlQuery.into(WIDGET).columns("id").insert(1)
    base_sql = base.get_sql()
    branch = base.on_conflict("id").do_nothing()
    assert base.get_sql() == base_sql
    assert "CONFLICT" not in base.get_sql()
    assert "CONFLICT" in branch.get_sql()


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


def test_returning_branch_does_not_leak():
    base = PostgresqlQuery.into(WIDGET).columns("id").insert(1)
    base_sql = base.get_sql()
    branch = base.returning("id")
    assert base.get_sql() == base_sql
    assert "RETURNING" not in base.get_sql()
    assert "RETURNING" in branch.get_sql()


def test_returning_branch_does_not_leak_sqlite():
    base = SqliteQuery.into(WIDGET).columns("id").insert(1)
    base_sql = base.get_sql()
    branch = base.returning("id")
    assert base.get_sql() == base_sql
    assert "RETURNING" not in base.get_sql()
    assert "RETURNING" in branch.get_sql()


def test_distinct_on_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id)
    base_sql = base.get_sql()
    branch = base.distinct_on(Field("name"))
    assert base.get_sql() == base_sql
    assert "DISTINCT ON" not in base.get_sql()
    assert "DISTINCT ON" in branch.get_sql()


def test_update_from_join_appends_from_only_on_branch():
    # Rendering an UPDATE with joins moves them into its FROM; this must not
    # leak into a sibling built from the same base UPDATE query.
    other = Table("other")
    base = PostgresqlQuery.update(WIDGET).set("id", 1)
    branch_a = base.join(other).on(WIDGET.id == other.id)
    branch_b = base.join(GADGET).on(WIDGET.id == GADGET.id)
    sql_a = branch_a.get_sql()
    sql_b = branch_b.get_sql()
    assert "other" in sql_a and "gadget" not in sql_a
    assert "gadget" in sql_b and "other" not in sql_b


WIDGET = Table("widget")
GADGET = Table("gadget")
THIRD = Table("third")
FOURTH = Table("fourth")


# --- SetOperationQuery COW isolation ---------------------------------------------------------


def test_set_operation_union_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id).union(PostgresqlQuery.from_(GADGET).select(GADGET.id))
    base_sql = str(base)
    branch = base.union(PostgresqlQuery.from_(THIRD).select(THIRD.id))
    assert str(base) == base_sql
    assert "third" not in str(base)
    assert "third" in str(branch)


def test_set_operation_union_sibling_branches_are_independent():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id).union(PostgresqlQuery.from_(GADGET).select(GADGET.id))
    branch_a = base.union(PostgresqlQuery.from_(THIRD).select(THIRD.id))
    branch_b = base.union(PostgresqlQuery.from_(FOURTH).select(FOURTH.id))
    assert "third" in str(branch_a) and "fourth" not in str(branch_a)
    assert "fourth" in str(branch_b) and "third" not in str(branch_b)


def test_set_operation_orderby_branch_does_not_leak():
    base = PostgresqlQuery.from_(WIDGET).select(WIDGET.id).union(PostgresqlQuery.from_(GADGET).select(GADGET.id))
    base_sql = str(base)
    branch = base.orderby(WIDGET.id)
    assert str(base) == base_sql
    assert "ORDER BY" not in str(base)
    assert "ORDER BY" in str(branch)


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
