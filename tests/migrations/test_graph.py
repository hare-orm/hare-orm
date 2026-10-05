from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import pytest

from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import CircularDependencyError, MigrationLoadError, UnknownMigrationError
from hare.migrations.loading.graph import MigrationGraph, MigrationKey


def _key(app_label: str, name: str) -> MigrationKey:
    return MigrationKey(app_label=app_label, name=name)


def test_add_node_duplicate_raises():
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    with pytest.raises(MigrationLoadError, match="Duplicate migration node"):
        graph.add_node(_key("app", "0001_initial"), migration=object())


def test_add_dependency_missing_child_raises_on_validate():
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    with pytest.raises(MigrationLoadError, match="references nonexistent child"):
        graph.add_dependency(
            _key("app", "0001_initial"),
            child=_key("app", "0002_missing"),
            parent=_key("app", "0001_initial"),
        )


def test_add_dependency_missing_parent_raises_on_validate():
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    with pytest.raises(MigrationLoadError, match="references nonexistent parent"):
        graph.add_dependency(
            _key("app", "0001_initial"),
            child=_key("app", "0001_initial"),
            parent=_key("app", "0000_ghost"),
        )


def test_add_dependency_skip_validation_defers_the_error():
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    # No error raised immediately...
    graph.add_dependency(
        _key("app", "0001_initial"),
        child=_key("app", "0002_missing"),
        parent=_key("app", "0001_initial"),
        skip_validation=True,
    )
    # ...but the dangling reference is still recorded and surfaces on demand.
    with pytest.raises(MigrationLoadError, match="references nonexistent child"):
        graph.validate_consistency()


def test_forwards_plan_unknown_target_raises():
    graph = MigrationGraph()
    with pytest.raises(UnknownMigrationError, match="Unknown migration target"):
        graph.forwards_plan(_key("app", "ghost"))


def test_backwards_plan_unknown_target_raises():
    graph = MigrationGraph()
    with pytest.raises(UnknownMigrationError, match="Unknown migration target"):
        graph.backwards_plan(_key("app", "ghost"))


def test_root_and_leaf_nodes_filtered_by_app_label():
    graph = MigrationGraph()
    graph.add_node(_key("app_a", "0001_initial"), migration=object())
    graph.add_node(_key("app_a", "0002_second"), migration=object())
    graph.add_node(_key("app_b", "0001_initial"), migration=object())
    graph.add_dependency(
        _key("app_a", "0002_second"),
        child=_key("app_a", "0002_second"),
        parent=_key("app_a", "0001_initial"),
    )

    assert graph.root_nodes("app_a") == [_key("app_a", "0001_initial")]
    assert graph.leaf_nodes("app_a") == [_key("app_a", "0002_second")]
    assert graph.root_nodes("app_b") == [_key("app_b", "0001_initial")]
    assert graph.leaf_nodes() == sorted([_key("app_a", "0002_second"), _key("app_b", "0001_initial")])


def test_leaf_and_root_nodes_ignore_a_cross_app_dependency_when_scoped():
    """app_a's own single migration is depended upon by app_b (e.g. an FK to app_a's model,
    generating `dependencies=[("app_a", "0001_initial")]` on app_b's side) - app_a's migration
    now has a CHILD, but that child belongs to a DIFFERENT app. Scoped to app_a,
    leaf_nodes("app_a")/get_single_leaf("app_a") must still report it as app_a's own leaf - a
    cross-app child says nothing about whether more of app_a's OWN history follows it. The
    unscoped, graph-wide leaf_nodes() call is unaffected (app_a's migration genuinely isn't a
    global sink here, since something in the graph does depend on it)."""
    graph = MigrationGraph()
    graph.add_node(_key("app_a", "0001_initial"), migration=object())
    graph.add_node(_key("app_b", "0001_initial"), migration=object())
    graph.add_dependency(
        _key("app_b", "0001_initial"),
        child=_key("app_b", "0001_initial"),
        parent=_key("app_a", "0001_initial"),
    )

    assert graph.leaf_nodes("app_a") == [_key("app_a", "0001_initial")]
    assert graph.get_single_leaf("app_a") == _key("app_a", "0001_initial")
    assert graph.leaf_nodes() == [_key("app_b", "0001_initial")]
    # app_b's own migration has a cross-app PARENT (app_a) - root_nodes("app_b") must likewise
    # still report it as app_b's own root; only a same-app parent should disqualify it.
    assert graph.root_nodes("app_b") == [_key("app_b", "0001_initial")]


def test_get_single_leaf_returns_the_one_head():
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    graph.add_node(_key("app", "0002_second"), migration=object())
    graph.add_dependency(
        _key("app", "0002_second"),
        child=_key("app", "0002_second"),
        parent=_key("app", "0001_initial"),
    )

    assert graph.get_single_leaf("app") == _key("app", "0002_second")


def test_get_single_leaf_returns_none_for_unmigrated_app():
    graph = MigrationGraph()
    assert graph.get_single_leaf("app") is None


def test_get_single_leaf_raises_on_conflicting_heads():
    """Two migrations both depending on the same parent - a genuine fork with no merge
    migration - must raise instead of letting a caller silently pick (or apply) an arbitrary
    branch, the same way Django's own migration executor refuses to guess through it."""
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    graph.add_node(_key("app", "0002_a"), migration=object())
    graph.add_node(_key("app", "0002_b"), migration=object())
    graph.add_dependency(
        _key("app", "0002_a"),
        child=_key("app", "0002_a"),
        parent=_key("app", "0001_initial"),
    )
    graph.add_dependency(
        _key("app", "0002_b"),
        child=_key("app", "0002_b"),
        parent=_key("app", "0001_initial"),
    )

    with pytest.raises(ConfigurationError, match="Conflicting migrations"):
        graph.get_single_leaf("app")


def test_forwards_and_backwards_plan_order():
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_initial"), migration=object())
    graph.add_node(_key("app", "0002_second"), migration=object())
    graph.add_node(_key("app", "0003_third"), migration=object())
    graph.add_dependency(
        _key("app", "0002_second"),
        child=_key("app", "0002_second"),
        parent=_key("app", "0001_initial"),
    )
    graph.add_dependency(
        _key("app", "0003_third"),
        child=_key("app", "0003_third"),
        parent=_key("app", "0002_second"),
    )

    assert graph.forwards_plan(_key("app", "0003_third")) == [
        _key("app", "0001_initial"),
        _key("app", "0002_second"),
        _key("app", "0003_third"),
    ]
    assert graph.backwards_plan(_key("app", "0001_initial")) == [
        _key("app", "0003_third"),
        _key("app", "0002_second"),
        _key("app", "0001_initial"),
    ]


def _graph_with_cross_app_dependents() -> MigrationGraph:
    """blog.0001 <- blog.0002 <- shop.0001, and blog.0001 <- blog.0003 <- shop.0002 (branch)."""
    graph = MigrationGraph()
    for key in (
        _key("blog", "0001"),
        _key("blog", "0002"),
        _key("blog", "0003"),
        _key("shop", "0001"),
        _key("shop", "0002"),
    ):
        graph.add_node(key, migration=object())
    for child, parent in (
        (_key("blog", "0002"), _key("blog", "0001")),
        (_key("blog", "0003"), _key("blog", "0001")),
        (_key("shop", "0001"), _key("blog", "0002")),
        (_key("shop", "0002"), _key("blog", "0003")),
    ):
        graph.add_dependency(child, child=child, parent=parent)
    return graph


def test_same_app_children_excludes_other_apps():
    graph = _graph_with_cross_app_dependents()

    assert graph.same_app_children(_key("blog", "0001")) == [_key("blog", "0002"), _key("blog", "0003")]
    assert graph.same_app_children(_key("blog", "0002")) == []
    assert graph.same_app_children(_key("shop", "0001")) == []


def test_same_app_children_unknown_target_raises():
    with pytest.raises(UnknownMigrationError, match="Unknown migration target"):
        MigrationGraph().same_app_children(_key("app", "ghost"))


def test_backwards_plan_for_all_puts_every_dependent_before_its_dependency():
    graph = _graph_with_cross_app_dependents()

    plan = graph.backwards_plan_for_all([_key("blog", "0002"), _key("blog", "0003")])

    assert set(plan) == {_key("blog", "0002"), _key("blog", "0003"), _key("shop", "0001"), _key("shop", "0002")}
    assert plan.index(_key("shop", "0001")) < plan.index(_key("blog", "0002"))
    assert plan.index(_key("shop", "0002")) < plan.index(_key("blog", "0003"))
    assert len(plan) == len(set(plan))


def test_backwards_plan_for_all_without_targets_is_empty():
    assert _graph_with_cross_app_dependents().backwards_plan_for_all([]) == []


def test_backwards_plan_for_all_unknown_target_raises():
    with pytest.raises(UnknownMigrationError, match="Unknown migration target"):
        MigrationGraph().backwards_plan_for_all([_key("app", "ghost")])


def test_validate_consistency_raises_on_cycle_with_zero_leaf_nodes():
    """A cycle where every node's only child is a DIFFERENT app leaves the unscoped,
    graph-wide `leaf_nodes()` empty - a caller that only starts its traversal from leaf_nodes()
    (the default, no-explicit-target migrate plan) would find nothing to walk and silently see
    no cycle at all. validate_consistency() must catch it anyway, by walking every node's own
    dependency chain.

    leaf_nodes(app_label) itself (scoped to one app) is NOT empty here for either app - each
    node's only child belongs to the OTHER app, and a cross-app child doesn't count against a
    node's own app-scoped leaf status (see leaf_nodes()'s own docstring) - so each node is,
    correctly, still its own app's leaf despite sitting inside a cross-app cycle."""
    graph = MigrationGraph()
    graph.add_node(_key("app_a", "0001_a"), migration=object())
    graph.add_node(_key("app_b", "0001_b"), migration=object())
    graph.add_dependency(
        _key("app_a", "0001_a"),
        child=_key("app_a", "0001_a"),
        parent=_key("app_b", "0001_b"),
        skip_validation=True,
    )
    graph.add_dependency(
        _key("app_b", "0001_b"),
        child=_key("app_b", "0001_b"),
        parent=_key("app_a", "0001_a"),
        skip_validation=True,
    )

    assert graph.leaf_nodes() == []
    assert graph.leaf_nodes("app_a") == [_key("app_a", "0001_a")]
    assert graph.leaf_nodes("app_b") == [_key("app_b", "0001_b")]
    with pytest.raises(CircularDependencyError):
        graph.validate_consistency()


def test_forwards_plan_circular_dependency_raises_instead_of_hanging():
    """A cycle in the dependency graph must raise, not spin forever.

    Bounded by a worker thread with a hard timeout so a regression back to the
    unbounded-stack-growth bug fails fast instead of hanging the test run.
    """
    graph = MigrationGraph()
    graph.add_node(_key("app", "0001_a"), migration=object())
    graph.add_node(_key("app", "0002_b"), migration=object())
    graph.add_dependency(
        _key("app", "0002_b"),
        child=_key("app", "0002_b"),
        parent=_key("app", "0001_a"),
        skip_validation=True,
    )
    graph.add_dependency(
        _key("app", "0001_a"),
        child=_key("app", "0001_a"),
        parent=_key("app", "0002_b"),
        skip_validation=True,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(graph.forwards_plan, _key("app", "0002_b"))
        try:
            with pytest.raises(CircularDependencyError):
                future.result(timeout=5)
        except FutureTimeoutError:
            pytest.fail("forwards_plan hung on a circular dependency instead of raising")
