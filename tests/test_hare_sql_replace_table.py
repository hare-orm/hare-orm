"""Regression tests for replace_table()/nodes_() on terms wrapping another term.

RangeCriterion/NestedCriterion subclasses previously either dropped part of their state during
replace_table (BetweenCriterion ignored start/end) or
retargeted the wrong attribute (NestedCriterion assigned right.replace_table(...) to
self.nested instead of nested.replace_table(...)). Negative (unary -term) had no
replace_table()/nodes_() overrides at all, so a table swap or a fields_()/tables_() walk never
reached the wrapped term.
"""

from hare.sql import Field, Table
from hare.sql.enums import Equality
from hare.sql.terms import NestedCriterion

WIDGET = Table("widget")
GADGET = Table("gadget")


def test_negative_replace_table_retargets_wrapped_term():
    negated = -Field("amount", table=WIDGET)
    replaced = negated.replace_table(WIDGET, GADGET)

    assert replaced.term.table is GADGET


def test_negative_nodes_includes_wrapped_term():
    field = Field("amount", table=WIDGET)
    negated = -field

    assert list(negated.nodes_()) == [negated, field, WIDGET]


def test_between_criterion_replace_table_retargets_start_and_end():
    criterion = Field("created_at", table=WIDGET).between(Field("start", table=WIDGET), Field("end", table=WIDGET))
    replaced = criterion.replace_table(WIDGET, GADGET)

    assert replaced.term.table is GADGET
    assert replaced.start.table is GADGET
    assert replaced.end.table is GADGET


def test_nested_criterion_replace_table_retargets_nested_not_right():
    left = Field("a", table=WIDGET)
    right = Field("b", table=WIDGET)
    nested = Field("c", table=WIDGET)
    criterion = NestedCriterion(Equality.EQ, Equality.EQ, left, right, nested)

    replaced = criterion.replace_table(WIDGET, GADGET)

    assert replaced.left.table is GADGET
    assert replaced.right.table is GADGET
    assert replaced.nested.table is GADGET


def test_hash_is_not_stale_after_replace_table():
    # Field.get_sql only includes the table namespace when the table carries an alias (or
    # ctx.with_namespace is set) - use aliased tables so the two Fields actually render
    # differently, otherwise hash equality would hold regardless of the caching bug.
    t1 = Table("t1").as_("a1")
    t2 = Table("t2").as_("a2")
    f = Field("col", table=t1)
    hash(f)  # force memoization against t1

    f2 = f.replace_table(t1, t2)

    assert hash(f2) != hash(Field("col", table=t1))
    assert hash(f2) == hash(Field("col", table=t2))


def test_set_does_not_collapse_distinct_fields_after_replace_table():
    t1 = Table("t1").as_("a1")
    t2 = Table("t2").as_("a2")
    f = Field("col", table=t1)
    hash(f)

    f2 = f.replace_table(t1, t2)

    assert {f2, Field("col", table=t1)} == {Field("col", table=t2), Field("col", table=t1)}
    assert len({f2, Field("col", table=t1)}) == 2
