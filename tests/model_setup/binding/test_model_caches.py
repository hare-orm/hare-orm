"""Caches: every process-wide cache built for models registers itself, and a change to a model
drops what each of them holds for it - nothing has to list the caches."""

import gc
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from hare.core.caching.cache import Cache
from hare.core.caching.caches import Caches
from hare.core.caching.constants import ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL
from hare.core.caching.model_cache import ModelCache
from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from tests.shared_models.testmodels import Author, Book

#: The model-keyed caches that are not ModelCache - each registered in Caches itself.
#: Weak dictionaries that hold state, not something computed again when dropped.
WEAK_STATE = {
    # The schema a request query class had before preparation, restored on invalidation.
    # The request query classes reading each model - what forgetting a model goes by.
    ("hare/contrib/request_query/declaration/request_query_registry.py", "classes_by_model"),
    # Active delivery queues/errors of each scope and loop, never recomputable cache entries.
    ("hare/instrumentation/observers/observer_set.py", "self.dispatchers"),
    # The topics of the outbox events each open transaction wrote - signalled once it commits.
    ("hare/contrib/outbox/wakeups/outbox_wakeup.py", "self.pending_topics"),
    # The topics each open transaction sent a NOTIFY of - one NOTIFY per topic and transaction.
    ("hare/contrib/outbox/wakeups/listen_notify_wakeup.py", "self.notified_topics"),
    # A health check's readings of each pool at its previous run - what "since the last check" counts from.
    ("hare/health/health_check.py", "weakref.WeakKeyDictionary()"),
    # The cursors of each pool's measured waits and connects the pool metrics took.
    ("hare/contrib/opentelemetry/pool_metric_instruments.py", "self.cursors"),
}

REGISTERED_MODEL_KEYED_CACHES = {
    # Forgetting a model rebuilds the request query classes that read it.
    ("hare/contrib/request_query/declaration/request_query_registry.py", "classes_by_model"),
}


@pytest.mark.database_independent
def test_write_rules_are_bounded_and_writer_observes_model_invalidation(monkeypatch):
    cache = Cache(2, holds_sql=False)
    monkeypatch.setattr(RowWriteRules, "RULES_CACHE", cache)
    writer = object.__new__(InstanceWriter)
    writer.model = Author
    try:
        first = writer.get_update_rules(["name"], WriteType.UPDATE)
        assert writer.get_update_rules(["name"], WriteType.UPDATE) is first
        RowWriteRules.get(Author, WriteType.UPDATE, ["name"], "update()")
        RowWriteRules.get(Author, WriteType.UPDATE, ["name"], "bulk_update()")
        assert cache.get_bucket(Author).entry_count == 2
        after_eviction = writer.get_update_rules(["name"], WriteType.UPDATE)
        assert after_eviction is not first
        Caches.forget_model_caches([Author])
        assert cache.get_bucket(Author).entry_count == 0
        after_invalidation = writer.get_update_rules(["name"], WriteType.UPDATE)
        assert after_invalidation is not after_eviction
        assert after_invalidation.field_names == first.field_names
    finally:
        cache.clear()
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_global_keys_do_not_infer_model_lifetime():
    cache = Cache(2, holds_sql=False)
    try:
        cache[(Author, "key")] = "global"
        model_bucket = cache.get_bucket(Author)
        model_bucket[("key",)] = "model"
        cache.forget_model(Author)
        assert cache[(Author, "key")] == "global"
        assert not model_bucket
        assert list(cache) == [(Author, "key")]
        assert len(cache) == cache.entry_count == 1
    finally:
        cache.clear()
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_cache_instances_do_not_share_storage_when_object_addresses_are_reused(monkeypatch):
    monkeypatch.setattr("hare.core.caching.cache.id", lambda value: 1, raising=False)
    holder = SimpleNamespace(cache_buckets={})
    first = Cache(holds_sql=False)
    second = Cache(holds_sql=False)
    try:
        first.set_value(holder, "first")
        assert second.get_value(holder) is None
        second.set_value(holder, "second")
        second.clear()
        assert first.get_value(holder) == "first"
        assert second.get_value(holder) is None
    finally:
        first.clear()
        second.clear()
        Caches.registered.remove(first)
        Caches.registered.remove(second)


@pytest.mark.database_independent
def test_forgetting_a_models_sql_clears_only_the_sql_caches_in_place():
    model = SimpleNamespace(_meta=SimpleNamespace(plan_cache_buckets={}))
    sql_cache = Cache(2)
    plain_cache = Cache(2, holds_sql=False)
    try:
        bucket = sql_cache.get_bucket(model)
        bucket["statement"] = "old SQL"
        plain_cache.get_bucket(model)["fact"] = "value"
        Caches.forget_model_sql(model)
        assert sql_cache.get_bucket(model) is bucket
        assert not bucket
        assert plain_cache.get_bucket(model)["fact"] == "value"
    finally:
        sql_cache.clear()
        plain_cache.clear()
        Caches.registered.remove(sql_cache)
        Caches.registered.remove(plain_cache)
        Caches.sql_caches.remove(sql_cache)


@pytest.mark.database_independent
def test_a_model_cache_registers_itself_and_forgets_one_model():
    cache: ModelCache[str] = ModelCache()
    try:
        assert cache.storage in Caches.registered
        cache[Author] = "author"
        cache[Book] = "book"
        Caches.forget_model_caches([Author])
        assert dict(cache) == {Book: "book"}
    finally:
        Caches.registered.remove(cache.storage)


@pytest.mark.database_independent
def test_a_cache_of_values_depending_on_other_models_is_dropped_whole():
    cache: ModelCache[str] = ModelCache(depends_on_other_models=True)
    try:
        cache[Author] = "author"
        cache[Book] = "book"
        Caches.forget_model_caches([Author])
        assert dict(cache) == {}
    finally:
        Caches.registered.remove(cache.storage)


@pytest.mark.database_independent
def test_forgetting_every_cache_drops_every_registered_one():
    cache: ModelCache[str] = ModelCache()
    try:
        cache[Author] = "author"
        Caches.forget_all_caches()
        assert dict(cache) == {}
    finally:
        Caches.registered.remove(cache.storage)


@pytest.mark.database_independent
def test_no_cache_is_made_by_hand():
    """Every cache is a ``Cache`` or a ``ModelCache`` - nothing memoizes with functools, an
    ``OrderedDict`` or a weak dictionary of its own, which nothing would drop when the models or
    the registries change."""
    hand_made = re.compile(r"lru_cache|functools\.cache\b|^\s*@cache\b|OrderedDict\(|Weak(?:Key|Value)Dictionary\(")
    offenders = []
    for path in sorted(Path("hare").rglob("*.py")):
        relative_path = path.as_posix()
        if relative_path in {"hare/core/caching/cache.py", "hare/core/caching/cache_bucket.py"}:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if hand_made.search(line) and (relative_path, line.split(":")[0].strip()) not in WEAK_STATE:
                offenders.append(f"{relative_path}:{line_number}")
    assert offenders == []


@pytest.mark.database_independent
def test_a_model_bucket_is_invalidated_with_the_model():
    cache: Cache[str] = Cache(holds_sql=False)
    try:
        cache.get_bucket(Author)["key"] = "author"
        cache.get_bucket(Book)["key"] = "book"
        Caches.forget_model_caches([Author])
        assert cache.get_bucket(Author) == {}
        assert cache.get_bucket(Book) == {"key": "book"}
        Caches.forget_all_caches()
        assert cache.get_bucket(Book) == {}
    finally:
        cache.clear()
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_model_buckets_depending_on_other_models_are_dropped_together():
    cache: Cache[str] = Cache(holds_sql=False, depends_on_other_models=True)
    try:
        cache.get_bucket(Author)["key"] = "author"
        cache.get_bucket(Book)["key"] = "book"
        Caches.forget_model_caches([Author])
        assert cache.get_bucket(Book) == {}
    finally:
        cache.clear()
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_an_owner_bucket_is_emptied_in_place():
    class Owner:
        def __init__(self) -> None:
            self.cache_buckets: dict[int, dict[str, str]] = {}

    cache: Cache[str] = Cache(holds_sql=False)
    owner, other = Owner(), Owner()
    try:
        bucket = cache.get_bucket(owner)
        other_bucket = cache.get_bucket(other)
        assert cache.get_bucket(owner) is bucket
        bucket["key"] = "value"
        other_bucket["key"] = "other"
        cache.forget_holder(owner)
        assert bucket == {} and other_bucket == {"key": "other"}
        Caches.forget_all_caches()
        assert other_bucket == {}
    finally:
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_a_shared_bucket_is_emptied_in_place_and_gone_with_its_holders():
    cache: Cache[str] = Cache(holds_sql=False, depends_on_other_models=True)
    try:
        bucket = cache.new_shared_bucket()
        bucket["key"] = "value"
        Caches.forget_model_caches([Author])
        assert bucket == {}
        assert len(cache.buckets) == 1
        del bucket
        gc.collect()
        assert len(cache.buckets) == 0
    finally:
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_a_lifetime_value_is_absent_after_invalidation():
    class Owner:
        value: str | None = None

    cache: Cache[str] = Cache(holds_sql=False)
    owner = Owner()
    try:
        cache.set_value(owner, "kept")
        assert cache.get_value(owner) == "kept"
        Caches.forget_model_caches([Author])
        assert cache.get_value(owner) == "kept"
        Caches.forget_all_caches()
        assert cache.get_value(owner) is None
    finally:
        Caches.registered.remove(cache)


@pytest.mark.asyncio
async def test_a_manager_keeps_its_queryset_until_models_change(db):
    queryset = Author.objects
    assert Author.objects is queryset
    Caches.forget_model_caches([Book])
    assert Author.objects is not queryset


@pytest.mark.database_independent
def test_a_fact_is_computed_once_per_model_until_the_model_changes():
    calls = []

    @ModelCache.fact()
    def get_table(model: type[Model]) -> str:
        calls.append(model)
        return model._meta.db_table

    try:
        assert get_table(Author) == get_table(Author) == Author._meta.db_table
        assert calls == [Author]
        Caches.forget_model_caches([Book])
        get_table(Author)
        assert calls == [Author]
        Caches.forget_model_caches([Author])
        get_table(Author)
        assert calls == [Author, Author]
    finally:
        Caches.registered.remove(get_table.cache.storage)


@pytest.mark.database_independent
def test_every_model_keyed_cache_is_registered():
    """A cache keyed by model classes is a ModelCache - a WeakKeyDictionary of its own would be
    forgotten by nothing when its model changes."""
    declaration = re.compile(r"^\s+(\w+): ClassVar\[(?:weakref\.)?WeakKeyDictionary\[type\[\"?Model", re.MULTILINE)
    unregistered = []
    for path in sorted(Path("hare").rglob("*.py")):
        relative_path = path.as_posix()
        if relative_path.startswith(("hare/contrib/admin/", "hare/contrib/ui/", "hare/contrib/site/")):
            continue
        for match in declaration.finditer(path.read_text(encoding="utf-8")):
            if (relative_path, match.group(1)) not in REGISTERED_MODEL_KEYED_CACHES:
                unregistered.append(f"{relative_path} {match.group(1)}")
    assert unregistered == []


@pytest.mark.database_independent
def test_a_cache_dropped_with_another_is_dropped_whenever_that_one_drops_entries():
    source: Cache[str] = Cache()
    dependent: Cache[str] = Cache(dropped_with=source)
    try:
        source.get_bucket(Author)[("key",)] = "author"
        dependent.get_bucket(Book)[("key",)] = "book"
        source.forget_model(Author)
        assert dependent.get_bucket(Book).get(("key",)) is None
        dependent.get_bucket(Book)[("key",)] = "book"
        source.clear()
        assert dependent.get_bucket(Book).get(("key",)) is None
        dependent.get_bucket(Book)[("key",)] = "book"
        Caches.forget_model_caches([Author])
        assert dependent.get_bucket(Book).get(("key",)) is None
    finally:
        source.clear()
        Caches.registered.remove(source)
        Caches.registered.remove(dependent)


@pytest.mark.database_independent
@pytest.mark.parametrize("configured_size", ["many", "0", "-5", "1.5", "1000001"])
def test_a_cache_size_outside_the_bounds_is_a_configuration_error(monkeypatch, configured_size):
    monkeypatch.setenv(ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL, configured_size)

    with pytest.raises(ConfigurationError, match=ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL):
        Cache.max_size_from_env()


@pytest.mark.database_independent
def test_a_cache_size_within_the_bounds_is_taken(monkeypatch):
    monkeypatch.setenv(ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL, "64")

    assert Cache.max_size_from_env() == 64


@pytest.mark.database_independent
def test_bucket_eviction_preserves_recent_entries():
    """A model's plain bucket grew with every key of a JSON value a filter named - keys from data."""
    from hare.core.caching.cache_bucket import CacheBucket
    from hare.core.caching.constants import MAX_PLAIN_BUCKET_ENTRIES

    bucket = CacheBucket[int](MAX_PLAIN_BUCKET_ENTRIES)
    for number in range(MAX_PLAIN_BUCKET_ENTRIES):
        assert bucket.store(f"data__k{number}", number) == number
    assert len(bucket) == MAX_PLAIN_BUCKET_ENTRIES
    bucket.store("data__past", -1)
    assert bucket["data__past"] == -1
    assert len(bucket) == MAX_PLAIN_BUCKET_ENTRIES
    assert "data__k0" not in bucket
    assert bucket["data__k1"] == 1


@pytest.mark.database_independent
def test_a_hit_read_without_the_lock_keeps_its_value_when_cleared_meanwhile():
    """A read takes no lock: an invalidation in the middle of a hit doesn't wait for it, and the hit
    still returns the value it read - its LRU reorder finds the entry gone."""
    from hare.core.caching.cache_bucket import CacheBucket

    reading, release = Event(), Event()
    completed = []

    class PausedEntries(OrderedDict):
        def get(self, key, default=None):
            value = super().get(key, default)
            reading.set()
            assert release.wait(10), "reader watchdog"
            completed.append("read")
            return value

        def clear(self):
            completed.append("clear")
            super().clear()

    bucket = CacheBucket(2)
    bucket["value"] = 7
    bucket.entries = PausedEntries(bucket.entries)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(bucket.get, "value")
        try:
            assert reading.wait(10), "reader start watchdog"
            executor.submit(bucket.clear).result(timeout=10)
        finally:
            release.set()
        assert reader.result(timeout=10) == 7
    assert completed == ["clear", "read"]
    assert not bucket


@pytest.mark.database_independent
@pytest.mark.parametrize("replace_existing", [False, True])
def test_cache_bound_holds_while_a_writer_publishes_an_entry(replace_existing):
    from hare.core.caching.cache_bucket import CacheBucket

    published, release = Event(), Event()

    class PausedEntries(OrderedDict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if value == 10:
                published.set()
                assert release.wait(10), "writer watchdog"

    bucket = CacheBucket[int](2)
    bucket[0] = 0
    bucket[1] = 1
    bucket.entries = PausedEntries(bucket.entries)
    key = 1 if replace_existing else 2
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(bucket.store, key, 10)
        try:
            assert published.wait(10), "writer start watchdog"
            assert len(bucket) == bucket.entry_count == 2
            assert set(bucket) == ({0, 1} if replace_existing else {1, 2})
            assert bucket.peek(key) == 10
        finally:
            release.set()
        assert writer.result(timeout=10) == 10


@pytest.mark.database_independent
def test_lock_free_reads_survive_concurrent_evictions_and_clears():
    """Readers hitting a bucket while other threads store past its bound and clear it get the stored
    value or the default - never an error of a reorder, an iteration or a count racing a write."""
    from hare.core.caching.cache_bucket import CacheBucket

    bucket = CacheBucket[int](4)
    start = Barrier(4)

    def read(worker):
        start.wait(timeout=10)
        for number in range(3000):
            key = number % 8
            assert bucket.get(key) in (None, key)
            if number % 50 == 0:
                assert len(bucket) <= 4
                assert all(isinstance(entry_key, int) for entry_key in bucket)

    def write(worker):
        start.wait(timeout=10)
        for number in range(3000):
            bucket[number % 8] = number % 8
            if number % 97 == 0:
                bucket.clear()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(read, 0),
            executor.submit(read, 1),
            executor.submit(write, 2),
            executor.submit(write, 3),
        ]
        for future in futures:
            future.result(timeout=60)
    assert bucket.entry_count <= 4


@pytest.mark.database_independent
def test_concurrent_cache_users_share_bounded_holder_storage():
    holder = SimpleNamespace()
    cache = Cache(7, holds_sql=False)
    start = Barrier(4)

    def use_cache(worker):
        start.wait(timeout=10)
        bucket = cache.get_bucket(holder)
        for number in range(80):
            assert cache.get_bucket(holder) is bucket
            key = (worker, number % 8)
            bucket[key] = number
            assert bucket.get(key) in (None, number)
            if number % 17 == 0:
                cache.clear()
        return bucket

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            buckets = list(executor.map(use_cache, range(4)))
        assert all(bucket is buckets[0] for bucket in buckets)
        assert cache.entry_count <= 7
        cache.clear()
        assert cache.get_bucket(holder) is buckets[0]
        assert not buckets[0]
    finally:
        cache.clear()
        Caches.registered.remove(cache)


@pytest.mark.database_independent
def test_peek_reads_a_value_without_moving_it_in_the_lru_order():
    cache: Cache[str] = Cache(2, holds_sql=False)
    holder = SimpleNamespace()
    bucket = cache.get_bucket(holder)
    bucket["first"] = "a"
    bucket["second"] = "b"

    assert bucket.peek("first") == "a"
    assert bucket.peek("missing", "default") == "default"
    bucket["third"] = "c"

    assert "first" not in bucket
    assert list(bucket) == ["second", "third"]


@pytest.mark.database_independent
def test_a_model_value_missed_makes_no_bucket_and_a_stored_one_reads_back():
    cache = ModelCache()
    assert cache.storage.get_model_value(Author, "missing") == "missing"
    assert cache.storage.bucket_id not in Author._meta.plan_cache_buckets

    cache[Author] = "kept"

    assert cache.storage.get_model_value(Author) == "kept"
    assert cache.get(Author) == "kept"
    del cache[Author]
