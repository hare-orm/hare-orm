"""Caches: every process-wide cache built for models registers itself, and a change to a model
drops what each of them holds for it - nothing has to list the caches."""

import gc
import re
from pathlib import Path

import pytest

from hare.core.cache import Cache
from hare.core.caches import Caches
from hare.core.model_cache import ModelCache
from hare.models import Model
from tests.testmodels import Author, Book

#: The model-keyed caches that are not ModelCache - each registered in Caches itself.
#: Weak dictionaries that hold state, not something computed again when dropped.
WEAK_STATE = {
    # The fields a request query class had before its parameters were prepared - put back.
    ("hare/contrib/request_query/base.py", "unprepared_fields"),
    # The request query classes reading each model - what forgetting a model goes by.
    ("hare/contrib/request_query/base.py", "classes_by_model"),
    # The latest dispatch per event loop - the next one waits for it.
    ("hare/instrumentation/observer_dispatch.py", "weakref.WeakKeyDictionary()"),
}

REGISTERED_MODEL_KEYED_CACHES = {
    # Forgetting a model rebuilds the request query classes that read it.
    ("hare/contrib/request_query/base.py", "classes_by_model"),
}


def test_a_model_cache_registers_itself_and_forgets_one_model():
    cache: ModelCache[str] = ModelCache()
    try:
        assert cache in Caches.registered
        cache[Author] = "author"
        cache[Book] = "book"
        Caches.forget_model_caches([Author])
        assert dict(cache) == {Book: "book"}
    finally:
        Caches.registered.remove(cache)


def test_a_cache_of_values_depending_on_other_models_is_dropped_whole():
    cache: ModelCache[str] = ModelCache(depends_on_other_models=True)
    try:
        cache[Author] = "author"
        cache[Book] = "book"
        Caches.forget_model_caches([Author])
        assert dict(cache) == {}
    finally:
        Caches.registered.remove(cache)


def test_forgetting_every_cache_drops_every_registered_one():
    cache: ModelCache[str] = ModelCache()
    try:
        cache[Author] = "author"
        Caches.forget_all_caches()
        assert dict(cache) == {}
    finally:
        Caches.registered.remove(cache)


def test_no_cache_is_made_by_hand():
    """Every cache is a ``Cache`` or a ``ModelCache`` - nothing memoizes with functools, an
    ``OrderedDict`` or a weak dictionary of its own, which nothing would drop when the models or
    the registries change."""
    hand_made = re.compile(r"lru_cache|functools\.cache\b|^\s*@cache\b|OrderedDict\(|Weak(?:Key|Value)Dictionary\(")
    offenders = []
    for path in sorted(Path("hare").rglob("*.py")):
        relative_path = path.as_posix()
        if relative_path == "hare/core/cache.py":
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if hand_made.search(line) and (relative_path, line.split(":")[0].strip()) not in WEAK_STATE:
                offenders.append(f"{relative_path}:{line_number}")
    assert offenders == []


def test_a_model_bucket_is_a_plain_dict_dropped_with_the_model():
    cache: Cache[str] = Cache(holds_sql=False, keyed_by_model=False)
    try:
        cache.get_model_bucket(Author)["key"] = "author"
        cache.get_model_bucket(Book)["key"] = "book"
        Caches.forget_model_caches([Author])
        assert cache.get_model_bucket(Author) == {}
        assert cache.get_model_bucket(Book) == {"key": "book"}
        Caches.forget_all_caches()
        assert cache.get_model_bucket(Book) == {}
    finally:
        cache.clear()
        Caches.registered.remove(cache)


def test_model_buckets_depending_on_other_models_are_dropped_together():
    cache: Cache[str] = Cache(holds_sql=False, keyed_by_model=False, depends_on_other_models=True)
    try:
        cache.get_model_bucket(Author)["key"] = "author"
        cache.get_model_bucket(Book)["key"] = "book"
        Caches.forget_model_caches([Author])
        assert cache.get_model_bucket(Book) == {}
    finally:
        cache.clear()
        Caches.registered.remove(cache)


def test_an_owner_bucket_is_emptied_in_place():
    class Owner:
        def __init__(self) -> None:
            self.cache_buckets: dict[int, dict[str, str]] = {}

    cache: Cache[str] = Cache(holds_sql=False, keyed_by_model=False)
    owner, other = Owner(), Owner()
    try:
        bucket = cache.get_owner_bucket(owner)
        other_bucket = cache.get_owner_bucket(other)
        assert cache.get_owner_bucket(owner) is bucket
        bucket["key"] = "value"
        other_bucket["key"] = "other"
        cache.forget_owner(owner)
        assert bucket == {} and other_bucket == {"key": "other"}
        Caches.forget_all_caches()
        assert other_bucket == {}
    finally:
        Caches.registered.remove(cache)


def test_a_shared_bucket_is_emptied_in_place_and_gone_with_its_holders():
    cache: Cache[str] = Cache(holds_sql=False, keyed_by_model=False, depends_on_other_models=True)
    try:
        bucket = cache.new_shared_bucket()
        bucket["key"] = "value"
        Caches.forget_model_caches([Author])
        assert bucket == {}
        assert len(cache.shared_buckets) == 1
        del bucket
        gc.collect()
        assert len(cache.shared_buckets) == 0
    finally:
        Caches.registered.remove(cache)


def test_an_owner_value_is_set_back_to_none_when_dropped():
    class Owner:
        value: str | None = None

    cache: Cache[str] = Cache(holds_sql=False, keyed_by_model=False, owner_attribute="value")
    owner = Owner()
    try:
        cache.set_owner_value(owner, "kept")
        assert owner.value == "kept"
        Caches.forget_model_caches([Author])
        assert owner.value == "kept"
        Caches.forget_all_caches()
        assert owner.value is None
    finally:
        Caches.registered.remove(cache)


@pytest.mark.asyncio
async def test_a_manager_keeps_its_queryset_until_models_change(db):
    queryset = Author.objects
    assert Author.objects is queryset
    Caches.forget_model_caches([Book])
    assert Author.objects is not queryset


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
        Caches.registered.remove(get_table.cache)


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
