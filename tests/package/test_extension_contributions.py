"""Extensions publish a whole generation or leave the old generation and caches intact."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hare.core.caching.cache import Cache
from hare.core.caching.caches import Caches
from hare.core.extensions import ExtensionContribution, LookupRegistration, MethodRegistration
from hare.core.extensions.declarations import MappingRegistration
from hare.core.extensions.extension_loader import ExtensionLoader
from hare.core.extensions.snapshot_mapping import SnapshotMapping
from hare.core.registries import Registries
from hare.dialects.base.sql_dialect import SqlDialect
from hare.exceptions import ConfigurationError
from hare.fields import CharField
from hare.fields.registrations.registered_lookup import RegisteredLookup
from hare.query.execution.query_execution import QueryExecution
from hare.query.filters import FieldLookup
from hare.query.queryset.extensions.query_set_extension import QuerySetExtension

pytestmark = pytest.mark.database_independent


class ExtensionDialect(SqlDialect):
    name = "extension_test"


def test_invalid_last_component_does_not_publish_or_invalidate(monkeypatch):
    snapshot = Registries.snapshot
    invalidations = []
    monkeypatch.setattr(Registries, "forget_all_caches", lambda: invalidations.append(True))
    contribution = ExtensionContribution(
        dialects=(ExtensionDialect(),),
        lookups=(
            LookupRegistration(
                CharField,
                "extension_equals",
                RegisteredLookup(lambda field: FieldLookup(lambda term, value: term == value)),
            ),
        ),
        methods=(MethodRegistration("filter", ExtensionDialect, QuerySetExtension(lambda builder: builder)),),
    )
    with pytest.raises(ConfigurationError, match="filter"):
        Registries.register(contribution)
    assert Registries.snapshot is snapshot
    assert invalidations == []
    assert "extension_equals" not in CharField.get_class_lookups(CharField)


def test_success_publishes_once_and_keeps_previous_snapshot_unchanged(monkeypatch):
    snapshot = Registries.snapshot
    monkeypatch.setattr(Registries, "snapshot", snapshot)
    invalidations = []
    monkeypatch.setattr(Registries, "forget_all_caches", lambda: invalidations.append(True))
    dialect = ExtensionDialect()
    Registries.register(ExtensionContribution(dialects=(dialect,)))
    current = Registries.snapshot
    assert current.generation == snapshot.generation + 1
    assert current.dialects[dialect.name] is dialect
    assert dialect.name not in snapshot.dialects
    Registries.register(ExtensionContribution(dialects=(dialect,)))
    assert Registries.snapshot is current
    assert invalidations == [True]
    with pytest.raises(TypeError):
        current.dialects["another"] = dialect


def test_failed_factory_is_not_repeated_until_refresh():
    loader = ExtensionLoader()
    attempts = []

    def factory():
        attempts.append(True)
        raise ConfigurationError("invalid declaration")

    for _ in range(2):
        with pytest.raises(ConfigurationError, match="invalid declaration"):
            loader.load("broken", factory)
    assert attempts == [True]
    loader.clear_failures()
    with pytest.raises(ConfigurationError, match="invalid declaration"):
        loader.load("broken", factory)
    assert attempts == [True, True]


def test_factory_cannot_publish_part_of_a_contribution():
    snapshot = Registries.snapshot

    def factory():
        CharField.register_lookup("extension_partial", lambda field: FieldLookup(lambda term, value: term == value))
        return ExtensionContribution()

    with pytest.raises(ConfigurationError, match="without publishing"):
        ExtensionLoader().load("partial", factory)
    assert Registries.snapshot is snapshot


def test_recursive_loading_has_an_explicit_diagnostic():
    loader = ExtensionLoader()

    def factory():
        loader.load("cycle", factory)
        return ExtensionContribution()

    with pytest.raises(ConfigurationError, match="Recursive extension loading: cycle"):
        loader.load("cycle", factory)
    assert not loader.active


@pytest.mark.asyncio
async def test_active_execution_keeps_declarations_and_cache_entries_separate(monkeypatch):
    monkeypatch.setattr(Registries, "snapshot", Registries.snapshot)
    mapping = SnapshotMapping({"column": "before"})
    mapping.seal()
    cache = Cache(2, holds_sql=False)
    previous = QueryExecution()
    try:
        mapping.register("column", "after")
        cache[("column",)] = "new statement"

        async def read_previous():
            assert mapping["column"] == "before"
            assert cache.get(("column",)) is None
            cache[("column",)] = "old statement"

            async def nested():
                assert mapping["column"] == "before"
                assert cache[("column",)] == "old statement"

            await QueryExecution().call(nested)

        await previous.call(read_previous)
        assert mapping["column"] == "after"
        assert cache[("column",)] == "new statement"
        assert cache.entry_count == 2
        cache[("another",)] = "third statement"
        assert cache.entry_count == 2
        assert Registries.execution_snapshot.get() is None
    finally:
        Caches.registered.remove(cache)


def test_component_and_mirror_registration_is_one_publication(monkeypatch):
    monkeypatch.setattr(Registries, "snapshot", Registries.snapshot)
    mapping, mirror = SnapshotMapping({"column": "a"}), SnapshotMapping({"column": "a"})
    mapping.seal()
    mirror.seal()
    previous = Registries.snapshot
    invalidations = []
    monkeypatch.setattr(Registries, "forget_all_caches", lambda: invalidations.append(True))
    mapping.register("column", "b", (mirror,))
    assert Registries.snapshot.generation == previous.generation + 1
    assert mapping["column"] == mirror["column"] == "b"
    assert invalidations == [True]
    token = Registries.execution_snapshot.set(previous)
    try:
        assert mapping["column"] == mirror["column"] == "a"
    finally:
        Registries.execution_snapshot.reset(token)


def test_component_declarations_cannot_escape_a_failed_contribution():
    mapping = SnapshotMapping({"column": "before"})
    mapping.seal()
    snapshot = Registries.snapshot
    contribution = ExtensionContribution(
        components=(MappingRegistration(mapping, "column", "after"),),
        methods=(MethodRegistration("filter", ExtensionDialect, QuerySetExtension(lambda builder: builder)),),
    )
    with pytest.raises(ConfigurationError, match="filter"):
        Registries.register(contribution)
    assert Registries.snapshot is snapshot
    assert mapping["column"] == "before"


def test_factory_cannot_mutate_an_existing_component():
    mapping = SnapshotMapping({"column": "before"})
    mapping.seal()

    def factory():
        mapping.register("column", "after")
        return ExtensionContribution()

    with pytest.raises(ConfigurationError, match="without publishing"):
        ExtensionLoader().load("partial component", factory)
    assert mapping["column"] == "before"


@pytest.mark.asyncio
async def test_iterator_pins_snapshot_through_pages_and_close_without_leaking_it(monkeypatch):
    monkeypatch.setattr(Registries, "snapshot", Registries.snapshot)
    mapping = SnapshotMapping({"column": "before"})
    mapping.seal()
    closed = []

    async def rows():
        try:
            while True:
                yield mapping["column"]
        finally:
            closed.append(mapping["column"])

    iterator = QueryExecution.iterate(SimpleNamespace(_execution_dependencies=()), rows())
    try:
        assert await anext(iterator) == "before"
        assert Registries.execution_snapshot.get() is None
        mapping.register("column", "after")
        assert mapping["column"] == "after"
        assert await anext(iterator) == "before"
    finally:
        await asyncio.create_task(iterator.aclose())
    assert closed == ["before"]
    assert Registries.execution_snapshot.get() is None
